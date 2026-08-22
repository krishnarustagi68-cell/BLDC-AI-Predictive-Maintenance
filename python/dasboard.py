import tkinter as tk
from tkinter import ttk
import serial
import json
import threading
import requests
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from collections import deque
from datetime import datetime

# --- SETTINGS ---
NGROK_URL = "https://subside-bankable-mutiny.ngrok-free.dev/data"
COLORS = {"Normal": "#10B981", "Misalignment": "#F59E0B", "Overload": "#EF4444", "Unknown": "#6B7280"}

class ModernDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GRAD2GREAT | MOTOR HEALTH ANALYTICS")
        self.geometry("1100x700")
        self.configure(bg="#0F172A")

        self.data_history = deque([0]*50, maxlen=50)
        self.running = True

        self._setup_ui()
        
        # Start Serial Thread (Update COM port as needed)
        self.thread = threading.Thread(target=self._read_serial, args=("COM3",), daemon=True)
        self.thread.start()

    def _setup_ui(self):
        # 1. Sidebar (Connection Info)
        sidebar = tk.Frame(self, bg="#1E293B", width=250)
        sidebar.pack(side="left", fill="y")
        
        tk.Label(sidebar, text="DEVICE STATUS", font=("Arial", 12, "bold"), bg="#1E293B", fg="#94A3B8").pack(pady=20)
        self.status_dot = tk.Label(sidebar, text="●  CONNECTED", fg="#10B981", bg="#1E293B", font=("Arial", 10))
        self.status_dot.pack(pady=5)
        
        self.clock_lbl = tk.Label(sidebar, text="", fg="#94A3B8", bg="#1E293B")
        self.clock_lbl.pack(side="bottom", pady=20)
        self._update_clock()

        # 2. Main Content Area
        main_area = tk.Frame(self, bg="#0F172A")
        main_area.pack(side="right", fill="both", expand=True, padx=20)

        # Header / Big Condition Card
        self.cond_card = tk.Frame(main_area, bg=COLORS["Unknown"], height=100)
        self.cond_card.pack(fill="x", pady=20)
        self.cond_text = tk.Label(self.cond_card, text="WAITING FOR DATA", font=("Arial", 24, "bold"), bg=COLORS["Unknown"], fg="white")
        self.cond_text.pack(pady=25)

        # Metrics Grid
        metrics_frame = tk.Frame(main_area, bg="#0F172A")
        metrics_frame.pack(fill="x")

        self.val_temp = self._create_metric_card(metrics_frame, "TEMPERATURE", "0.0°C", 0)
        self.val_rpm = self._create_metric_card(metrics_frame, "RPM", "0", 1)
        self.val_vib = self._create_metric_card(metrics_frame, "VIBRATION (Z)", "0.00G", 2)

        # 3. Live Graph
        graph_frame = tk.Frame(main_area, bg="#1E293B", padx=10, pady=10)
        graph_frame.pack(fill="both", expand=True, pady=20)
        
        self.fig = Figure(figsize=(5, 3), dpi=100, facecolor='#1E293B')
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('#0F172A')
        self.ax.tick_params(colors='white')
        self.line, = self.ax.plot(self.data_history, color='#38BDF8', linewidth=2)
        self.ax.set_ylim(-1, 1)

        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _create_metric_card(self, parent, title, value, col):
        card = tk.Frame(parent, bg="#1E293B", padx=20, pady=20)
        card.grid(row=0, column=col, padx=10, sticky="nsew")
        parent.grid_columnconfigure(col, weight=1)
        
        tk.Label(card, text=title, font=("Arial", 10), bg="#1E293B", fg="#94A3B8").pack()
        lbl = tk.Label(card, text=value, font=("Arial", 20, "bold"), bg="#1E293B", fg="white")
        lbl.pack(pady=5)
        return lbl

    def _update_clock(self):
        self.clock_lbl.config(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._update_clock)

    def _read_serial(self, port):
        try:
            ser = serial.Serial(port, 115200, timeout=1)
            while self.running:
                if ser.in_waiting > 0:
                    line = ser.readline().decode('utf-8').strip()
                    try:
                        data = json.loads(line)
                        self.after(0, self._process_data, data)
                        # Sync to Cloud in background
                        requests.post(NGROK_URL, json=data, timeout=1)
                    except: pass
        except Exception as e:
            print(f"Serial Error: {e}")

    def _process_data(self, data):
        cond = data.get("Condition", "Normal")
        
        # Update Big Card
        self.cond_card.config(bg=COLORS.get(cond, COLORS["Unknown"]))
        self.cond_text.config(text=cond.upper(), bg=COLORS.get(cond, COLORS["Unknown"]))
        
        # Update Text Values
        self.val_temp.config(text=f"{data.get('Temperature', 0):.1f}°C")
        self.val_rpm.config(text=f"{data.get('RPM', 0):.0f}")
        self.val_vib.config(text=f"{data.get('VibrationZ', 0):.3f}G")

        # Update Graph
        self.data_history.append(data.get('VibrationZ', 0))
        self.line.set_ydata(self.data_history)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw()

if __name__ == "__main__":
    app = ModernDashboard()
    app.mainloop()