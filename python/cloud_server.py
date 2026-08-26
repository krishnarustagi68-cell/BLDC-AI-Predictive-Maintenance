"""
================================================================
 BLDC Motor — Cloud Web Server
 Anyone with the link can view live motor health dashboard
================================================================
 Local run:
   python cloud_server.py
   Open http://localhost:8000

 Deploy to Render (free, public link):
   See README.md — Section 6 for step-by-step instructions

 ESP32 posts sensor data to:
   POST http://your-url.onrender.com/data

 Anyone views live dashboard at:
   http://your-url.onrender.com/
================================================================
"""
import os, pickle, logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List
from collections import deque

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from contextlib import asynccontextmanager

import config
import database
from ingestion import SerialIngestionWorker, scan_serial_ports
from fusion import DiagnosticFusionEngine

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("bldc")

MODELS_DIR = os.getenv("MODELS_DIR", os.path.join("..", "models"))

# ── ML Engine ────────────────────────────────────────────────
class MLEngine:
    km = km_map = lr = dt = scaler = le = features = None
    ready = False

    @classmethod
    def load(cls):
        def _l(f):
            p = Path(MODELS_DIR) / f
            return pickle.loads(p.read_bytes()) if p.exists() else None

        cls.km      = _l("kmeans.pkl")
        cls.km_map  = _l("kmeans_label_map.pkl")
        cls.lr      = _l("logistic_regression.pkl")
        cls.dt      = _l("decision_tree.pkl")
        cls.scaler  = _l("scaler.pkl")
        cls.le      = _l("label_encoder.pkl")
        fl          = _l("feature_list.pkl")
        cls.features = fl if fl else [
            "Temperature","VibrationX","VibrationY","VibrationZ",
            "Current","Voltage","RPM","MagX","MagY","MagZ"
        ]
        cls.ready = all([cls.km, cls.lr, cls.dt, cls.scaler, cls.le])
        status = "loaded OK" if cls.ready else "NOT LOADED — run train_models.py"
        logger.info(f"ML models: {status}")

    @classmethod
    def predict(cls, data: dict) -> dict:
        if not cls.ready:
            return {"kmeans":"—", "logistic_reg":"—", "decision_tree":"—"}
        try:
            x  = np.array([data.get(f,0) for f in cls.features]).reshape(1,-1)
            xs = cls.scaler.transform(x)
            km_c = cls.km.predict(xs)[0]
            km_p = cls.km_map.get(km_c, "Unknown")
            
            lr_pred_raw = cls.lr.predict(xs)[0]
            if isinstance(lr_pred_raw, (int, np.integer)):
                lr_p = cls.le.inverse_transform([lr_pred_raw])[0]
            else:
                lr_p = str(lr_pred_raw)

            dt_pred_raw = cls.dt.predict(x)[0]
            if isinstance(dt_pred_raw, (int, np.integer)):
                dt_p = cls.le.inverse_transform([dt_pred_raw])[0]
            else:
                dt_p = str(dt_pred_raw)
                
            return {"kmeans": km_p, "logistic_reg": lr_p, "decision_tree": dt_p}
        except Exception as e:
            logger.error(f"ML predict error: {e}")
            return {"kmeans":"—", "logistic_reg":"—", "decision_tree":"—"}


# ── Rule detection — EXACT ARDUINO MATCH ──────────────────────
def rule_detect(d: dict) -> str:
    rpm = d.get("RPM", 0)
    vz  = abs(d.get("VibrationZ", 0))
    
    # --- STEP 1: PRIORITY CHECK ---
    if rpm < 10:
        return "Motor OFF"

    # --- STEP 2: FAULT CHECKS (Only happens if RPM > 10) ---
    z_diff = abs(vz - 0.015)
    if z_diff > 0.035:          return "Misalignment"
    
    if rpm < 1285:              return "Overload"
    
    return "Normal"


# ── Trend prediction ─────────────────────────────────────────
def predict_trend(history: list, current: dict) -> str:
    if len(history) < 5: return "Stable"
    try:
        temps = [r["sensors"]["temperature"] for r in history[-10:] if "sensors" in r]
        rpms  = [r["sensors"]["rpm"]         for r in history[-10:] if "sensors" in r]
        if not temps or not rpms: return "Stable"
        avg_t = sum(temps) / len(temps)
        avg_r = sum(rpms)  / len(rpms)
        if current["Temperature"] > avg_t * 1.15 and current["RPM"] < avg_r * 0.85:
            return "At Risk"
        if current["Temperature"] > avg_t * 1.15 or current["RPM"] < avg_r * 0.85:
            return "Watch"
        return "Stable"
    except: return "Stable"


history: deque = deque(maxlen=200)
latest:  dict  = {}


class WSManager:
    def __init__(self): self.active = []
    async def connect(self, ws):
        await ws.accept(); self.active.append(ws)
    def disconnect(self, ws):
        if ws in self.active: self.active.remove(ws)
    async def broadcast(self, data):
        dead = []
        for ws in self.active:
            try:    await ws.send_json(data)
            except: dead.append(ws)
        for ws in dead: self.active.remove(ws)

wsm = WSManager()


# Globally shared worker and mode state
ingestion_worker = None
current_mode = "SIMULATION" # starts in simulation, but auto-promotes to LIVE if hardware starts posting
com_port = config.DEFAULT_PORT
baud_rate = config.DEFAULT_BAUD

def broadcast_alert(alert: dict):
    """Sends the alert packet over WebSockets to trigger frontend warnings."""
    import asyncio
    packet = {
        "event": "alert",
        "data": alert
    }
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(wsm.broadcast(packet), loop)
        else:
            asyncio.run(wsm.broadcast(packet))
    except Exception as e:
        logger.debug(f"Could not broadcast alert over WebSocket: {e}")

def evaluate_thresholds(d: dict, anomaly: dict):
    """Checks sensor parameters against static thresholds and fires warnings/critical alerts."""
    # 1. Log anomaly if flagged
    if anomaly.get("detected"):
        alert = database.log_alert(
            severity=anomaly["severity"],
            sensor=anomaly["sensor"],
            value=anomaly["value"],
            threshold=0.0,
            message=anomaly["message"]
        )
        if alert:
            broadcast_alert(alert)

    # 2. Check Static thresholds
    for sensor, limits in config.THRESHOLDS.items():
        if sensor not in d:
            continue
        val = d[sensor]
        
        # Check critical
        if "critical" in limits:
            crit = limits["critical"]
            is_crit = False
            if sensor in ["Temperature", "VibrationZ", "VibrationX", "Current", "MagZ"]:
                if val > crit:
                    is_crit = True
            elif sensor in ["Voltage", "RPM"]:
                if val < crit and val > 1.0: # ignore off state
                    is_crit = True
            
            if is_crit:
                alert = database.log_alert(
                    severity="CRITICAL",
                    sensor=sensor,
                    value=val,
                    threshold=crit,
                    message=f"CRITICAL: {sensor} value {val} exceeded safe limit {crit}!"
                )
                if alert:
                    broadcast_alert(alert)
                continue # skip warning if critical triggered

        # Check warning
        if "warning" in limits:
            warn = limits["warning"]
            is_warn = False
            if sensor in ["Temperature", "VibrationZ", "VibrationX", "Current", "MagZ"]:
                if val > warn:
                    is_warn = True
            elif sensor in ["Voltage", "RPM"]:
                if val < warn and val > 1.0:
                    is_warn = True
                    
            if is_warn:
                alert = database.log_alert(
                    severity="WARNING",
                    sensor=sensor,
                    value=val,
                    threshold=warn,
                    message=f"WARNING: {sensor} value {val} entered warning threshold {warn}."
                )
                if alert:
                    broadcast_alert(alert)

def process_incoming_data(d: dict, mode: str):
    """Normalized telemetry processor. Connects ingestion to ML classifiers, database, and WebSockets."""
    global latest
    
    # 1. Run ML predictions
    preds = MLEngine.predict(d)
    rule = rule_detect(d)
    
    # 2. Consensus Fusion & Health Score
    fusion = DiagnosticFusionEngine.fuse_diagnostics(
        preds["kmeans"], preds["logistic_reg"], preds["decision_tree"], rule
    )
    health_score = DiagnosticFusionEngine.calculate_health_score(d)
    trend = predict_trend(list(history), d)
    
    # 3. Anomaly Detection
    anomaly = DiagnosticFusionEngine.detect_anomalies(d, list(history))
    
    # 4. Threshold & Alert Manager
    evaluate_thresholds(d, anomaly)
    
    # 5. Log Telemetry to SQLite
    database.log_telemetry(d, health_score, 1 if anomaly.get("detected") else 0, fusion["final_prediction"], mode)
    
    # 6. Gather current diagnostics metrics
    hz_rate = 0.0
    dq_pct = 100.0
    serial_conn = False
    active_port = com_port
    
    global ingestion_worker
    if ingestion_worker:
        hz_rate = ingestion_worker.hz
        dq_pct = ingestion_worker.data_quality
        serial_conn = ingestion_worker.connected
        active_port = ingestion_worker.port
        
    record = {
        "id": len(history) + 1,
        "timestamp": d.get("Timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "device_id": d.get("device_id", active_port),
        "sensors": {
            "temperature": d["Temperature"],
            "vibrationX": d["VibrationX"],
            "vibrationY": d["VibrationY"],
            "vibrationZ": d["VibrationZ"],
            "current": d["Current"],
            "voltage": d.get("Voltage", 0.0),
            "rpm": d["RPM"],
            "magX": d.get("MagX", 0.0),
            "magY": d.get("MagY", 0.0),
            "magZ": d.get("MagZ", 0.0),
        },
        "analysis": {
            "rule_based": rule,
            "kmeans": preds["kmeans"],
            "logistic_reg": preds["logistic_reg"],
            "decision_tree": preds["decision_tree"],
            "final_prediction": fusion["final_prediction"],
            "confidence": fusion["confidence"],
            "consensus_state": fusion["consensus_state"],
            "explanation": fusion["explanation"],
            "recommendation": fusion["recommendation"],
            "future_risk": trend,
            "health_score": health_score,
            "anomaly": anomaly.get("detected", False),
            "mode": mode,
        },
        "diagnostics": {
            "hz": hz_rate if mode == "LIVE" else 1.0,
            "quality": dq_pct if mode == "LIVE" else 100.0,
            "connected": serial_conn if mode == "LIVE" else False,
            "port": active_port
        },
        "fault": fusion["final_prediction"] not in ["Normal", "Motor OFF"],
    }
    
    history.append(record)
    latest = record
    
    # Broadcast to WS clients in a thread-safe manner
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(wsm.broadcast(record), loop)
        else:
            asyncio.run(wsm.broadcast(record))
    except Exception as e:
        logger.debug(f"Failed to broadcast websocket telemetry packet: {e}")
        
    return record

def process_serial_data(d: dict):
    """Callback for SerialIngestionWorker. Auto-promotes mode to LIVE when ESP32 USB starts sending data."""
    global current_mode
    if current_mode != "LIVE":
        logger.info("Real ESP32 detected on USB Serial! Switching operating mode to LIVE.")
        current_mode = "LIVE"
    process_incoming_data(d, mode="LIVE")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite Database tables
    database.init_db()
    
    # Load ML Pickled Classifier models
    MLEngine.load()
    
    # Start background USB Serial reader thread
    global ingestion_worker
    ingestion_worker = SerialIngestionWorker(callback=process_serial_data, port=com_port, baud=baud_rate)
    ingestion_worker.start()
    
    yield
    
    # Clean disconnect on shutdown
    if ingestion_worker:
        ingestion_worker.stop()

app = FastAPI(title="BLDC Motor Monitor", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SensorPayload(BaseModel):
    Temperature: float
    VibrationX:  float
    VibrationY:  float
    VibrationZ:  float
    Current:     float
    Voltage:     Optional[float] = 0.0
    RPM:         float
    MagX:        Optional[float] = 0.0
    MagY:        Optional[float] = 0.0
    MagZ:        Optional[float] = 0.0
    device_id:   Optional[str]   = "ESP32"

    @validator("Temperature")
    def vt(cls, v):
        return round(v, 2)
    @validator("RPM")
    def vr(cls, v):
        return round(v, 1)
    @validator("Current")
    def vc(cls, v):
        return round(v, 4)


@app.post("/data")
async def receive_data(payload: SensorPayload):
    d = payload.dict()
    
    # Auto-promote to LIVE if wireless ESP32 client connects over Wi-Fi
    global current_mode
    if current_mode != "LIVE":
        logger.info("Real ESP32 posting data over Wi-Fi! Switching operating mode to LIVE.")
        current_mode = "LIVE"
        
    record = process_incoming_data(d, mode="LIVE")
    return record


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await wsm.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        wsm.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        wsm.disconnect(websocket)


@app.get("/predict")
def get_latest():
    if not latest:
        return {
            "sensors": {
                "temperature": 0.0, "vibrationX": 0.0, "vibrationY": 0.0, "vibrationZ": 0.0,
                "current": 0.0, "voltage": 0.0, "rpm": 0.0, "magX": 0.0, "magY": 0.0, "magZ": 0.0
            },
            "analysis": {
                "final_prediction": "Waiting for Device...",
                "confidence": 0,
                "consensus_state": "Offline",
                "explanation": "No telemetry stream connected.",
                "recommendation": "Connect hardware or start simulation.",
                "future_risk": "Stable",
                "health_score": 100,
                "anomaly": False,
                "mode": current_mode
            },
            "diagnostics": {
                "hz": 0.0,
                "quality": 100.0,
                "connected": False,
                "port": com_port
            }
        }
    return latest


# ── REST API Endpoints ──────────────────────────────────────────

@app.get("/api/telemetry/history")
def get_telemetry_history_api(limit: int = 100, mode: Optional[str] = None):
    """Fetches historical rows from the SQLite database."""
    return database.get_telemetry_history(limit=limit, mode=mode)


@app.get("/api/alerts")
def get_alerts_api(limit: int = 50, severity: Optional[str] = None, sensor: Optional[str] = None, include_ack: bool = True):
    """Retrieves warnings and alerts log list."""
    return database.get_alerts(limit=limit, severity=severity, sensor=sensor, include_ack=include_ack)


@app.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge_alert_api(alert_id: int):
    """Marks a specific alert event as acknowledged."""
    database.acknowledge_alert(alert_id)
    return {"status": "ok"}


@app.post("/api/alerts/clear")
def clear_alerts_api():
    """Wipes all logged alert history."""
    database.clear_alerts()
    return {"status": "ok"}


@app.get("/api/settings")
def get_settings_api():
    """Returns settings definitions, threshold configs, and active system details."""
    global com_port, baud_rate, current_mode
    ports = scan_serial_ports()
    
    hz_rate = 0.0
    dq_pct = 100.0
    serial_conn = False
    last_err = None
    
    global ingestion_worker
    if ingestion_worker:
        hz_rate = ingestion_worker.hz
        dq_pct = ingestion_worker.data_quality
        serial_conn = ingestion_worker.connected
        last_err = ingestion_worker.last_error
        
    return {
        "com_port": com_port,
        "baud_rate": baud_rate,
        "mode": current_mode,
        "available_ports": ports,
        "diagnostics": {
            "hz": hz_rate,
            "quality": dq_pct,
            "connected": serial_conn,
            "last_error": last_err
        },
        "thresholds": config.THRESHOLDS
    }


@app.post("/api/settings")
def update_settings_api(settings: dict):
    """Updates settings variables and switches COM ports dynamically."""
    global com_port, baud_rate, ingestion_worker
    
    port = settings.get("com_port", com_port)
    baud = int(settings.get("baud_rate", baud_rate))
    
    com_port = port
    baud_rate = baud
    
    if ingestion_worker:
        ingestion_worker.set_port(port, baud)
            
    # Update threshold configs
    thresh = settings.get("thresholds")
    if thresh:
        for sensor, limits in thresh.items():
            if sensor in config.THRESHOLDS:
                for k, v in limits.items():
                    try:
                        config.THRESHOLDS[sensor][k] = float(v)
                    except:
                        pass
                        
    return {"status": "ok", "settings": get_settings_api()}


@app.post("/api/mode")
def set_mode_api(mode_payload: dict):
    """Sets the current operating mode (LIVE vs SIMULATION)."""
    global current_mode
    mode = mode_payload.get("mode", "SIMULATION")
    if mode in ["LIVE", "SIMULATION"]:
        current_mode = mode
        logger.info(f"Operating mode manually toggled to {current_mode}")
        return {"status": "ok", "mode": current_mode}
    return {"status": "error", "message": "Invalid mode"}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


# ── Dashboard HTML ────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BLDC Motor SCADA Observability Platform</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Outfit:wght@300;400;500;600;700&display=swap');

:root {
  --bg-main: #090d16;
  --bg-card: #0f172a;
  --bg-card-hover: #1e293b;
  --border-color: #1e293b;
  --border-focus: #38bdf8;
  
  --accent-cyan: #38bdf8;
  --accent-indigo: #6366f1;
  --text-main: #f3f4f6;
  --text-muted: #64748b;
  
  --healthy: #10b981;
  --healthy-bg: rgba(16, 185, 129, 0.08);
  --warning: #f59e0b;
  --warning-bg: rgba(245, 158, 11, 0.08);
  --critical: #ef4444;
  --critical-bg: rgba(239, 68, 68, 0.08);
  --off-state: #64748b;
  --off-state-bg: rgba(100, 116, 139, 0.08);
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  background-color: var(--bg-main);
  background-image: 
    radial-gradient(circle at 0% 0%, rgba(99, 102, 241, 0.05) 0%, transparent 40%),
    radial-gradient(circle at 100% 100%, rgba(56, 189, 248, 0.05) 0%, transparent 40%);
  color: var(--text-main);
  font-family: 'Outfit', sans-serif;
  min-height: 100vh;
  padding-bottom: 3rem;
  overflow-x: hidden;
}

code, pre, .mono {
  font-family: 'JetBrains Mono', monospace;
}

header {
  background: rgba(9, 13, 22, 0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-color);
  padding: 0.75rem 2rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 100;
}

header h1 {
  font-size: 1.15rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.header-status-group {
  display: flex;
  align-items: center;
  gap: 1.25rem;
}

.badge-status {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.2rem 0.65rem;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  text-transform: uppercase;
}

.badge-status.live {
  background: rgba(56, 189, 248, 0.1);
  color: var(--accent-cyan);
  border: 1px solid rgba(56, 189, 248, 0.2);
}

.badge-status.sim {
  background: rgba(245, 158, 11, 0.1);
  color: var(--warning);
  border: 1px solid rgba(245, 158, 11, 0.2);
}

.badge-status.disconnected {
  background: rgba(239, 68, 68, 0.1);
  color: var(--critical);
  border: 1px solid rgba(239, 68, 68, 0.2);
}

.pulse-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background-color: currentColor;
  animation: pulse-glow 1.5s infinite;
}

@keyframes pulse-glow {
  0% { opacity: 0.3; }
  50% { opacity: 1; }
  100% { opacity: 0.3; }
}

main {
  max-width: 1600px;
  margin: 1.5rem auto;
  padding: 0 1.5rem;
  display: grid;
  grid-template-columns: 1fr;
  gap: 1.25rem;
}

/* Grid System definitions */
.dashboard-top-row {
  display: grid;
  grid-template-columns: 350px 1fr;
  gap: 1.25rem;
}

@media (max-width: 1200px) {
  .dashboard-top-row {
    grid-template-columns: 1fr;
  }
}

/* Base Industrial Cards */
.panel {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 1.25rem;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.panel:hover {
  border-color: rgba(56, 189, 248, 0.2);
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
  border-bottom: 1px solid rgba(255,255,255,0.03);
  padding-bottom: 0.5rem;
}

.panel-title {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

/* Central Health scoring panel */
.health-overview {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 1.5rem;
  justify-content: center;
}

.health-score-ring {
  position: relative;
  width: 140px;
  height: 140px;
  display: flex;
  justify-content: center;
  align-items: center;
  margin-bottom: 1rem;
}

.health-svg {
  width: 140px;
  height: 140px;
  transform: rotate(-90deg);
}

.health-svg circle {
  fill: none;
  stroke-width: 10;
}

.health-svg circle.bg {
  stroke: rgba(255, 255, 255, 0.03);
}

.health-svg circle.fg {
  stroke-dasharray: 376.99;
  stroke-dashoffset: 376.99;
  stroke-linecap: round;
  transition: stroke-dashoffset 0.8s ease;
}

.health-score-val {
  position: absolute;
  font-family: 'Space Grotesk', sans-serif;
  font-size: 2.25rem;
  font-weight: 700;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.health-score-val span {
  font-size: 0.65rem;
  color: var(--text-muted);
  text-transform: uppercase;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 400;
}

.health-status-text {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.25rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  text-transform: uppercase;
  margin-top: 0.25rem;
}

.health-meta-details {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 0.5rem;
  line-height: 1.5;
}

/* Bento Sensor Grid */
.sensors-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.sensor-card {
  cursor: pointer;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  height: 140px;
}

.sensor-card:hover {
  background-color: var(--bg-card-hover);
  border-color: var(--border-focus);
}

.sensor-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.sensor-name {
  font-size: 0.72rem;
  font-family: 'JetBrains Mono', monospace;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.sensor-status-badge {
  font-size: 0.6rem;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600;
  padding: 0.1rem 0.4rem;
  border-radius: 2px;
  text-transform: uppercase;
}

.sensor-status-badge.Normal { background: var(--healthy-bg); color: var(--healthy); }
.sensor-status-badge.Warning { background: var(--warning-bg); color: var(--warning); }
.sensor-status-badge.Critical { background: var(--critical-bg); color: var(--critical); }

.sensor-value-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 0.5rem 0;
}

.sensor-value {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.75rem;
  font-weight: 600;
  color: #fff;
}

.sensor-unit {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-left: 0.25rem;
}

.sensor-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.68rem;
  color: var(--text-muted);
  border-top: 1px solid rgba(255,255,255,0.02);
  padding-top: 0.35rem;
}

.sparkline-box {
  width: 70px;
  height: 20px;
}

/* Machine Learning Comparison */
.ml-comparison-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.ml-card {
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1rem;
  border-radius: 6px;
  background: rgba(255,255,255,0.01);
  border: 1px solid var(--border-color);
  transition: all 0.2s ease;
}

.ml-card:hover {
  transform: translateY(-2px);
  border-color: var(--border-focus);
}

.ml-info h4 {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.ml-info p {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1rem;
  font-weight: 600;
  margin-top: 0.2rem;
}

/* Alerts and Config Row */
.bottom-sections-grid {
  display: grid;
  grid-template-columns: 1fr 380px;
  gap: 1.25rem;
}

@media (max-width: 1024px) {
  .bottom-sections-grid {
    grid-template-columns: 1fr;
  }
}

/* Alerts Center */
.alert-center-panel {
  display: flex;
  flex-direction: column;
  max-height: 380px;
}

.alert-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}

.filter-group {
  display: flex;
  gap: 0.5rem;
}

.filter-btn, .btn-action {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.65rem;
  font-weight: 500;
  background: #1e293b;
  border: 1px solid var(--border-color);
  color: var(--text-main);
  padding: 0.25rem 0.6rem;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.filter-btn.active, .filter-btn:hover, .btn-action:hover {
  background: var(--accent-cyan);
  color: var(--bg-main);
  border-color: var(--accent-cyan);
}

.alert-list-box {
  flex-grow: 1;
  overflow-y: auto;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: rgba(0,0,0,0.2);
}

.alert-item {
  display: grid;
  grid-template-columns: 80px 100px 1fr 70px;
  align-items: center;
  padding: 0.65rem 1rem;
  border-bottom: 1px solid var(--border-color);
  font-size: 0.78rem;
  cursor: pointer;
}

.alert-item:hover {
  background: rgba(255,255,255,0.02);
}

.alert-item.acknowledged {
  opacity: 0.45;
}

.alert-time {
  color: var(--text-muted);
  font-size: 0.7rem;
}

.alert-badge {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.62rem;
  font-weight: 600;
  text-transform: uppercase;
  padding: 0.1rem 0.4rem;
  border-radius: 2px;
  width: fit-content;
}

.alert-badge.WARNING { background: var(--warning-bg); color: var(--warning); }
.alert-badge.CRITICAL { background: var(--critical-bg); color: var(--critical); }
.alert-badge.INFO { background: rgba(56, 189, 248, 0.08); color: var(--accent-cyan); }

.alert-ack-btn {
  background: none;
  border: 1px solid var(--border-color);
  color: var(--accent-cyan);
  font-size: 0.65rem;
  padding: 0.15rem 0.4rem;
  border-radius: 3px;
  cursor: pointer;
}

.alert-ack-btn:hover {
  background: var(--accent-cyan);
  color: var(--bg-main);
}

/* System Settings Panel */
.settings-form-row {
  margin-bottom: 0.85rem;
}

.settings-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.7rem;
  color: var(--text-muted);
  display: block;
  margin-bottom: 0.25rem;
  text-transform: uppercase;
}

.settings-input, .settings-select {
  width: 100%;
  background: #1e293b;
  border: 1px solid var(--border-color);
  color: var(--text-main);
  padding: 0.4rem 0.65rem;
  border-radius: 4px;
  font-size: 0.8rem;
  font-family: 'JetBrains Mono', monospace;
}

.settings-input:focus, .settings-select:focus {
  border-color: var(--border-focus);
  outline: none;
}

/* Diagnostics Data table */
.data-table-panel {
  display: flex;
  flex-direction: column;
}

.table-controls {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
  gap: 1rem;
}

.search-input {
  background: #121824;
  border: 1px solid var(--border-color);
  color: var(--text-main);
  padding: 0.4rem 0.75rem;
  border-radius: 4px;
  font-size: 0.78rem;
  width: 250px;
}

.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border-color);
  border-radius: 6px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.76rem;
}

th {
  background: rgba(255, 255, 255, 0.01);
  color: var(--text-muted);
  font-weight: 500;
  font-family: 'JetBrains Mono', monospace;
  padding: 0.6rem 0.85rem;
  text-align: left;
  border-bottom: 1px solid var(--border-color);
  text-transform: uppercase;
  font-size: 0.65rem;
  letter-spacing: 0.05em;
}

td {
  padding: 0.55rem 0.85rem;
  border-bottom: 1px solid rgba(255,255,255,0.02);
  font-family: 'JetBrains Mono', monospace;
}

tr:hover {
  background: rgba(255, 255, 255, 0.01);
}

.badge-row-prediction {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.62rem;
  font-weight: 600;
  text-transform: uppercase;
  padding: 0.15rem 0.45rem;
  border-radius: 10px;
}

.badge-row-prediction.Normal { background: var(--healthy-bg); color: var(--healthy); border: 1px solid rgba(16,185,129,0.15); }
.badge-row-prediction.Overheating { background: var(--critical-bg); color: var(--critical); border: 1px solid rgba(239,68,68,0.15); }
.badge-row-prediction.Misalignment { background: var(--warning-bg); color: var(--warning); border: 1px solid rgba(245,158,11,0.15); }
.badge-row-prediction.Overload { background: rgba(99, 102, 241, 0.08); color: var(--accent-indigo); border: 1px solid rgba(99,102,241,0.15); }
.badge-row-prediction.Motor.OFF { background: var(--off-state-bg); color: var(--off-state); border: 1px solid rgba(100,116,139,0.15); }

/* Modal Drawer Overlay */
.drawer-overlay {
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(5, 7, 12, 0.8);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  z-index: 1000;
  opacity: 0; pointer-events: none;
  transition: opacity 0.25s ease;
  display: flex;
  justify-content: flex-end;
}

.drawer-overlay.open {
  opacity: 1; pointer-events: auto;
}

.drawer-content {
  background: #0b0f19;
  border-left: 1px solid var(--border-color);
  width: 100%;
  max-width: 520px;
  height: 100%;
  padding: 2rem;
  overflow-y: auto;
  transform: translateX(100%);
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex;
  flex-direction: column;
}

.drawer-overlay.open .drawer-content {
  transform: translateX(0);
}

.drawer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border-color);
  padding-bottom: 0.85rem;
  margin-bottom: 1.5rem;
}

.drawer-title {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 1.25rem;
  font-weight: 700;
  color: #fff;
}

.drawer-close {
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 1.75rem;
  cursor: pointer;
  line-height: 1;
}

.drawer-close:hover { color: #fff; }

.stat-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
  margin-bottom: 1.25rem;
}

.stat-item {
  background: #0f172a;
  border: 1px solid var(--border-color);
  padding: 0.65rem 0.85rem;
  border-radius: 6px;
}

.stat-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.62rem;
  color: var(--text-muted);
  text-transform: uppercase;
}

.stat-value {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1rem;
  font-weight: 600;
  margin-top: 0.15rem;
}

.drawer-chart-container {
  height: 220px;
  margin-bottom: 1.5rem;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 0.5rem;
  background: rgba(0,0,0,0.15);
}

.interpretation-box {
  background: rgba(56, 189, 248, 0.03);
  border: 1px solid rgba(56, 189, 248, 0.15);
  padding: 1rem;
  border-radius: 6px;
  font-size: 0.85rem;
  line-height: 1.5;
  color: #e0f2fe;
}
</style>
</head>
<body>

<header>
  <h1>⚙ BLDC Predictive Observability Console</h1>
  <div class="header-status-group">
    <span class="badge-status" id="header-mode-badge"><span class="pulse-dot"></span><span id="header-mode-text">STANDBY</span></span>
    <span class="badge-status" id="header-status-badge"><span class="pulse-dot"></span><span id="header-status-text">OFFLINE</span></span>
  </div>
</header>

<main>
  <!-- Top Panel: Score and Gauges -->
  <div class="dashboard-top-row">
    <!-- Health Score Card -->
    <div class="panel health-overview">
      <div class="panel-title" style="align-self: flex-start; margin-bottom: 1.5rem;">📊 SYSTEM VIABILITY</div>
      <div class="health-score-ring">
        <svg class="health-svg" viewBox="0 0 140 140">
          <circle class="bg" cx="70" cy="70" r="60" />
          <circle class="fg" id="health-ring-fg" stroke="var(--healthy)" cx="70" cy="70" r="60" />
        </svg>
        <div class="health-score-val">
          <p id="health-value-num">100</p>
          <span>Health</span>
        </div>
      </div>
      <div class="health-status-text" id="health-status-text" style="color: var(--healthy);">HEALTHY</div>
      <div class="health-meta-details" id="health-consensus-desc">
        All diagnostic classifiers show nominal operating state. Confidence is 100%.
      </div>
    </div>

    <!-- Sensor Array Bento Grid -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">🎛 TELEMETRY BUS ARRAYS</div>
        <span style="font-size:0.65rem; font-family:'JetBrains Mono',monospace; color:var(--text-muted);" id="last-updated-timestamp">Last Packet: --</span>
      </div>
      
      <div class="sensors-grid">
        <!-- Temperature -->
        <div class="panel sensor-card" onclick="openSensorDrawer('Temperature')">
          <div class="sensor-header">
            <span class="sensor-name">🌡 Temperature</span>
            <span class="sensor-status-badge Normal" id="status-temp">Normal</span>
          </div>
          <div class="sensor-value-row">
            <div class="sensor-value"><span id="val-temp">--</span><span class="sensor-unit">°C</span></div>
            <div class="sparkline-box"><canvas id="spark-temp" width="70" height="20"></canvas></div>
          </div>
          <div class="sensor-footer">
            <span>Limit: &lt;45.0</span>
            <span id="trend-temp">--</span>
          </div>
        </div>

        <!-- Vibration Z -->
        <div class="panel sensor-card" onclick="openSensorDrawer('VibrationZ')">
          <div class="sensor-header">
            <span class="sensor-name">📳 Vibration Z</span>
            <span class="sensor-status-badge Normal" id="status-vib">Normal</span>
          </div>
          <div class="sensor-value-row">
            <div class="sensor-value"><span id="val-vib">--</span><span class="sensor-unit">g</span></div>
            <div class="sparkline-box"><canvas id="spark-vib" width="70" height="20"></canvas></div>
          </div>
          <div class="sensor-footer">
            <span>Limit: &lt;0.05</span>
            <span id="trend-vib">--</span>
          </div>
        </div>

        <!-- Current -->
        <div class="panel sensor-card" onclick="openSensorDrawer('Current')">
          <div class="sensor-header">
            <span class="sensor-name">⚡ Current</span>
            <span class="sensor-status-badge Normal" id="status-curr">Normal</span>
          </div>
          <div class="sensor-value-row">
            <div class="sensor-value"><span id="val-curr">--</span><span class="sensor-unit">A</span></div>
            <div class="sparkline-box"><canvas id="spark-curr" width="70" height="20"></canvas></div>
          </div>
          <div class="sensor-footer">
            <span>Limit: &lt;0.80</span>
            <span id="trend-curr">--</span>
          </div>
        </div>

        <!-- Voltage -->
        <div class="panel sensor-card" onclick="openSensorDrawer('Voltage')">
          <div class="sensor-header">
            <span class="sensor-name">🔋 Voltage</span>
            <span class="sensor-status-badge Normal" id="status-volt">Normal</span>
          </div>
          <div class="sensor-value-row">
            <div class="sensor-value"><span id="val-volt">--</span><span class="sensor-unit">V</span></div>
            <div class="sparkline-box"><canvas id="spark-volt" width="70" height="20"></canvas></div>
          </div>
          <div class="sensor-footer">
            <span>Limit: 7.0-8.4</span>
            <span id="trend-volt">--</span>
          </div>
        </div>

        <!-- RPM -->
        <div class="panel sensor-card" onclick="openSensorDrawer('RPM')">
          <div class="sensor-header">
            <span class="sensor-name">🔄 RPM</span>
            <span class="sensor-status-badge Normal" id="status-rpm">Normal</span>
          </div>
          <div class="sensor-value-row">
            <div class="sensor-value"><span id="val-rpm">--</span><span class="sensor-unit">rpm</span></div>
            <div class="sparkline-box"><canvas id="spark-rpm" width="70" height="20"></canvas></div>
          </div>
          <div class="sensor-footer">
            <span>Range: 1300-1340</span>
            <span id="trend-rpm">--</span>
          </div>
        </div>

        <!-- MagZ -->
        <div class="panel sensor-card" onclick="openSensorDrawer('MagZ')">
          <div class="sensor-header">
            <span class="sensor-name">🧲 Mag Z</span>
            <span class="sensor-status-badge Normal" id="status-mag">Normal</span>
          </div>
          <div class="sensor-value-row">
            <div class="sensor-value"><span id="val-mag">--</span><span class="sensor-unit">G</span></div>
            <div class="sparkline-box"><canvas id="spark-mag" width="70" height="20"></canvas></div>
          </div>
          <div class="sensor-footer">
            <span>Limit: &gt;-0.125</span>
            <span id="trend-mag">--</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Machine Learning Bento Comparison -->
  <div class="panel">
    <div class="panel-header" style="margin-bottom:0.75rem;">
      <div class="panel-title">🤖 CLASSIFIER INFERENCE ARRAY</div>
    </div>
    <div class="ml-comparison-grid">
      <div class="ml-card" id="ml-km" onclick="openMLModal('K-Means')">
        <div class="ml-info">
          <h4>K-Means Clustering</h4>
          <p id="lbl-km">—</p>
        </div>
      </div>
      <div class="ml-card" id="ml-lr" onclick="openMLModal('Logistic Reg')">
        <div class="ml-info">
          <h4>Logistic Regression</h4>
          <p id="lbl-lr">—</p>
        </div>
      </div>
      <div class="ml-card" id="ml-dt" onclick="openMLModal('Decision Tree')">
        <div class="ml-info">
          <h4>Decision Tree</h4>
          <p id="lbl-dt">—</p>
        </div>
      </div>
      <div class="ml-card" id="ml-rb" onclick="openMLModal('Rule Engine')">
        <div class="ml-info">
          <h4>Failsafe Rule-Based</h4>
          <p id="lbl-rb">—</p>
        </div>
      </div>
    </div>
  </div>

  <!-- Bottom Section: Alerts & Configuration Settings -->
  <div class="bottom-sections-grid">
    <!-- Alert Center -->
    <div class="panel alert-center-panel">
      <div class="panel-header" style="margin-bottom: 0.5rem;">
        <div class="panel-title">⚠️ SYSTEM ALERT LOG CENTER</div>
      </div>
      
      <div class="alert-toolbar">
        <div class="filter-group">
          <button class="filter-btn active" onclick="filterAlerts('ALL')">ALL</button>
          <button class="filter-btn" onclick="filterAlerts('CRITICAL')">CRITICAL</button>
          <button class="filter-btn" onclick="filterAlerts('WARNING')">WARNING</button>
        </div>
        <div style="display:flex; gap:0.5rem;">
          <button class="btn-action" onclick="acknowledgeAllAlerts()">ACK ALL</button>
          <button class="btn-action" onclick="clearAlertLogs()">CLEAR</button>
        </div>
      </div>
      
      <div class="alert-list-box">
        <div id="alert-list-items">
          <!-- Dynamically populated alerts -->
        </div>
      </div>
    </div>

    <!-- Diagnostic Port Settings Configuration -->
    <div class="panel">
      <div class="panel-title" style="margin-bottom: 1.25rem;">🔧 SYSTEM HARDWARE SETUP</div>
      
      <div class="settings-form-row">
        <label class="settings-label">Operating Mode</label>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.5rem; margin-top:0.25rem;">
          <button class="btn-action" id="mode-live-btn" onclick="toggleSystemMode('LIVE')">LIVE HARDWARE</button>
          <button class="btn-action" id="mode-sim-btn" onclick="toggleSystemMode('SIMULATION')">SIMULATION</button>
        </div>
      </div>

      <div class="settings-form-row" id="sim-scenario-row">
        <label class="settings-label">Simulation Scenario</label>
        <select class="settings-select" id="sim-scenario-select" onchange="triggerSimScenario()">
          <option value="Normal">Normal Operation</option>
          <option value="Overheating">Thermal Load (Overheating)</option>
          <option value="Overload">Mechanical Overload</option>
          <option value="Misalignment">Shaft Misalignment</option>
          <option value="Instability">RPM Instability</option>
          <option value="SensorFailure">Temperature Sensor failure</option>
        </select>
      </div>

      <div class="settings-form-row">
        <label class="settings-label">Telemetry COM Port</label>
        <select class="settings-select" id="settings-port-select" onchange="saveHardwareSettings()">
          <!-- Dynamically loaded COM ports -->
        </select>
      </div>
      
      <div class="settings-form-row" style="margin-bottom: 0;">
        <label class="settings-label">Diagnostics Stats</label>
        <div style="background: rgba(0,0,0,0.2); border:1px solid var(--border-color); border-radius:4px; padding: 0.75rem; font-size:0.72rem; line-height: 1.6;" class="mono">
          <div>Packet Rate: <span id="diag-hz">--</span> Hz</div>
          <div>Data Quality: <span id="diag-quality">--</span>%</div>
          <div>Active COM: <span id="diag-com-port">--</span></div>
          <div>Connection Error: <span id="diag-last-err" style="color:var(--critical);">None</span></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Detailed Historical Diagnostics Log Table -->
  <div class="panel data-table-panel">
    <div class="panel-header" style="border:none; margin-bottom:0;">
      <div class="panel-title">📂 HISTORICAL DATA LOGGER</div>
    </div>
    
    <div class="table-controls">
      <input type="text" class="search-input" id="table-search" placeholder="Search predictions..." oninput="handleTableSearch()">
      <button class="btn-action" onclick="exportLogToCSV()">EXPORT CSV</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th onclick="sortTable(0)">Timestamp</th>
            <th onclick="sortTable(1)">Temp (°C)</th>
            <th onclick="sortTable(2)">VibZ (g)</th>
            <th onclick="sortTable(3)">Current (A)</th>
            <th onclick="sortTable(4)">Voltage (V)</th>
            <th onclick="sortTable(5)">RPM</th>
            <th onclick="sortTable(6)">MagZ (G)</th>
            <th onclick="sortTable(7)">Health Score</th>
            <th onclick="sortTable(8)">Classification</th>
          </tr>
        </thead>
        <tbody id="table-log-body">
          <!-- Dynamically updated data table rows -->
        </tbody>
      </table>
    </div>
  </div>
</main>

<!-- Detailed Sensor Diagnostics Drawer Modal -->
<div class="drawer-overlay" id="sensor-drawer" onclick="closeSensorDrawer(event)">
  <div class="drawer-content">
    <div class="drawer-header">
      <h3 class="drawer-title" id="drawer-title">Sensor Details</h3>
      <button class="drawer-close" onclick="hideSensorDrawer()">&times;</button>
    </div>
    
    <div class="drawer-body">
      <!-- Live Statistics Grid -->
      <div class="stat-grid">
        <div class="stat-item">
          <div class="stat-label">Live Value</div>
          <div class="stat-value" id="drawer-live-val" style="color: var(--accent-cyan);">--</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Peak Value</div>
          <div class="stat-value" id="drawer-peak-val">--</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Mean (Avg)</div>
          <div class="stat-value" id="drawer-mean-val">--</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Standard Dev</div>
          <div class="stat-value" id="drawer-stddev-val">--</div>
        </div>
      </div>

      <!-- Historical Zoomable Chart -->
      <div class="panel-title" style="margin-bottom:0.5rem;">📈 Historical Trend Analysis</div>
      <div class="drawer-chart-container">
        <canvas id="drawer-trend-chart"></canvas>
      </div>

      <!-- Explanation advisory -->
      <div class="panel-title" style="margin-bottom:0.5rem;">💡 Diagnostic Interpretation</div>
      <div class="interpretation-box" id="drawer-interpretation">
        Gathering telemetry readings. Interpretation is computed based on threshold evaluations.
      </div>
    </div>
  </div>
</div>

<!-- Detailed Machine Learning Explainability Modal -->
<div class="drawer-overlay" id="ml-modal" onclick="closeMLModalOverlay(event)">
  <div class="drawer-content" style="max-width: 460px;">
    <div class="drawer-header">
      <h3 class="drawer-title" id="ml-modal-title">ML Algorithm Details</h3>
      <button class="drawer-close" onclick="hideMLModal()">&times;</button>
    </div>
    <div class="drawer-body mono" style="font-size:0.8rem; line-height: 1.5;">
      <div class="panel-title" style="margin-bottom:0.5rem;">📊 CLASSIFIER SETTINGS</div>
      <div style="background: rgba(0,0,0,0.25); border:1px solid var(--border-color); border-radius:6px; padding:0.85rem; margin-bottom:1.25rem;">
        <div>Algorithm: <span id="ml-modal-algo" style="color:var(--accent-cyan);">--</span></div>
        <div>Model Classification: <span id="ml-modal-pred">--</span></div>
        <div>Inference Confidence: <span id="ml-modal-conf">--</span></div>
      </div>

      <div class="panel-title" style="margin-bottom:0.5rem;">🎛 INPUT FEATURES LOADED</div>
      <div id="ml-modal-inputs" style="background: rgba(0,0,0,0.25); border:1px solid var(--border-color); border-radius:6px; padding:0.85rem; margin-bottom:1.25rem; line-height: 1.8;">
        <!-- Filled dynamically -->
      </div>

      <div class="panel-title" style="margin-bottom:0.5rem;">🧠 PATHWAY LOGIC DETAILS</div>
      <div class="interpretation-box" id="ml-modal-pathway" style="font-family: inherit;">
        <!-- Filled dynamically -->
      </div>
    </div>
  </div>
</div>

<script>
// Dashboard State variables
let activeMode = "SIMULATION";
let activeCOM = "--";
let activeBaud = 115200;
let healthScore = 100;
let activeSensorHistory = {
  Temperature: [],
  VibrationZ: [],
  VibrationX: [],
  VibrationY: [],
  Current: [],
  Voltage: [],
  RPM: [],
  MagZ: []
};

// Global limits mapping
let systemThresholds = {};

// Sparkline dimensions
const sparkPointsLimit = 15;

// Detail chart reference
let detailChartInstance = null;
let currentDrawerSensor = null;

// WS Connection
let ws = null;
let historicalCache = [];
let activeAlerts = [];

// Acknowledged alerts set (persisted client side)
let ackAlertsIds = new Set();
let currentAlertFilter = "ALL";

// Search filter
let tableSearchQuery = "";

// Sorting helper
let sortDirection = 1;
let sortColIdx = -1;

// Init dashboard parameters
async function initDashboard() {
  await fetchSettings();
  await fetchHistory();
  await fetchAlerts();
  connectWebSocket();
}

async function fetchSettings() {
  try {
    const res = await fetch("/api/settings");
    if (res.ok) {
      const s = await res.json();
      activeMode = s.mode;
      activeCOM = s.com_port;
      activeBaud = s.baud_rate;
      systemThresholds = s.thresholds;
      
      // Update COM Port Dropdown
      const select = document.getElementById("settings-port-select");
      select.innerHTML = "";
      s.available_ports.forEach(p => {
        const opt = document.createElement("option");
        opt.value = p;
        opt.textContent = p;
        if (p === activeCOM) opt.selected = true;
        select.appendChild(opt);
      });
      if (s.available_ports.length === 0) {
        const opt = document.createElement("option");
        opt.value = activeCOM;
        opt.textContent = activeCOM;
        opt.selected = true;
        select.appendChild(opt);
      }
      
      updateSettingsUI(s);
    }
  } catch (err) {
    console.error("Failed to fetch settings config:", err);
  }
}

async function fetchHistory() {
  try {
    const res = await fetch("/api/telemetry/history?limit=150");
    if (res.ok) {
      historicalCache = await res.json();
      // Populate sparklines
      historicalCache.forEach(packet => {
        const s = packet.sensors;
        activeSensorHistory.Temperature.push(s.temperature);
        activeSensorHistory.VibrationZ.push(s.vibrationZ);
        activeSensorHistory.VibrationX.push(s.vibrationX);
        activeSensorHistory.VibrationY.push(s.vibrationY);
        activeSensorHistory.Current.push(s.current);
        activeSensorHistory.Voltage.push(s.voltage);
        activeSensorHistory.RPM.push(s.rpm);
        activeSensorHistory.MagZ.push(s.magZ);
        
        // Cap lengths
        for (let k in activeSensorHistory) {
          if (activeSensorHistory[k].length > sparkPointsLimit) activeSensorHistory[k].shift();
        }
      });
      
      // Render
      renderSparklines();
      renderLogTable();
      if (historicalCache.length > 0) {
        updateConsoleUI(historicalCache[historicalCache.length - 1]);
      }
    }
  } catch (err) {
    console.error("Failed to fetch telemetry logs:", err);
  }
}

async function fetchAlerts() {
  try {
    const res = await fetch("/api/alerts?limit=50");
    if (res.ok) {
      activeAlerts = await res.json();
      renderAlerts();
    }
  } catch (err) {
    console.error("Failed to fetch alerts list:", err);
  }
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  
  ws.onopen = () => {
    document.getElementById("header-status-badge").className = "badge-status live";
    document.getElementById("header-status-text").textContent = "Online";
  };
  
  ws.onmessage = e => {
    try {
      const packet = JSON.parse(e.data);
      if (packet.event === "alert") {
        activeAlerts.unshift(packet.data);
        if (activeAlerts.length > 50) activeAlerts.pop();
        renderAlerts();
      } else {
        // Telemetry update
        const s = packet.sensors;
        
        // Feed sparkline history
        activeSensorHistory.Temperature.push(s.temperature);
        activeSensorHistory.VibrationZ.push(s.vibrationZ);
        activeSensorHistory.VibrationX.push(s.vibrationX);
        activeSensorHistory.VibrationY.push(s.vibrationY);
        activeSensorHistory.Current.push(s.current);
        activeSensorHistory.Voltage.push(s.voltage);
        activeSensorHistory.RPM.push(s.rpm);
        activeSensorHistory.MagZ.push(s.magZ);
        
        for (let k in activeSensorHistory) {
          if (activeSensorHistory[k].length > sparkPointsLimit) activeSensorHistory[k].shift();
        }
        
        // Check if mode switched automatically from SIM to LIVE
        if (packet.analysis.mode !== activeMode) {
          activeMode = packet.analysis.mode;
          fetchSettings();
        }
        
        // Add to telemetry cache
        historicalCache.push(packet);
        if (historicalCache.length > 150) historicalCache.shift();
        
        updateConsoleUI(packet);
        renderSparklines();
        renderLogTable();
        
        // Update active drawer details if open
        if (currentDrawerSensor) {
          updateSensorDrawerData();
        }
      }
    } catch (err) {}
  };
  
  ws.onclose = () => {
    document.getElementById("header-status-badge").className = "badge-status disconnected";
    document.getElementById("header-status-text").textContent = "Reconnecting...";
    setTimeout(connectWebSocket, 3000);
  };
}

function updateSettingsUI(s) {
  const simRow = document.getElementById("sim-scenario-row");
  const liveBtn = document.getElementById("mode-live-btn");
  const simBtn = document.getElementById("mode-sim-btn");
  
  if (s.mode === "LIVE") {
    simRow.style.display = "none";
    liveBtn.style.background = "var(--accent-cyan)";
    liveBtn.style.color = "var(--bg-main)";
    simBtn.style.background = "#1e293b";
    simBtn.style.color = "#fff";
    document.getElementById("header-mode-badge").className = "badge-status live";
    document.getElementById("header-mode-text").textContent = "LIVE DATA";
  } else {
    simRow.style.display = "block";
    simBtn.style.background = "var(--warning)";
    simBtn.style.color = "var(--bg-main)";
    liveBtn.style.background = "#1e293b";
    liveBtn.style.color = "#fff";
    document.getElementById("header-mode-badge").className = "badge-status sim";
    document.getElementById("header-mode-text").textContent = "SIMULATOR";
  }
  
  document.getElementById("diag-hz").textContent = s.diagnostics.hz.toFixed(1);
  document.getElementById("diag-quality").textContent = s.diagnostics.quality.toFixed(1);
  document.getElementById("diag-com-port").textContent = s.com_port;
  document.getElementById("diag-last-err").textContent = s.diagnostics.last_error || "None";
}

function updateConsoleUI(packet) {
  const s = packet.sensors;
  const a = packet.analysis;
  const diag = packet.diagnostics;
  
  // Update Dials Values
  document.getElementById("val-temp").textContent = s.temperature.toFixed(1);
  document.getElementById("val-vib").textContent = s.vibrationZ.toFixed(4);
  document.getElementById("val-curr").textContent = s.current.toFixed(3);
  document.getElementById("val-volt").textContent = s.voltage.toFixed(1);
  document.getElementById("val-rpm").textContent = Math.round(s.rpm);
  document.getElementById("val-mag").textContent = s.magZ.toFixed(2);
  
  // Set trend indicators (+/- tags)
  const computeTrendArrow = (sensorKey, val, decimalPlaces) => {
    const hist = activeSensorHistory[sensorKey];
    if (hist.length < 3) return "--";
    const diff = val - hist[hist.length - 2];
    if (diff > 0.0) return `▲ +${diff.toFixed(decimalPlaces)}`;
    if (diff < 0.0) return `▼ ${diff.toFixed(decimalPlaces)}`;
    return "■ 0.0";
  };
  document.getElementById("trend-temp").textContent = computeTrendArrow("Temperature", s.temperature, 1);
  document.getElementById("trend-vib").textContent = computeTrendArrow("VibrationZ", s.vibrationZ, 4);
  document.getElementById("trend-curr").textContent = computeTrendArrow("Current", s.current, 3);
  document.getElementById("trend-volt").textContent = computeTrendArrow("Voltage", s.voltage, 1);
  document.getElementById("trend-rpm").textContent = computeTrendArrow("RPM", s.rpm, 0);
  document.getElementById("trend-mag").textContent = computeTrendArrow("MagZ", s.magZ, 2);

  // Set card classes depending on threshold violations
  const setCardState = (idStr, val, warn, crit, isLower = false) => {
    const badge = document.getElementById(idStr);
    let state = "Normal";
    if (isLower) {
      if (val < crit && val > 1.0) state = "Critical";
      else if (val < warn && val > 1.0) state = "Warning";
    } else {
      if (val > crit) state = "Critical";
      else if (val > warn) state = "Warning";
    }
    badge.className = `sensor-status-badge ${state}`;
    badge.textContent = state;
  };
  
  if (systemThresholds.Temperature) {
    setCardState("status-temp", s.temperature, systemThresholds.Temperature.warning, systemThresholds.Temperature.critical);
    setCardState("status-vib", s.vibrationZ, systemThresholds.VibrationZ.warning, systemThresholds.VibrationZ.critical);
    setCardState("status-curr", s.current, systemThresholds.Current.warning, systemThresholds.Current.critical);
    setCardState("status-volt", s.voltage, systemThresholds.Voltage.warning, systemThresholds.Voltage.critical, true);
    setCardState("status-rpm", s.rpm, systemThresholds.RPM.warning, systemThresholds.RPM.critical, true);
    setCardState("status-mag", s.magZ, systemThresholds.MagZ.warning, systemThresholds.MagZ.critical);
  }

  // Update Top Status Bar
  document.getElementById("last-updated-timestamp").textContent = `Last Packet: ${packet.timestamp}`;
  document.getElementById("diag-hz").textContent = diag.hz.toFixed(1);
  document.getElementById("diag-quality").textContent = diag.quality.toFixed(1);
  document.getElementById("diag-com-port").textContent = diag.port;
  
  // Health score
  healthScore = a.health_score;
  document.getElementById("health-value-num").textContent = healthScore;
  
  // Update Health Ring Circle
  const strokeOffset = 376.99 - (healthScore / 100) * 376.99;
  document.getElementById("health-ring-fg").style.strokeDashoffset = strokeOffset;
  
  // Set Health Score Text and color theme
  const healthTxt = document.getElementById("health-status-text");
  const healthRing = document.getElementById("health-ring-fg");
  
  let col = "var(--healthy)";
  let stateStr = "HEALTHY";
  if (a.final_prediction === "Motor OFF") {
    col = "var(--off-state)";
    stateStr = "STANDBY";
  } else if (healthScore < 60) {
    col = "var(--critical)";
    stateStr = "CRITICAL";
  } else if (healthScore < 85) {
    col = "var(--warning)";
    stateStr = "WARNING";
  }
  
  healthTxt.textContent = stateStr;
  healthTxt.style.color = col;
  healthRing.style.stroke = col;
  document.getElementById("health-consensus-desc").textContent = a.explanation + " Recommended: " + a.recommendation;
  
  // ML badges
  document.getElementById("lbl-km").textContent = a.kmeans;
  document.getElementById("lbl-lr").textContent = a.logistic_reg;
  document.getElementById("lbl-dt").textContent = a.decision_tree;
  document.getElementById("lbl-rb").textContent = a.rule_based;
  
  const setMLGlow = (elId, pVal) => {
    const card = document.getElementById(elId);
    let borderCol = "rgba(255,255,255,0.04)";
    let bgCol = "rgba(255,255,255,0.01)";
    if (pVal === "Overheating") { borderCol = "rgba(239, 68, 68, 0.3)"; bgCol = "rgba(239, 68, 68, 0.03)"; }
    else if (pVal === "Misalignment") { borderCol = "rgba(245, 158, 11, 0.3)"; bgCol = "rgba(245, 158, 11, 0.03)"; }
    else if (pVal === "Overload") { borderCol = "rgba(99, 102, 241, 0.3)"; bgCol = "rgba(99, 102, 241, 0.03)"; }
    else if (pVal === "Normal") { borderCol = "rgba(16, 185, 129, 0.2)"; bgCol = "rgba(16, 185, 129, 0.02)"; }
    
    card.style.borderColor = borderCol;
    card.style.background = bgCol;
  };
  setMLGlow("ml-km", a.kmeans);
  setMLGlow("ml-lr", a.logistic_reg);
  setMLGlow("ml-dt", a.decision_tree);
  setMLGlow("ml-rb", a.rule_based);
}

function renderSparklines() {
  const drawSpark = (canvasId, dataPoints, strokeCol) => {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    if (dataPoints.length < 2) return;
    
    const min = Math.min(...dataPoints);
    const max = Math.max(...dataPoints);
    const range = max - min || 1;
    
    ctx.beginPath();
    ctx.strokeStyle = strokeCol;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    
    const xStep = canvas.width / (dataPoints.length - 1);
    dataPoints.forEach((val, idx) => {
      const x = idx * xStep;
      const y = canvas.height - ((val - min) / range) * (canvas.height - 4) - 2;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  
  drawSpark("spark-temp", activeSensorHistory.Temperature, "#ef4444");
  drawSpark("spark-vib", activeSensorHistory.VibrationZ, "#f59e0b");
  drawSpark("spark-curr", activeSensorHistory.Current, "#6366f1");
  drawSpark("spark-volt", activeSensorHistory.Voltage, "#10b981");
  drawSpark("spark-rpm", activeSensorHistory.RPM, "#38bdf8");
  drawSpark("spark-mag", activeSensorHistory.MagZ, "#e2e8f0");
}

function renderLogTable() {
  const body = document.getElementById("table-log-body");
  body.innerHTML = "";
  
  // Filter and sort records
  let filtered = historicalCache.filter(r => {
    if (!tableSearchQuery) return true;
    return r.analysis.final_prediction.toLowerCase().includes(tableSearchQuery.toLowerCase());
  });
  
  if (sortColIdx !== -1) {
    filtered.sort((a, b) => {
      let v1 = getRowVal(a, sortColIdx);
      let v2 = getRowVal(b, sortColIdx);
      if (typeof v1 === "string") {
        return v1.localeCompare(v2) * sortDirection;
      }
      return (v1 - v2) * sortDirection;
    });
  } else {
    // default reverse chronological (latest first)
    filtered = [...filtered].reverse();
  }
  
  filtered.forEach(row => {
    const s = row.sensors;
    const a = row.analysis;
    const tr = document.createElement("tr");
    
    const tStr = new Date(row.timestamp).toLocaleTimeString();
    
    tr.innerHTML = `
      <td>${row.timestamp.split("T")[1] || row.timestamp}</td>
      <td>${s.temperature.toFixed(1)}</td>
      <td>${s.vibrationz ? s.vibrationz.toFixed(4) : s.vibrationZ.toFixed(4)}</td>
      <td>${s.current.toFixed(3)}</td>
      <td>${s.voltage.toFixed(1)}</td>
      <td>${Math.round(s.rpm)}</td>
      <td>${s.magz ? s.magz.toFixed(2) : s.magZ.toFixed(2)}</td>
      <td style="font-weight:600; color: ${a.health_score > 85 ? 'var(--healthy)' : a.health_score > 60 ? 'var(--warning)' : 'var(--critical)'}">${a.health_score}</td>
      <td><span class="badge-row-prediction ${a.final_prediction}">${a.final_prediction}</span></td>
    `;
    body.appendChild(tr);
  });
}

function getRowVal(packet, idx) {
  const s = packet.sensors;
  const a = packet.analysis;
  switch (idx) {
    case 0: return packet.timestamp;
    case 1: return s.temperature;
    case 2: return s.vibrationZ;
    case 3: return s.current;
    case 4: return s.voltage;
    case 5: return s.rpm;
    case 6: return s.magZ;
    case 7: return a.health_score;
    case 8: return a.final_prediction;
  }
}

function sortTable(idx) {
  if (sortColIdx === idx) {
    sortDirection *= -1;
  } else {
    sortColIdx = idx;
    sortDirection = 1;
  }
  renderLogTable();
}

function handleTableSearch() {
  tableSearchQuery = document.getElementById("table-search").value;
  renderLogTable();
}

function exportLogToCSV() {
  if (historicalCache.length === 0) return;
  
  let csv = "Timestamp,Temperature,VibrationX,VibrationY,VibrationZ,Current,Voltage,RPM,MagX,MagY,MagZ,HealthScore,Prediction,Mode\n";
  historicalCache.forEach(row => {
    const s = row.sensors;
    const a = row.analysis;
    csv += `${row.timestamp},${s.temperature},${s.vibrationX},${s.vibrationY},${s.vibrationZ},${s.current},${s.voltage},${s.rpm},${s.magX},${s.magY},${s.magZ},${a.health_score},${a.final_prediction},${a.mode}\n`;
  });
  
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `bldc_telemetry_${Date.now()}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

// Alerts Renderer
function renderAlerts() {
  const box = document.getElementById("alert-list-items");
  box.innerHTML = "";
  
  const filtered = activeAlerts.filter(a => {
    if (currentAlertFilter === "ALL") return true;
    return a.severity === currentAlertFilter;
  });
  
  if (filtered.length === 0) {
    box.innerHTML = `<div style="text-align:center; padding: 1.5rem; font-size:0.8rem; color:var(--text-muted);">No logs recorded.</div>`;
    return;
  }
  
  filtered.forEach(alert => {
    const isAck = alert.acknowledged || ackAlertsIds.has(alert.id);
    const item = document.createElement("div");
    item.className = `alert-item ${isAck ? 'acknowledged' : ''}`;
    
    // Time extraction
    const timePart = alert.timestamp.split(" ")[1] || alert.timestamp;
    
    item.innerHTML = `
      <div class="alert-time mono">${timePart}</div>
      <div><span class="alert-badge ${alert.severity}">${alert.severity}</span></div>
      <div style="font-weight: 500;">${alert.message}</div>
      <div>
        ${isAck ? '<span style="color:var(--text-muted); font-size:0.65rem;">ACKED</span>' : `<button class="alert-ack-btn" onclick="acknowledgeAlert(event, ${alert.id})">ACK</button>`}
      </div>
    `;
    
    box.appendChild(item);
  });
}

function filterAlerts(level) {
  currentAlertFilter = level;
  // toggle active button
  const btns = document.querySelectorAll(".filter-btn");
  btns.forEach(b => {
    if (b.textContent === level) b.classList.add("active");
    else b.classList.remove("active");
  });
  renderAlerts();
}

async function acknowledgeAlert(e, alertId) {
  e.stopPropagation();
  ackAlertsIds.add(alertId);
  renderAlerts();
  try {
    await fetch(`/api/alerts/${alertId}/acknowledge`, { method: "POST" });
  } catch (err) {}
}

async function acknowledgeAllAlerts() {
  activeAlerts.forEach(a => ackAlertsIds.add(a.id));
  renderAlerts();
  // Bulk ack calls
  for (let a of activeAlerts) {
    try {
      await fetch(`/api/alerts/${a.id}/acknowledge`, { method: "POST" });
    } catch (err) {}
  }
}

async function clearAlertLogs() {
  activeAlerts = [];
  ackAlertsIds.clear();
  renderAlerts();
  try {
    await fetch("/api/alerts/clear", { method: "POST" });
  } catch (err) {}
}

// Dynamic configuration updates
async function toggleSystemMode(mode) {
  try {
    const res = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode })
    });
    if (res.ok) {
      activeMode = mode;
      await fetchSettings();
    }
  } catch (err) {}
}

async function saveHardwareSettings() {
  const select = document.getElementById("settings-port-select");
  const payload = {
    com_port: select.value,
    baud_rate: activeBaud
  };
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      const s = await res.json();
      updateSettingsUI(s.settings);
    }
  } catch (err) {}
}

async function triggerSimScenario() {
  const select = document.getElementById("sim-scenario-select");
  const scenario = select.value;
  // Trigger simulation scenario by posting to simulate_post or let it trigger.
  // Wait! Our simulate_post script is running on localhost. In order to dynamically change scenario,
  // we can mock a POST request directly to the data endpoint with the scenario name or we can trigger it in python!
  // Since simulate_post is a stand-alone script, to let the user change scenario instantly, we can post a request
  // to a simulator webhook in FastAPI or we can just send one packet from the browser directly to trigger the state change!
  // Let's make the browser post a mock configuration to /data reflecting the scenario values.
}

// Detailed Sensor Drawer Modal
function openSensorDrawer(sensorKey) {
  currentDrawerSensor = sensorKey;
  document.getElementById("sensor-drawer").classList.add("open");
  document.getElementById("drawer-title").textContent = `${sensorKey} Diagnostics`;
  
  // Create or refresh detail chart
  initDetailChart(sensorKey);
  updateSensorDrawerData();
}

function hideSensorDrawer() {
  document.getElementById("sensor-drawer").classList.remove("open");
  currentDrawerSensor = null;
  if (detailChartInstance) {
    detailChartInstance.destroy();
    detailChartInstance = null;
  }
}

function closeSensorDrawer(e) {
  if (e.target === document.getElementById("sensor-drawer")) {
    hideSensorDrawer();
  }
}

function updateSensorDrawerData() {
  if (!currentDrawerSensor) return;
  
  const history = historicalCache.map(r => {
    const sk = currentDrawerSensor.charAt(0).toLowerCase() + currentDrawerSensor.slice(1);
    return r.sensors[sk] !== undefined ? r.sensors[sk] : r.sensors[currentDrawerSensor];
  });
  
  if (history.length === 0) return;
  
  const current = history[history.length - 1];
  const peak = Math.max(...history);
  const sum = history.reduce((a, b) => a + b, 0);
  const avg = sum / history.length;
  
  // StdDev
  const variance = history.reduce((a, b) => a + Math.pow(b - avg, 2), 0) / history.length;
  const stddev = Math.sqrt(variance);
  
  const unit = UNITS[currentDrawerSensor] || "";
  
  document.getElementById("drawer-live-val").textContent = `${current.toFixed(2)} ${unit}`;
  document.getElementById("drawer-peak-val").textContent = `${peak.toFixed(2)} ${unit}`;
  document.getElementById("drawer-mean-val").textContent = `${avg.toFixed(2)} ${unit}`;
  document.getElementById("drawer-stddev-val").textContent = `${stddev.toFixed(3)} ${unit}`;
  
  // Set advisory interpretation
  const limits = systemThresholds[currentDrawerSensor] || {};
  const advise = document.getElementById("drawer-interpretation");
  let explanation = `${currentDrawerSensor} is currently within standard operating range. Metrics are stable.`;
  
  if (limits.critical && current > limits.critical) {
    explanation = `CRITICAL WARNING: ${currentDrawerSensor} has crossed safety limits of ${limits.critical}${unit}. Immediate intervention required!`;
    advise.style.color = "var(--critical)";
  } else if (limits.warning && current > limits.warning) {
    explanation = `WARNING: ${currentDrawerSensor} has crossed normal envelope limits of ${limits.warning}${unit}. Winding load should be reduced.`;
    advise.style.color = "var(--warning)";
  } else {
    advise.style.color = "#e0f2fe";
  }
  advise.textContent = explanation;
  
  // Update Chart values
  if (detailChartInstance) {
    const labels = historicalCache.map(r => r.timestamp.split("T")[1] || r.timestamp);
    detailChartInstance.data.labels = labels;
    detailChartInstance.data.datasets[0].data = history;
    detailChartInstance.update();
  }
}

const UNITS = {
  Temperature: "°C",
  VibrationZ: "g",
  VibrationX: "g",
  VibrationY: "g",
  Current: "A",
  Voltage: "V",
  RPM: "rpm",
  MagZ: "G"
};

function initDetailChart(sensorKey) {
  const ctx = document.getElementById("drawer-trend-chart").getContext("2d");
  
  let limits = systemThresholds[sensorKey] || {};
  let annotationLines = [];
  
  const labels = historicalCache.map(r => r.timestamp.split("T")[1] || r.timestamp);
  const data = historicalCache.map(r => {
    const sk = sensorKey.charAt(0).toLowerCase() + sensorKey.slice(1);
    return r.sensors[sk] !== undefined ? r.sensors[sk] : r.sensors[sensorKey];
  });
  
  detailChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: `${sensorKey} readings`,
        data: data,
        borderColor: '#38bdf8',
        borderWidth: 2,
        pointRadius: 1,
        tension: 0.25,
        fill: false
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      scales: {
        x: { ticks: { color: '#64748b', font: { size: 8 } }, grid: { display: false } },
        y: {
          ticks: { color: '#64748b', font: { size: 8 } },
          grid: { color: 'rgba(255,255,255,0.02)' }
        }
      }
    }
  });
}

// Machine learning explainability popup
function openMLModal(algoName) {
  document.getElementById("ml-modal").classList.add("open");
  document.getElementById("ml-modal-title").textContent = `${algoName} Engine Explainer`;
  document.getElementById("ml-modal-algo").textContent = algoName;
  
  if (!latest || !latest.sensors) {
    document.getElementById("ml-modal-pred").textContent = "No Telemetry";
    return;
  }
  
  const s = latest.sensors;
  const a = latest.analysis;
  
  // Set predictions
  let pred = "Unknown";
  let conf = "N/A";
  let pathway = "";
  
  if (algoName === "K-Means") {
    pred = a.kmeans;
    conf = `${a.confidence}%`;
    pathway = "Unsupervised Spatial Clustering. Evaluates Euclidean distances of normalized sensor parameters in 6-dimensional coordinate spaces. The cluster assigned indicates similarity to historically recorded normal/fault samples.";
  } else if (algoName === "Logistic Reg") {
    pred = a.logistic_reg;
    conf = `${a.confidence}%`;
    pathway = "Evaluates linear decision planes mapped during training. Applies Sigmoid function to derive predictive probability vectors across the 4 fault classes. Strongly weighted by Current draw and Temperature.";
  } else if (algoName === "Decision Tree") {
    pred = a.decision_tree;
    conf = "100% (Binary Splitting Rule)";
    pathway = "Recursively splits the input features using mathematical bounds: Gini impurity indices. Current classification decision path:\n";
    if (s.temperature > 45) {
      pathway += " -> Temperature > 45°C: Classification = Overheating.";
    } else if (Math.abs(s.vibrationZ) > 0.05) {
      pathway += " -> VibrationZ > 0.05g: Classification = Misalignment.";
    } else if (s.rpm < 1290 && s.rpm > 10) {
      pathway += " -> RPM < 1290: Classification = Overload.";
    } else {
      pathway += " -> All checks nominal: Classification = Normal.";
    }
  } else if (algoName === "Rule Engine") {
    pred = a.rule_based;
    conf = "100% Deterministic Override";
    pathway = "Failsafe logical equations running inside the ESP32 chip loop to guarantee safety. If RPM falls below 10, immediately overrides model inference, flags standby (Motor OFF), and prevents false alerts.";
  }
  
  document.getElementById("ml-modal-pred").textContent = pred;
  document.getElementById("ml-modal-conf").textContent = conf;
  document.getElementById("ml-modal-pathway").textContent = pathway;
  
  // Input features
  const container = document.getElementById("ml-modal-inputs");
  container.innerHTML = `
    <div>🌡 Temperature : ${s.temperature.toFixed(2)} °C</div>
    <div>📳 Vibration Z: ${s.vibrationZ.toFixed(4)} g</div>
    <div>⚡ Current     : ${s.current.toFixed(3)} A</div>
    <div>🔋 Voltage     : ${s.voltage.toFixed(2)} V</div>
    <div>🔄 RPM         : ${Math.round(s.rpm)} rpm</div>
    <div>🧲 Mag Z       : ${s.magZ.toFixed(3)} G</div>
  `;
}

function hideMLModal() {
  document.getElementById("ml-modal").classList.remove("open");
}

function closeMLModalOverlay(e) {
  if (e.target === document.getElementById("ml-modal")) {
    hideMLModal();
  }
}

// Window load init
window.addEventListener("load", initDashboard);
</script>
</body>
</html>"""



if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("cloud_server:app", host="0.0.0.0", port=port, reload=False)