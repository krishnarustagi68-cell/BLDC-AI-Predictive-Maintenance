"""
================================================================
BLDC Motor -- Local GUI Dashboard
Live monitoring via Serial from ESP32
Updated for ISM330DHCX + INA219 + MMC5983MA
================================================================
Requirements:
pip install pyserial matplotlib numpy
Models must be trained first (run train_models.py)
================================================================
"""

import os, sys, time, queue, pickle, random
import threading, argparse
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from collections import deque
from datetime import datetime

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

try:
    import serial
    import serial.tools.list_ports
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False
    print("pyserial not installed -- demo mode only")

MODELS_DIR = os.path.join("..", "models")
HISTORY = 60

# ── Detection thresholds -- FIXED TO MATCH YOUR REAL DATASET ──
THRESH = {
    "VibZ": 0.0015,    # VibZ > 0.0015 --> Misalignment (Normal max = 0.0012)
    "VibX": 0.0085,    # |VibX| > 0.0085 --> Misalignment (Normal max = 0.0072)
    "MagZ": -0.125,    # MagZ > -0.125 --> Misalignment (Normal min = -0.159)
    "RPM_OVR": 1290,   # RPM < 1290 --> Overload (Normal min = 1293)
    "TEMP": 45.0,      # Temp > 45 --> Overheating (Normal max = 44)
}

STATUS_COLORS = {
    "Normal": "#10B981",
    "Overheating": "#EF4444",
    "Misalignment": "#F59E0B",
    "Overload": "#8B5CF6",
    "Unknown": "#64748B",
}

STATUS_ICONS = {
    "Normal": "OK",
    "Overheating": "HOT",
    "Misalignment": "WARN",
    "Overload": "LOAD",
}

DARK = "#0F172A"
CARD = "#1E293B"
BORDER = "#334155"
TEXT = "#F1F5F9"
MUTED = "#94A3B8"
ACCENT = "#38BDF8"

ALL_CONDITIONS = ["Normal", "Misalignment", "Overload", "Overheating"]

# ── Rule detection ────────────────────────────────────────────
def detect(d: dict) -> str:
    vz = abs(d.get("VibrationZ", 0))
    vx = abs(d.get("VibrationX", 0))
    rpm = d.get("RPM", 0)
    t = d.get("Temperature", 0)
    mz = d.get("MagZ", 0)

    misalign_score = 0
    if vz > THRESH["VibZ"]: misalign_score += 2
    if vx > THRESH["VibX"]: misalign_score += 1
    if mz > THRESH["MagZ"]: misalign_score += 1

    if misalign_score >= 2: return "Misalignment"
    if rpm > 0 and rpm < THRESH["RPM_OVR"]: return "Overload"
    if t > THRESH["TEMP"]: return "Overheating"
    return "Normal"

# ── ML models ─────────────────────────────────────────────────
class Models:
    def __init__(self):
        self.ready = False
        try:
            def L(f): return pickle.load(open(os.path.join(MODELS_DIR, f), "rb"))
            self.km = L("kmeans.pkl")
            self.km_map = L("kmeans_label_map.pkl")
            self.lr = L("logistic_regression.pkl")
            self.dt = L("decision_tree.pkl")
            self.scaler = L("scaler.pkl")
            self.le = L("label_encoder.pkl")
            self.features = L("feature_list.pkl")
            self.ready = True
            print("ML models loaded OK")
        except FileNotFoundError as e:
            print(f"Models not ready: {e}")

    def predict(self, d: dict) -> dict:
        if not self.ready:
            return {"K-Means": "--", "Logistic Reg": "--", "Decision Tree": "--"}
        try:
            x = np.array([d.get(f, 0) for f in self.features]).reshape(1, -1)
            xs = self.scaler.transform(x)
            km_c = self.km.predict(xs)[0]
            km_p = self.km_map.get(km_c, "Unknown")
            
            lr_pred_raw = self.lr.predict(xs)[0]
            if isinstance(lr_pred_raw, (int, np.integer)):
                lr_p = self.le.inverse_transform([lr_pred_raw])[0]
            else:
                lr_p = str(lr_pred_raw)

            dt_pred_raw = self.dt.predict(x)[0]
            if isinstance(dt_pred_raw, (int, np.integer)):
                dt_p = self.le.inverse_transform([dt_pred_raw])[0]
            else:
                dt_p = str(dt_pred_raw)
                
            return {"K-Means": km_p, "Logistic Reg": lr_p, "Decision Tree": dt_p}
        except Exception as e:
            print(f"ML error: {e}")
            return {"K-Means": "--", "Logistic Reg": "--", "Decision Tree": "--"}

# ── Serial / Demo data thread ────────────────────────────────
class DataThread(threading.Thread):
    def __init__(self, port, q, demo=False):
        super().__init__(daemon=True)
        self.port = port
        self.q = q
        self.demo = demo
        self.running = True

    def run(self):
        if self.demo: self._demo()
        else: self._serial()

    def _serial(self):
        try:
            ser = serial.Serial(self.port, 115200, timeout=2)
            print(f"Connected: {self.port}")
            time.sleep(14) # Wait for ESC arm + ramp
            ser.flushInput()
            while self.running:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
                d = self._parse(raw)
                if d: self.q.put(d)
            ser.close()
        except Exception as e:
            self.q.put({"error": str(e)})

    def _demo(self):
        t = 0
        while self.running:
            t += 1
            phase = (t // 20) % 4
            if phase == 0: # Normal
                d = {"Temperature": random.uniform(18, 22), "VibrationX": random.uniform(-0.0072, -0.0061),
                     "VibrationY": random.uniform(0.9828, 0.9838), "VibrationZ": random.uniform(0.0001, 0.0012),
                     "Current": 0.0, "Voltage": random.uniform(8.28, 8.33),
                     "RPM": random.choice([1310.8, 1319.4, 1328.1, 1339.1]),
                     "MagX": random.uniform(0.329, 0.350), "MagY": random.uniform(-0.460, -0.435), "MagZ": random.uniform(-0.159, -0.133)}
            elif phase == 1: # Overheating
                d = {"Temperature": random.uniform(46, 50), "VibrationX": random.uniform(-0.0072, -0.0061),
                     "VibrationY": random.uniform(0.9828, 0.9838), "VibrationZ": random.uniform(0.0001, 0.0012),
                     "Current": 0.0, "Voltage": random.uniform(8.25, 8.30),
                     "RPM": random.choice([1310.8, 1319.4, 1328.1]),
                     "MagX": random.uniform(0.329, 0.350), "MagY": random.uniform(-0.460, -0.435), "MagZ": random.uniform(-0.159, -0.133)}
            elif phase == 2: # Misalignment
                d = {"Temperature": random.uniform(18, 24), "VibrationX": random.uniform(-0.0137, -0.0076),
                     "VibrationY": random.uniform(0.9809, 0.9826), "VibrationZ": random.uniform(0.0015, 0.0045),
                     "Current": 0.0, "Voltage": random.uniform(8.25, 8.30),
                     "RPM": random.choice([1330.4, 1339.1, 1347.8, 1355.3]),
                     "MagX": random.uniform(0.321, 0.349), "MagY": random.uniform(-0.461, -0.429), "MagZ": random.uniform(-0.122, -0.080)}
            else: # Overload
                d = {"Temperature": random.uniform(29, 44), "VibrationX": random.uniform(-0.0072, -0.0061),
                     "VibrationY": random.uniform(0.9828, 0.9838), "VibrationZ": random.uniform(0.0001, 0.0012),
                     "Current": 0.0, "Voltage": random.uniform(8.10, 8.20),
                     "RPM": random.uniform(1211, 1269),
                     "MagX": random.uniform(0.329, 0.350), "MagY": random.uniform(-0.460, -0.435), "MagZ": random.uniform(-0.159, -0.133)}
            self.q.put(d)
            time.sleep(0.8)

    def _parse(self, line):
        try:
            if not line or any(x in line for x in ["Temperature", "ERROR", "READY", "===", "Motor"]): return None
            p = [x.strip() for x in line.split(",")]
            if len(p) < 7: return None
            
            def safe_float(val, default=0.0):
                try: return float(val)
                except ValueError: return default

            return {"Temperature": float(p[0]), "VibrationX": float(p[1]), "VibrationY": float(p[2]),
                    "VibrationZ": float(p[3]), "Current": float(p[4]), "Voltage": float(p[5]),
                    "RPM": float(p[6]),
                    "MagX": safe_float(p[7]) if len(p) > 7 else 0.0,
                    "MagY": safe_float(p[8]) if len(p) > 8 else 0.0,
                    "MagZ": safe_float(p[9]) if len(p) > 9 else 0.0}
        except: return None

    def stop(self): self.running = False

# ── Main Dashboard ────────────────────────────────────────────
class Dashboard(tk.Tk):
    def __init__(self, port="COM3", demo=False):
        super().__init__()
        self.port = port
        self.demo = demo
        self.q = queue.Queue()
        self.thread = None
        self.models = Models()
        self._hist = {k: deque([0.0]*HISTORY, maxlen=HISTORY) for k in
                      ["Temperature", "VibrationX", "VibrationY", "VibrationZ", "Voltage", "RPM"]}
        self._build_ui()
        self._build_charts()
        if demo or not SERIAL_OK: self._start_demo()
        self.after(500, self._poll)

    def _build_ui(self):
        self.title("BLDC Motor Health Monitor")
        self.configure(bg=DARK)
        self.geometry("1400x880")

        # Title
        tb = tk.Frame(self, bg=DARK); tb.pack(fill="x", padx=20, pady=(12, 4))
        tk.Label(tb, text="BLDC Motor Health Monitor", font=("Courier New", 16, "bold"), bg=DARK, fg=ACCENT).pack(side="left")
        self._clock_lbl = tk.Label(tb, text="", font=("Courier New", 10), bg=DARK, fg=MUTED); self._clock_lbl.pack(side="right")
        self._tick_clock()

        # Connection
        cf = tk.Frame(self, bg=CARD, padx=12, pady=7); cf.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(cf, text="Port:", bg=CARD, fg=MUTED, font=("Courier New", 10)).pack(side="left")
        self._port_var = tk.StringVar(value=self.port)
        self._combo = ttk.Combobox(cf, textvariable=self._port_var, width=12, font=("Courier New", 10))
        self._combo["values"] = self._get_ports(); self._combo.pack(side="left", padx=(6, 14))
        self._btn = tk.Button(cf, text="Connect", command=self._toggle_conn, bg="#1D4ED8", fg="white", font=("Courier New", 10, "bold"), relief="flat", padx=12, pady=4, cursor="hand2"); self._btn.pack(side="left")
        tk.Button(cf, text="R", command=self._refresh_ports, bg=CARD, fg=MUTED, relief="flat", font=("Courier New", 12), cursor="hand2").pack(side="left", padx=4)
        self._dot = tk.Label(cf, text="Demo" if self.demo else "Disconnected", font=("Courier New", 10), bg=CARD, fg="#F59E0B" if self.demo else STATUS_COLORS["Unknown"]); self._dot.pack(side="right")

        body = tk.Frame(self, bg=DARK); body.pack(fill="both", expand=True, padx=20)
        L = tk.Frame(body, bg=DARK); R = tk.Frame(body, bg=DARK)
        L.pack(side="left", fill="both", expand=True); R.pack(side="right", fill="both", expand=True, padx=(14, 0))

        self._build_sensor_cards(L); self._build_status_banner(L); self._build_detection_info(L)
        self._build_predictions(L); self._build_alert_log(L)
        self._chart_frame = tk.Frame(R, bg=CARD, bd=1, relief="solid"); self._chart_frame.pack(fill="both", expand=True)

    def _build_sensor_cards(self, p):
        tk.Label(p, text="LIVE SENSORS", font=("Courier New", 9, "bold"), bg=DARK, fg=MUTED).pack(anchor="w", pady=(0, 4))
        g = tk.Frame(p, bg=DARK); g.pack(fill="x")
        sensors = [("Temperature", "C", "T", "#EF4444"), ("VibrationX", "g", "Vx", "#F97316"),
                   ("VibrationY", "g", "Vy", "#A855F7"), ("VibrationZ", "g", "Vz", "#EAB308"),
                   ("Voltage", "V", "V", "#34D399"), ("RPM", "rpm", "R", "#38BDF8")]
        self._sv = {}
        for i, (name, unit, icon, col) in enumerate(sensors):
            c = tk.Frame(g, bg=CARD, bd=1, relief="solid", highlightthickness=1, highlightbackground=BORDER)
            c.grid(row=0, column=i, padx=(0, 6) if i < 5 else 0, sticky="nsew", ipadx=7, ipady=7); g.columnconfigure(i, weight=1)
            tk.Label(c, text=f"{icon} {name}", font=("Courier New", 8), bg=CARD, fg=MUTED).pack()
            v = tk.StringVar(value="--"); tk.Label(c, textvariable=v, font=("Courier New", 16, "bold"), bg=CARD, fg=col).pack()
            tk.Label(c, text=unit, font=("Courier New", 8), bg=CARD, fg=MUTED).pack(); self._sv[name] = v

    def _build_status_banner(self, p):
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", pady=8)
        self._status_lbl = tk.Label(p, text="DISCONNECTED", font=("Courier New", 22, "bold"), bg=CARD, fg=STATUS_COLORS["Unknown"], anchor="center", pady=11); self._status_lbl.pack(fill="x")

    def _build_detection_info(self, p):
        f = tk.Frame(p, bg=CARD, padx=10, pady=5); f.pack(fill="x", pady=(4, 0))
        tk.Label(f, text="Detection:", font=("Courier New", 8), bg=CARD, fg=MUTED).pack(side="left")
        self._detect_var = tk.StringVar(value="--"); tk.Label(f, textvariable=self._detect_var, font=("Courier New", 9, "bold"), bg=CARD, fg=ACCENT).pack(side="left", padx=(8, 0))

    def _build_predictions(self, p):
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", pady=8)
        tk.Label(p, text="ML PREDICTIONS", font=("Courier New", 9, "bold"), bg=DARK, fg=MUTED).pack(anchor="w")
        g = tk.Frame(p, bg=DARK); g.pack(fill="x", pady=(6, 0)); self._pv = {}
        for i, (name, col) in enumerate([("K-Means", "#3B82F6"), ("Logistic Reg", "#F59E0B"), ("Decision Tree", "#10B981")]):
            c = tk.Frame(g, bg=CARD, padx=12, pady=8); c.grid(row=0, column=i, padx=(0, 8) if i < 2 else 0, sticky="nsew"); g.columnconfigure(i, weight=1)
            tk.Label(c, text=name, font=("Courier New", 8), bg=CARD, fg=col).pack()
            v = tk.StringVar(value="--"); tk.Label(c, textvariable=v, font=("Courier New", 11, "bold"), bg=CARD, fg=TEXT).pack(pady=(4, 0)); self._pv[name] = v

    def _build_alert_log(self, p):
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", pady=8)
        tk.Label(p, text="ALERT LOG", font=("Courier New", 9, "bold"), bg=DARK, fg=MUTED).pack(anchor="w")
        lf = tk.Frame(p, bg=CARD); lf.pack(fill="both", expand=True, pady=(4, 0))
        self._log_txt = tk.Text(lf, height=5, bg=CARD, fg=MUTED, font=("Courier New", 8), relief="flat", state="disabled")
        sb = ttk.Scrollbar(lf, command=self._log_txt.yview); self._log_txt.configure(yscrollcommand=sb.set)
        self._log_txt.pack(side="left", fill="both", expand=True, padx=8, pady=6); sb.pack(side="right", fill="y")

    def _build_charts(self):
        configs = [("Temperature", "C", "#EF4444"), ("VibrationX", "g", "#F97316"),
                   ("VibrationY", "g", "#A855F7"), ("VibrationZ", "g", "#EAB308"),
                   ("Voltage", "V", "#34D399"), ("RPM", "rpm", "#38BDF8")]
        fig, axes = plt.subplots(3, 2, figsize=(6.5, 8.2), facecolor=CARD, sharex=True); axes = axes.flatten()
        fig.subplots_adjust(left=0.18, right=0.96, top=0.97, bottom=0.05, hspace=0.55, wspace=0.45)
        self._lines = {}
        for ax, (key, unit, col) in zip(axes, configs):
            ax.set_facecolor(DARK); ax.tick_params(colors=MUTED, labelsize=6); ax.set_ylabel(f"{key}\n({unit})", color=MUTED, fontsize=6)
            for sp in ax.spines.values(): sp.set_color(BORDER)
            line, = ax.plot(list(self._hist[key]), color=col, linewidth=1.2); self._lines[key] = (line, ax)
        canvas = FigureCanvasTkAgg(fig, master=self._chart_frame); canvas.draw(); canvas.get_tk_widget().pack(fill="both", expand=True)
        self._fig = fig; self._canvas = canvas

    def _update_charts(self):
        for key, (line, ax) in self._lines.items():
            d = list(self._hist[key]); line.set_ydata(d); line.set_xdata(range(len(d)))
            if max(d) != min(d): m = (max(d) - min(d)) * 0.15 or 0.1; ax.set_ylim(min(d) - m, max(d) + m)
            ax.set_xlim(0, HISTORY - 1)
        self._canvas.draw_idle()

    def _process(self, data):
        self._sv["Temperature"].set(f"{data['Temperature']:.1f}")
        self._sv["VibrationX"].set(f"{data['VibrationX']:.4f}")
        self._sv["VibrationY"].set(f"{data['VibrationY']:.4f}")
        self._sv["VibrationZ"].set(f"{data['VibrationZ']:.4f}")
        self._sv["Voltage"].set(f"{data.get('Voltage', 0):.2f}")
        self._sv["RPM"].set(f"{data['RPM']:.0f}")

        for k in self._hist: self._hist[k].append(data.get(k, 0.0))

        cond = detect(data)
        col = STATUS_COLORS.get(cond, STATUS_COLORS["Unknown"]); icon = STATUS_ICONS.get(cond, "?")
        self._status_lbl.config(text=f"{icon} {cond.upper()}", fg=col)

        vz = abs(data.get("VibrationZ", 0)); vx = abs(data.get("VibrationX", 0))
        rpm = data.get("RPM", 0); t = data.get("Temperature", 0); mz = data.get("MagZ", 0)

        if cond == "Misalignment":
            reasons = []
            if vz > THRESH["VibZ"]: reasons.append(f"|VibZ|={vz:.4f}")
            if vx > THRESH["VibX"]: reasons.append(f"|VibX|={vx:.4f}")
            if mz > THRESH["MagZ"]: reasons.append(f"MagZ={mz:.3f}")
            info = "Misalignment: " + ", ".join(reasons)
        elif cond == "Overload": info = f"Overload: RPM={rpm:.0f}"
        elif cond == "Overheating": info = f"Overheating: T={t:.1f}C"
        else: info = f"Normal: VibZ={vz:.4f} VibX={vx:.4f} RPM={rpm:.0f}"
        self._detect_var.set(info)

        if cond != "Normal":
            ts = datetime.now().strftime("%H:%M:%S")
            msg = f"[{ts}] {cond}: T={t:.1f}C |VibZ|={vz:.4f} |VibX|={vx:.4f} RPM={rpm:.0f}\n"
            self._log_txt.config(state="normal"); self._log_txt.insert("end", msg); self._log_txt.see("end"); self._log_txt.config(state="disabled")

        # ML Predictions
        preds = self.models.predict(data)
        for name, val in preds.items():
            if name in self._pv:
                self._pv[name].set(val)

    def _poll(self):
        updated = False
        while not self.q.empty():
            item = self.q.get_nowait()
            if "error" in item: messagebox.showerror("Serial Error", item["error"]); self._disconnect(); break
            self._process(item); updated = True
        if updated: self._update_charts()
        self.after(500, self._poll)

    def _toggle_conn(self):
        if self.thread and self.thread.running: self._disconnect()
        else: self._connect()

    def _connect(self):
        port = self._port_var.get(); self.thread = DataThread(port, self.q, demo=False); self.thread.start()
        self._btn.config(text="Disconnect", bg="#DC2626"); self._dot.config(text=f"{port}", fg="#10B981")

    def _disconnect(self):
        if self.thread: self.thread.stop(); self.thread = None
        self._btn.config(text="Connect", bg="#1D4ED8"); self._dot.config(text="Disconnected", fg=STATUS_COLORS["Unknown"])

    def _start_demo(self):
        self.thread = DataThread(self.port, self.q, demo=True); self.thread.start()
        self._btn.config(text="Stop Demo", bg="#DC2626"); self._dot.config(text="Demo Mode", fg="#F59E0B")

    def _get_ports(self):
        if not SERIAL_OK: return ["DEMO"]
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return ports if ports else ["COM3"]

    def _refresh_ports(self): self._combo["values"] = self._get_ports()
    def _tick_clock(self): self._clock_lbl.config(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S")); self.after(1000, self._tick_clock)

    def on_close(self):
        if self.thread: self.thread.stop()
        self.destroy()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BLDC Motor GUI Dashboard")
    parser.add_argument("--port", default="COM3", help="Serial port")
    parser.add_argument("--demo", action="store_true", help="Demo mode (no ESP32)")
    args = parser.parse_args()
    app = Dashboard(port=args.port, demo=args.demo)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
