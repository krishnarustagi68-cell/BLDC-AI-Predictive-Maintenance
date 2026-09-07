# BLDC Motor AI Predictive Maintenance Rig
### Hardware-in-the-Loop Multi-Sensor Telemetry & Real-Time Fault Consensus Engine

[![Hardware](https://img.shields.io/badge/Microcontroller-ESP32%20WROOM--32-E7352C?style=for-the-badge&logo=espressif&logoColor=white)](esp32/)
[![Bus Architecture](https://img.shields.io/badge/I2C-Dual%20Independent%20Buses-00f3ff?style=for-the-badge&logo=circuitverse&logoColor=black)](#hardware-components)
[![Sensor Suite](https://img.shields.io/badge/Telemetry-6%20Sensors%20Fused-00ff9d?style=for-the-badge&logo=databricks&logoColor=black)](#hardware-components)
[![ML Engine](https://img.shields.io/badge/ML%20Classification-Consensus%20Voting%20(85--95%25)-8b5cf6?style=for-the-badge&logo=scikit-learn&logoColor=white)](python/train_models.py)
[![Cloud Stream](https://img.shields.io/badge/Backend-FastAPI%20%2B%20WebSockets-009688?style=for-the-badge&logo=fastapi&logoColor=white)](python/cloud_server.py)

---

## 🏛️ System Overview

This project is an **end-to-end hardware-in-the-loop diagnostic testbed** engineered to continuously monitor brushless DC (BLDC) motor health in real time. Powered by an **ESP32 WROOM-32** microcontroller, the system interfaces directly with **6 distinct physical sensors** across a **Dual I2C Bus architecture**, streams serialized telemetry over WiFi, and classifies 4 critical operational states using a **Consensus Machine Learning Diagnostic Engine**.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                   PHYSICAL MOTOR RIG & SENSOR TELEMETRY                          │
│                                                                                  │
│   ┌──────────────────────────┐             ┌─────────────────────────────────┐   │
│   │   BLDC Motor + 30A ESC   │             │   6-Sensor Telemetry Suite      │   │
│   │   • 3-Phase 1000kV Motor │             │   • ISM330DHCX (6-DoF IMU)      │   │
│   │   • 12V LiPo Power Rail  │             │   • MMC5983MA (3-Axis Mag)      │   │
│   │   • 50Hz PWM Speed Pin   │             │   • INA219 (High-Side Current)  │   │
│   └────────────┬─────────────┘             │   • LM35 (Motor Core Temp)      │   │
│                │                           │   • Optical IR Tachometer (RPM) │   │
│                │                           └────────────────┬────────────────┘   │
└────────────────┼────────────────────────────────────────────┼────────────────────┘
                 │                                            │
                 ▼                                            ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                       ESP32 WROOM-32 FIRMWARE ENGINE                             │
│                                                                                  │
│   ┌────────────────────────────────────────┐ ┌───────────────────────────────┐   │
│   │   I2C Bus 0 (GPIO 21 SDA / 22 SCL)     │ │  I2C Bus 1 (GPIO 17 / 16)     │   │
│   │   • ISM330DHCX (0x6A)  • INA219 (0x40) │ │  • OLED #2 GM009605 (0x3C)    │   │
│   │   • MMC5983MA (0x30)   • OLED #1 (0x3C)│ │    [Resolves Address Conflict]│   │
│   └────────────────────────────────────────┘ └───────────────────────────────┘   │
│                                                                                  │
│   • Non-blocking polling loop (20Hz sampling)                                    │
│   • Local threshold safety overrides & fail-safe ESC cutoff                      │
│   • WiFi telemetry packet serialization (JSON / REST / WebSocket)                │
└──────────────────────────────────────┬───────────────────────────────────────────┘
                                       │
                     ┌─────────────────┴─────────────────┐
                     ▼                                   ▼
┌──────────────────────────────────────────┐ ┌─────────────────────────────────────┐
│    DIAGNOSTIC CONSENSUS ENGINE (Python)  │ │      REAL-TIME TELEMETRY SURFACES   │
│                                          │ │                                     │
│   ┌──────────────────────────────────┐   │ │  1. Dual Onboard OLED Displays      │
│   │  • K-Means Clustering (Unsup.)   │   │ │     (Live metrics & fault banner)   │
│   │  • Logistic Regression           │   │ │  2. Local Diagnostic GUI            │
│   │  • Decision Tree Classifier      │   │ │     (Tkinter / CustomTkinter)       │
│   │  • Physical Rule Safety Override │   │ │  3. Cloud Monitoring Dashboard     │
│   └──────────────────────────────────┘   │ │     (FastAPI + WebSocket + Charts)  │
│   Outputs: Physical Health Score (0-100) │ │                                     │
└──────────────────────────────────────────┘ └─────────────────────────────────────┘
```

---

## ⚡ 4 Operational States Classified

1. **Normal Operation:** Baseline thermal gradient, balanced 3-phase current draw, vibration acceleration $|V_z| < 0.50g$, stable RPM.
2. **Overheating Fault:** Core thermal buildup exceeding $49^\circ\text{C}$ while operating under sustained load, triggering thermal shock rate warnings ($>1.5^\circ\text{C}/\text{s}$).
3. **Dynamic Misalignment:** Radial eccentricity and mechanical imbalance causing significant Z-axis accelerometer spikes ($|V_z| > 0.70g$).
4. **Overload Condition:** Rotor braking / mechanical torque resistance causing current spikes on the INA219 rail with RPM degradation.

---

## 🔬 Diagnostic Consensus & Anomaly Engine (`fusion.py`)

Rather than relying on a single black-box model, the system fuses physical domain heuristics with machine learning predictions:
* **Physical Health Score (0–100):** Continuous mathematical score penalizing thermal overshoot (-30 pts), Z-axis vibration spikes (-30 pts), over-current draw (-20 pts), voltage drop (-10 pts), and RPM stalls (-10 pts).
* **Sensor Dropout Detection:** Flags hardware anomalies (e.g., motor spinning $>100\text{ RPM}$ while current reads $0.0\text{A}$, indicating INA219 shunt disconnection).
* **Consensus Voting:** Evaluates K-Means, Logistic Regression, and Decision Tree outputs, utilizing deterministic physical rules as a hard override for safety-critical states (e.g., Motor OFF).

---

## Hardware Components

| Component | Purpose | Interface | I2C Address |
|---|---|---|---|
| ESP32 WROOM-32 | Main controller & edge telemetry node | — | — |
| ISM330DHCX | 6-DoF IMU (Accelerometer + Gyroscope) | I2C Bus 0 | 0x6A |
| MMC5983MA | 3-Axis Precision Magnetometer | I2C Bus 0 | 0x30 |
| INA219 | High-Side DC Current & Bus Voltage Sensor | I2C Bus 0 | 0x40 |
| OLED #1 GM009605 | Primary sensor metric visualizer | I2C Bus 0 | 0x3C |
| OLED #2 GM009605 | Diagnostic state & fault banner | I2C Bus 1 | 0x3C |
| LM35 | Precision analog motor core temperature | Analog GPIO34 | — |
| Optical IR Sensor | High-speed optical tachometer (RPM) | Digital GPIO27 | — |
| ESC 30A | 3-Phase brushless electronic speed controller | PWM GPIO18 | — |
| 1000kV BLDC Motor | Brushless DC motor under diagnostic test | ESC 3-phase | — |
| 12V LiPo Battery | High-discharge power source for ESC & motor | Power rail | — |

> **Dual I2C Engineering Note:** Both GM009605 OLED displays share a hardcoded I2C address of `0x3C`. Instead of requiring an external I2C multiplexer (e.g., TCA9548A), this design leverages the ESP32's dual hardware I2C peripheral blocks: **Bus 0 (GPIO 21/22)** and **Bus 1 (GPIO 17/16)**. This eliminates bus contention, cuts hardware BOM cost, and maximizes frame refresh rates.

---

## Section 1 — Wiring Guide

### I2C Bus 0 — SDA=GPIO21, SCL=GPIO22
All sensors and OLED #1 connect to the same 2 wires.

```
ESP32 GPIO21 (SDA) ──────────────────────────────────────────┐
ESP32 GPIO22 (SCL) ────────────────────────────────────────┐ │
                                                           │ │
ISM330DHCX:    VCC→3.3V  GND→GND  SDA→21  SCL→22  SDO→GND │ │
MMC5983MA:     VCC→3.3V  GND→GND  SDA→21  SCL→22           │ │
INA219:        VCC→3.3V  GND→GND  SDA→21  SCL→22           │ │
OLED #1:       VCC→3.3V  GND→GND  SDA→21  SCL→22           │ │
```

**ISM330DHCX SDO pin:** Connect SDO to GND to set address 0x6A.
If SDO is HIGH (3.3V), address becomes 0x6B — change `imu.begin()` to `imu.begin(0x6B)` in code.

**INA219 Vin+ and Vin-:** These two pins go IN-LINE with the motor power wire.
```
Battery (+) ──→ INA219 Vin+ ──→ INA219 Vin- ──→ ESC battery+ wire
```
This is how the INA219 measures current flowing to the motor.

### I2C Bus 1 — SDA=GPIO17, SCL=GPIO16
OLED #2 only — separate bus solves the address conflict.

```
ESP32 GPIO17 (SDA) ──→ OLED #2 SDA
ESP32 GPIO16 (SCL) ──→ OLED #2 SCL
OLED #2 VCC ──→ 3.3V
OLED #2 GND ──→ GND
```

### LM35 Temperature Sensor
```
LM35 left  pin ──→ 5V
LM35 middle pin ──→ ESP32 GPIO34 (also connect a 0.1µF cap between this and GND)
LM35 right pin ──→ GND
```
> Note: ESP32 ADC is 3.3V max. LM35 at 5V can output up to 1500mV (150°C).
> At temperatures below 150°C it stays under 3.3V and is safe.
> If your motor gets hotter than 100°C, add a voltage divider (2 resistors).

### IR Sensor (RPM)
```
IR Sensor VCC ──→ 5V
IR Sensor GND ──→ GND
IR Sensor OUT ──→ ESP32 GPIO27
```
Stick a small piece of white reflective tape on the motor shaft.
The IR sensor detects one reflection per revolution.

### ESC (Electronic Speed Controller)
```
ESC signal wire (white/yellow) ──→ ESP32 GPIO18
ESC power wire (red)           ──→ 5V from ESC BEC
ESC ground wire (black)        ──→ GND
ESC battery+ (thick red)       ──→ INA219 Vin- ──→ Battery+
ESC battery- (thick black)     ──→ Battery-
ESC 3-phase wires              ──→ BLDC Motor (any order, swap 2 if wrong direction)
```

### ESP32 Power
```
ESP32 5V pin  ──→ 5V (from USB or ESC BEC output)
ESP32 GND     ──→ Common GND
```

### Complete Pin Summary

| ESP32 Pin | Connected to | Wire color suggestion |
|---|---|---|
| GPIO21 (SDA) | All sensors + OLED #1 SDA | Blue |
| GPIO22 (SCL) | All sensors + OLED #1 SCL | Yellow |
| GPIO17 (SDA1) | OLED #2 SDA only | Blue |
| GPIO16 (SCL1) | OLED #2 SCL only | Yellow |
| GPIO34 (ADC) | LM35 middle pin | Orange |
| GPIO27 | IR sensor output | White |
| GPIO18 (PWM) | ESC signal wire | Purple |
| 3.3V | VCC of all sensors + both OLEDs | Red (thin) |
| GND | GND of all components | Black |

---

## Section 2 — Software Setup (One Time)

### Install Arduino IDE
Download from https://www.arduino.cc/en/software
Install version 2.x (recommended).

### Add ESP32 Board to Arduino IDE
1. Open Arduino IDE
2. Go to File → Preferences
3. In "Additional boards manager URLs" paste:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
4. Click OK
5. Go to Tools → Board → Boards Manager
6. Search for "esp32"
7. Install "esp32 by Espressif Systems"

### Arduino IDE Board Settings
Every time you upload to ESP32, set these:
- Tools → Board → ESP32 Arduino → ESP32 Dev Module
- Tools → Upload Speed → 921600
- Tools → Flash Size → 4MB (32Mb)
- Tools → Port → (the COM port your ESP32 shows up on)

### Install Arduino Libraries
Go to Tools → Manage Libraries and install each one:

| Search for | Install | Author |
|---|---|---|
| SparkFun ISM330DHCX | SparkFun ISM330DHCX | SparkFun Electronics |
| SparkFun MMC5983MA | SparkFun MMC5983MA Magnetometer | SparkFun Electronics |
| Adafruit INA219 | Adafruit INA219 | Adafruit |
| Adafruit SSD1306 | Adafruit SSD1306 | Adafruit |
| Adafruit GFX | Adafruit GFX Library | Adafruit |
| ArduinoJson | ArduinoJson | Benoit Blanchon |

### Install Python (One Time)
Download Python 3.11 from python.org. During install, check "Add to PATH".

Open Command Prompt and run:
```
pip install -r python/requirements.txt
```

---

## Section 3 — Project Folder Structure

```
bldc_project/
├── esp32/
│   ├── step1_collect/
│   │   └── step1_collect.ino      ← Upload during data collection
│   └── step2_live/
│       └── step2_live.ino         ← Upload after training (live monitor)
│
├── python/
│   ├── data_collector.py          ← Collects CSV from ESP32
│   ├── train_models.py            ← Trains ML models
│   ├── gui_dashboard.py           ← Local desktop GUI
│   ├── cloud_server.py            ← Web server (public link)
│   └── requirements.txt
│
├── models/                        ← .pkl files saved here after training
├── dataset/                       ← motor_data.csv saved here
└── README.md                      ← This file
```

---

## Section 4 — Full Step-by-Step Workflow

### STEP 1 — Collect sensor data

**A. Upload firmware to ESP32**
1. Open `esp32/step1_collect/step1_collect.ino` in Arduino IDE
2. Select correct board and port (see Section 2)
3. Click Upload (hold BOOT button if needed)
4. Wait for "Done uploading"
5. Close Serial Monitor if it opens automatically

**B. Run data collector on PC**
Open Command Prompt in the `python/` folder.

Collect Normal condition (motor running freely, nothing touching it):
```
python data_collector.py --port COM3 --condition Normal --samples 300
```

Collect Overheating (wrap cloth around motor body):
```
python data_collector.py --port COM3 --condition Overheating --samples 300
```

Collect Misalignment (gently tilt motor frame 10-15 degrees ONLY):
```
python data_collector.py --port COM3 --condition Misalignment --samples 300
```
> Important for Misalignment: Tilt gently. If VibZ shows ±2.0 the script will skip those rows.
> Keep the motor spinning — RPM must stay above 600.

Collect Overload (light steady finger pressure on shaft while spinning):
```
python data_collector.py --port COM3 --condition Overload --samples 300
```

**Check your dataset:**
```
python -c "import pandas as pd; df=pd.read_csv('../dataset/motor_data.csv'); print(df['Condition'].value_counts())"
```
Should show 300 rows for each of the 4 conditions.

> Linux/Mac: replace COM3 with /dev/ttyUSB0 or /dev/ttyACM0

### STEP 2 — Train ML models

In the `python/` folder:
```
python train_models.py
```

This will print accuracy for all 3 models. Decision Tree is usually best (around 80-90%).
Six .pkl files will be saved to the `models/` folder.

### STEP 3 — Test local GUI

Run the GUI dashboard to verify everything works:
```
python gui_dashboard.py --port COM3
```

Or test without ESP32 using demo mode:
```
python gui_dashboard.py --demo
```

The GUI shows:
- Live sensor readings (Temperature, VibrationZ, Current, Voltage, RPM)
- Big status banner with fault condition
- All 3 ML model predictions
- Trend charts
- Alert log

### STEP 4 — Upload live firmware to ESP32

**Before uploading**, open `esp32/step2_live/step2_live.ino` and change these 3 lines:
```cpp
#define WIFI_SSID     "YOUR_WIFI_NAME"       // your WiFi network name
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"   // your WiFi password
#define SERVER_URL    "https://YOUR-APP-NAME.onrender.com/data"  // after deployment
```

Upload to ESP32. The motor will start, OLEDs will show sensor data and fault status.
Local fault detection works immediately even without internet.

### STEP 5 — Deploy to cloud (public link)

**Test locally first:**
```
python cloud_server.py
```
Open http://localhost:8000 in your browser. You should see the dashboard.
If you change SERVER_URL in the ESP32 to http://YOUR_PC_IP:8000/data it will post data locally.

**Deploy to Render (free public URL):**

1. Create a free account at https://render.com

2. Push your project to GitHub:
   ```
   git init
   git add .
   git commit -m "BLDC Motor Monitor"
   ```
   Create a new repo on github.com and push.
   Include the `models/` folder — the .pkl files must be in the repo.

3. In Render dashboard:
   - Click "New +" → "Web Service"
   - Connect your GitHub repo
   - Set these settings:
     - Name: bldc-motor-monitor (or anything you like)
     - Root Directory: python
     - Build Command: `pip install -r requirements.txt`
     - Start Command: `uvicorn cloud_server:app --host 0.0.0.0 --port $PORT`
   - Click "Create Web Service"

4. Wait 2-3 minutes for first deployment.

5. Your public URL is shown at top:
   ```
   https://bldc-motor-monitor.onrender.com
   ```
   Anyone can open this link on any device anywhere in the world.

6. Update `step2_live.ino`:
   ```cpp
   #define SERVER_URL "https://bldc-motor-monitor.onrender.com/data"
   ```
   Re-upload to ESP32.

7. Now ESP32 posts data to cloud → anyone opening the URL sees live data.

> Free tier note: Render free services sleep after 15 minutes of no traffic.
> First request after sleep takes 30-60 seconds to wake up.
> Upgrade to paid ($7/month) for always-on.

---

## Section 5 — Troubleshooting

### ESP32 not uploading
- Hold the BOOT button on ESP32, then click Upload, release BOOT when "Connecting..." appears
- Wrong port: Tools → Port → check which port appears/disappears when you plug/unplug ESP32
- Another app using the port: Close Serial Monitor, close other terminal windows

### ISM330DHCX not found
- Check SDA=21, SCL=22 wiring
- SDO pin must be connected to GND for address 0x6A
- Try address 0x6B if 0x6A fails: change `imu.begin()` to `imu.begin(0x6B)` in both .ino files

### INA219 not found
- Check SDA=21, SCL=22 wiring
- All 3 address pins (A0, A1, A2) must be connected to GND for address 0x40

### OLED #2 not showing
- Check SDA=17, SCL=16 wiring (different from OLED #1)
- OLED #2 gets its own I2C bus — do NOT connect it to pins 21/22

### Motor not spinning
- ESC needs 3 seconds of 1000µs signal to arm — code does this automatically
- Check 12V battery is connected and charged
- ESC beeps 3 times when armed successfully
- Check 3-phase wires are connected between ESC and motor

### COM port access denied (Windows)
- Serial Monitor is open — close it
- Another Python script is running with that port — close it
- Restart Arduino IDE

### data_collector.py shows 0 samples
- Motor not started yet — wait for ESC arm sequence (about 14 seconds)
- Wrong baud rate — must be 115200
- Serial Monitor open — close it

### Misalignment data all getting skipped
- Tilting too hard — VibZ hits ±2.0 limit
- Tilt only 10-15 degrees, hold steady, wait
- RPM dropping too low — reduce finger pressure on shaft

### train_models.py — accuracy too low
- Check dataset has enough samples (300 per condition)
- Misalignment data must have clearly different VibZ values than Normal
- Run `python -c "import pandas as pd; df=pd.read_csv('../dataset/motor_data.csv'); print(df.groupby('Condition')[['VibrationZ','RPM','Temperature']].mean())"` to check if conditions are separable

### Cloud server not receiving data
- Check WIFI_SSID and WIFI_PASSWORD in step2_live.ino are correct
- Check SERVER_URL ends with /data
- Open http://your-url.onrender.com/health to check server is running
- Open Serial Monitor at 115200 to see ESP32 debug output

---

## Section 6 — API Endpoints (for advanced use)

Once deployed, these endpoints are available:

| Method | URL | Description |
|---|---|---|
| POST | /data | ESP32 sends sensor readings here |
| GET | /predict | Returns latest processed data |
| GET | /history?n=50 | Returns last 50 readings |
| GET | /stats | Fault distribution and averages |
| GET | / | Live web dashboard |
| GET | /health | Server status check |
| GET | /docs | Auto-generated API documentation |
| WS | /ws | WebSocket real-time stream |

Example: test your server is running:
```
curl https://your-app.onrender.com/health
```

---

## Section 7 — Detection Thresholds

These thresholds are set in the Python code and ESP32 firmware.
After collecting your own data, you may need to adjust them.

| Condition | Threshold | How to adjust |
|---|---|---|
| Misalignment | |VibrationZ| > 0.70 g | If Normal VibZ goes above 0.50, increase to 0.80 |
| Overload | RPM > 1560 | Adjust based on your motor's normal max RPM |
| Overheating | Temp > 49°C AND RPM > 1480 | Adjust based on your environment temperature |

In `gui_dashboard.py` look for the `THRESH` dictionary.
In `cloud_server.py` look for the `rule_detect()` function.
In `step2_live.ino` look for the `#define THRESH_*` lines.

---

## Section 8 — Files Reference

| File | When to use |
|---|---|
| `esp32/step1_collect/step1_collect.ino` | Upload during data collection phase only |
| `esp32/step2_live/step2_live.ino` | Upload after training — for live monitoring |
| `python/data_collector.py` | Run to save CSV data from ESP32 |
| `python/train_models.py` | Run after collecting all 4 conditions |
| `python/gui_dashboard.py` | Run for local desktop monitoring |
| `python/cloud_server.py` | Run for cloud/website monitoring |
| `python/requirements.txt` | Python packages list |
| `models/` | .pkl files saved here by train_models.py |
| `dataset/motor_data.csv` | CSV dataset saved here by data_collector.py |

