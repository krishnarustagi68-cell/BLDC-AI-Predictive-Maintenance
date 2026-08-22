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
from typing import Optional
from collections import deque

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from contextlib import asynccontextmanager

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
            lr_p = cls.le.inverse_transform(cls.lr.predict(xs))[0]
            dt_p = cls.le.inverse_transform(cls.dt.predict(x))[0]
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
    
    # If your old code had 'if rpm < 1500' here, 
    # it was catching the 0 RPM before. 
    # Now that 'Motor OFF' is at the top, this is safe.
    if rpm < 1285:              return "Overload"
    
    return "Normal"


# ── Trend prediction ─────────────────────────────────────────
def predict_trend(history: list, current: dict) -> str:
    if len(history) < 5: return "Insufficient data"
    try:
        temps = [r["sensors"]["temperature"] for r in history[-10:]]
        rpms  = [r["sensors"]["rpm"]         for r in history[-10:]]
        avg_t = sum(temps) / len(temps)
        avg_r = sum(rpms)  / len(rpms)
        if current["Temperature"] > avg_t * 1.15 and current["RPM"] < avg_r * 0.85:
            return "At Risk"
        if current["Temperature"] > avg_t * 1.15 or current["RPM"] < avg_r * 0.85:
            return "Watch"
        return "Stable"
    except: return "Unknown"


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    MLEngine.load()
    yield

app = FastAPI(title="BLDC Motor Monitor", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
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
    global latest
    d = payload.dict()

    preds  = MLEngine.predict(d)
    rule   = rule_detect(d)
    
    # Priority: Rule-based ensures website matches OLED exactly
    final  = rule if rule == "Motor OFF" else preds.get("decision_tree", rule)
    trend  = predict_trend(list(history), d)

    record = {
        "id":        len(history) + 1,
        "timestamp": datetime.utcnow().isoformat(),
        "device_id": d["device_id"],
        "sensors": {
            "temperature": d["Temperature"],
            "vibrationX":  d["VibrationX"],
            "vibrationY":  d["VibrationY"],
            "vibrationZ":  d["VibrationZ"],
            "current":     d["Current"],
            "voltage":     d.get("Voltage", 0),
            "rpm":         d["RPM"],
            "magX":        d.get("MagX", 0),
            "magY":        d.get("MagY", 0),
            "magZ":        d.get("MagZ", 0),
        },
        "analysis": {
            "rule_based":       rule,
            "kmeans":           preds["kmeans"],
            "logistic_reg":     preds["logistic_reg"],
            "decision_tree":    preds["decision_tree"],
            "final_prediction": final,
            "future_risk":      trend,
        },
        "fault": final not in ["Normal", "Motor OFF"],
    }

    history.append(record)
    latest = record
    await wsm.broadcast(record)
    return record

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await wsm.connect(websocket)
    try:
        # Keep the connection alive to receive streaming broadcasts
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
            "sensors": {"temperature": 0, "vibrationZ": 0, "current": 0, "rpm": 0},
            "analysis": {"final_prediction": "Waiting for Device...", "future_risk": "Stable"}
        }
    return latest


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


# ── Dashboard HTML ────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BLDC Motor Health Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0F172A;--card:#1E293B;--border:#334155;--accent:#38BDF8;
      --green:#10B981;--red:#EF4444;--amber:#F59E0B;--purple:#8B5CF6;
      --gray:#64748B;--text:#F1F5F9;--muted:#94A3B8}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif}
header{background:var(--card);border-bottom:1px solid var(--border);
       padding:.75rem 1.5rem;display:flex;justify-content:space-between;align-items:center}
header h1{font-size:1rem;color:var(--accent);font-weight:600}
#ws{font-size:.72rem;padding:3px 10px;border-radius:20px}
.on{background:#065f46;color:#6ee7b7}.off{background:#7f1d1d;color:#fca5a5}
main{padding:1rem 1.5rem;max-width:1400px;margin:auto}
#banner{text-align:center;font-size:1.6rem;font-weight:700;padding:.85rem;
        border-radius:10px;margin-bottom:.9rem;background:var(--card);
        border:2px solid var(--border);transition:all .4s}
.sg{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.75rem;margin-bottom:.9rem}
.sc{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.75rem;text-align:center}
.sc .l{font-size:.68rem;color:var(--muted);margin-bottom:4px}
.sc .v{font-size:1.55rem;font-weight:700}
.sc .u{font-size:.68rem;color:var(--muted)}
.pg{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem;margin-bottom:.9rem}
.pc{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.75rem}
.pc .m{font-size:.68rem;color:var(--muted);margin-bottom:3px}
.pc .p{font-size:.95rem;font-weight:600}
.cg{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:.75rem;margin-bottom:.9rem}
.cc{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.75rem}
.cc h3{font-size:.72rem;color:var(--muted);margin-bottom:.35rem}
.hw{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.75rem;overflow-x:auto}
.hw h3{font-size:.72rem;color:var(--muted);margin-bottom:.5rem}
table{width:100%;border-collapse:collapse;font-size:.72rem}
th{color:var(--muted);text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)}
td{padding:3px 8px;border-bottom:1px solid var(--border)}
.badge{display:inline-block;padding:2px 7px;border-radius:20px;font-size:.68rem;font-weight:600}
.Normal{background:#065f46;color:#6ee7b7}
.Overheating{background:#7f1d1d;color:#fca5a5}
.Misalignment{background:#78350f;color:#fde68a}
.Overload{background:#4c1d95;color:#ddd6fe}
.Motor.OFF{background:#334155;color:#94A3B8} /* Added Gray for OFF */
footer{text-align:center;color:var(--muted);font-size:.68rem;padding:.75rem;margin-top:1rem}
</style>
</head>
<body>
<header>
  <h1>⚙ BLDC Motor Health Monitor — Live Dashboard</h1>
  <span id="ws" class="off">● Connecting...</span>
</header>
<main>
  <div id="banner" style="color:var(--muted)">● Waiting for ESP32 data...</div>
  <div class="sg">
    <div class="sc"><div class="l">🌡 Temperature</div><div class="v" id="vt" style="color:#EF4444">—</div><div class="u">°C</div></div>
    <div class="sc"><div class="l">📳 Vibration Z</div><div class="v" id="vz" style="color:#F59E0B">—</div><div class="u">g</div></div>
    <div class="sc"><div class="l">⚡ Current</div><div class="v" id="vi" style="color:#8B5CF6">—</div><div class="u">A</div></div>
    <div class="sc"><div class="l">🔋 Voltage</div><div class="v" id="vv" style="color:#34D399">—</div><div class="u">V</div></div>
    <div class="sc"><div class="l">🔄 RPM</div><div class="v" id="vr" style="color:#38BDF8">—</div><div class="u">rpm</div></div>
    <div class="sc"><div class="l">🧲 MagZ</div><div class="v" id="vmz" style="color:#F472B6">—</div><div class="u">G</div></div>
    <div class="sc"><div class="l">🔮 Future risk</div><div class="v" id="vf" style="font-size:1rem;color:#10B981">—</div><div class="u">trend</div></div>
    <div class="sc"><div class="l">📡 Device</div><div class="v" id="vd" style="font-size:.8rem;color:#94A3B8">—</div><div class="u">id</div></div>
  </div>
  <div class="pg">
    <div class="pc"><div class="m">K-Means</div><div class="p" id="pkm">—</div></div>
    <div class="pc"><div class="m">Logistic Regression</div><div class="p" id="plr">—</div></div>
    <div class="pc"><div class="m">Decision Tree</div><div class="p" id="pdt">—</div></div>
    <div class="pc"><div class="m">Rule-based</div><div class="p" id="prb">—</div></div>
  </div>
  <div class="cg">
    <div class="cc"><h3>Temperature trend</h3><canvas id="ct" height="90"></canvas></div>
    <div class="cc"><h3>RPM trend</h3><canvas id="cr" height="90"></canvas></div>
    <div class="cc"><h3>Current trend</h3><canvas id="ci" height="90"></canvas></div>
    <div class="cc"><h3>VibrationZ trend</h3><canvas id="cv" height="90"></canvas></div>
  </div>
  <div class="hw">
    <h3>Recent readings (live)</h3>
    <table>
      <thead><tr><th>Time</th><th>Temp</th><th>VibZ</th><th>Current</th><th>Voltage</th><th>RPM</th><th>MagZ</th><th>Prediction</th></tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
</main>
<script>
const SC={Normal:"#10B981",Overheating:"#EF4444",Misalignment:"#F59E0B",Overload:"#8B5CF6","Motor OFF":"#64748B"};
const SI={Normal:"✅",Overheating:"🔥",Misalignment:"⚠️",Overload:"⚡","Motor OFF":"💤"};
const N=50;const lb=[];
function mk(id,col){
  return new Chart(document.getElementById(id),{type:"line",
    data:{labels:lb,datasets:[{data:[],borderColor:col,borderWidth:1.5,pointRadius:0,tension:.3,fill:false}]},
    options:{responsive:true,animation:false,plugins:{legend:{display:false}},
      scales:{x:{display:false},y:{ticks:{color:"#64748B",font:{size:9}},grid:{color:"#1E293B"}}}}});
}
const ch={t:mk("ct","#EF4444"),r:mk("cr","#38BDF8"),i:mk("ci","#8B5CF6"),v:mk("cv","#F59E0B")};
function push(c,v){const d=c.data.datasets[0].data;d.push(v);if(d.length>N)d.shift();c.update("none")}
function upd(d){
  const s=d.sensors,a=d.analysis,c=a.final_prediction||"Normal";
  const col=SC[c]||"#94A3B8",ic=SI[c]||"❓";
  const bn=document.getElementById("banner");
  bn.textContent=ic+"  "+c.toUpperCase();bn.style.color=col;bn.style.borderColor=col;
  document.getElementById("vt").textContent=s.temperature.toFixed(1);
  document.getElementById("vz").textContent=s.vibrationZ.toFixed(3);
  document.getElementById("vi").textContent=s.current.toFixed(3);
  document.getElementById("vv").textContent=(s.voltage||0).toFixed(1);
  document.getElementById("vr").textContent=Math.round(s.rpm);
  document.getElementById("vmz").textContent=(s.magZ||0).toFixed(2);
  document.getElementById("vf").textContent=a.future_risk||"—";
  document.getElementById("vd").textContent=d.device_id||"—";
  document.getElementById("pkm").textContent=a.kmeans||"—";
  document.getElementById("plr").textContent=a.logistic_reg||"—";
  document.getElementById("pdt").textContent=a.decision_tree||"—";
  document.getElementById("prb").textContent=a.rule_based||"—";
  const t=new Date(d.timestamp).toLocaleTimeString();
  if(!lb.includes(t)){lb.push(t);if(lb.length>N)lb.shift();}
  push(ch.t,s.temperature);push(ch.r,s.rpm);push(ch.i,s.current);push(ch.v,s.vibrationZ);
  const tb=document.getElementById("tb");
  const tr=document.createElement("tr");
  const badgeClass = c.replace(" ", "."); // Handles "Motor OFF" css class
  tr.innerHTML=`<td>${t}</td><td>${s.temperature.toFixed(1)}</td>
    <td>${s.vibrationZ.toFixed(3)}</td><td>${s.current.toFixed(3)}</td>
    <td>${(s.voltage||0).toFixed(1)}</td><td>${Math.round(s.rpm)}</td>
    <td>${(s.magZ||0).toFixed(2)}</td>
    <td><span class="badge ${badgeClass}">${c}</span></td>`;
  tb.insertBefore(tr,tb.firstChild);
  if(tb.children.length>20)tb.lastChild.remove();
}
function conn(){
  const proto=location.protocol==="https:"?"wss:":"ws:";
  const ws=new WebSocket(proto+"//"+location.host+"/ws");
  const el=document.getElementById("ws");
  ws.onopen=()=>{el.textContent="● Live";el.className="on"};
  ws.onmessage=e=>{try{upd(JSON.parse(e.data))}catch{}};
  ws.onclose=()=>{el.textContent="● Reconnecting...";el.className="off";setTimeout(conn,3000)};
}
conn();
setInterval(async()=>{try{const r=await fetch("/predict");if(r.ok)upd(await r.json())}catch{}},5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("cloud_server:app", host="0.0.0.0", port=port, reload=False)