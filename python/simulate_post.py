# Physics-Correlated Telemetry Simulator Poster
# File: python/simulate_post.py

import time
import requests
import random
import argparse
import sys

SERVER_URL = "http://localhost:8000/data"

SCENARIOS = ["Normal", "Overheating", "Overload", "Misalignment", "Instability", "SensorFailure"]

def generate_telemetry(scenario, step_idx):
    """
    Generates physically correlated sensor values based on selected scenario
    and the elapsed time steps.
    """
    # Baseline Parameters
    temp = 20.0
    vx = -0.0065
    vy = 0.9830
    vz = 0.0006
    curr = 0.150
    volt = 8.30
    rpm = 1320.0
    mx = 0.335
    my = -0.450
    mz = -0.145

    if scenario == "Normal":
        # Fluctuates within normal ranges
        temp += random.uniform(-0.5, 0.5)
        vx += random.uniform(-0.0002, 0.0002)
        vy += random.uniform(-0.0002, 0.0002)
        vz += random.uniform(-0.0001, 0.0001)
        curr += random.uniform(-0.015, 0.015)
        volt += random.uniform(-0.01, 0.01)
        rpm += random.choice([-8.0, 0.0, 8.0])
        mx += random.uniform(-0.002, 0.002)
        my += random.uniform(-0.002, 0.002)
        mz += random.uniform(-0.002, 0.002)
        
    elif scenario == "Overheating":
        # Temperature climbs slowly over time steps (caps at 52C)
        temp = min(53.0, 20.0 + (step_idx * 0.8))
        curr += random.uniform(-0.015, 0.015)
        volt += random.uniform(-0.015, 0.015)
        rpm += random.choice([-8.0, 0.0, 8.0])
        
    elif scenario == "Overload":
        # Mechanical drag reduces RPM, spikes current, and raises temperature
        rpm = max(1220.0, 1320.0 - (step_idx * 4))
        curr = min(0.950, 0.150 + (step_idx * 0.04))
        temp = min(47.5, 20.0 + (step_idx * 0.5))
        vz += random.uniform(0.0002, 0.0005)
        
    elif scenario == "Misalignment":
        # Vibration along Z-axis spikes, MagZ shifts up
        vz = min(0.48, 0.0006 + (step_idx * 0.024))
        vx = min(0.018, -0.0065 - (step_idx * 0.0006))
        mz = min(-0.08, -0.145 + (step_idx * 0.0035))
        rpm += random.choice([-5.0, 5.0])
        
    elif scenario == "Instability":
        # RPM oscillates wildly, causing vibration fluctuations and current spikes
        oscillation = math_sin = 50.0 * random.uniform(-1.5, 1.5)
        rpm = 1320.0 + oscillation
        curr = 0.150 + abs(oscillation) * 0.003
        vz = 0.0006 + abs(oscillation) * 0.0004
        
    elif scenario == "SensorFailure":
        # Temperature sensor suddenly drops to 0.0 (or breaks)
        temp = 0.0
        vz += random.uniform(-0.0001, 0.0001)
        rpm += random.choice([-8.0, 0.0, 8.0])

    return {
        "Temperature": round(temp, 2),
        "VibrationX": round(vx, 4),
        "VibrationY": round(vy, 4),
        "VibrationZ": round(vz, 4),
        "Current": round(curr, 4),
        "Voltage": round(volt, 2),
        "RPM": round(rpm, 1),
        "MagX": round(mx, 3),
        "MagY": round(my, 3),
        "MagZ": round(mz, 3),
        "device_id": "ESP32_MOTOR_SIM",
        "Timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

def main():
    parser = argparse.ArgumentParser(description="Physics-Correlated BLDC Telemetry Simulator")
    parser.add_argument("--scenario", type=str, choices=SCENARIOS, default=None,
                        help="Static simulation scenario (if None, cycles through all)")
    parser.add_argument("--url", type=str, default=SERVER_URL,
                        help=f"Target ingestion endpoint (default: {SERVER_URL})")
    args = parser.parse_args()

    print(f"Starting simulation poster to: {args.url}")
    
    t = 0
    while True:
        t += 1
        
        # Decide scenario
        if args.scenario:
            scenario = args.scenario
            step_idx = t
        else:
            # Cycle through scenarios every 25 seconds
            scenario_idx = (t // 25) % len(SCENARIOS)
            scenario = SCENARIOS[scenario_idx]
            step_idx = t % 25
            
        payload = generate_telemetry(scenario, step_idx)
        
        try:
            r = requests.post(args.url, json=payload, timeout=2)
            print(f"[{time.strftime('%H:%M:%S')}] Scenario: {scenario:<15} | Status: {r.status_code} | Telemetry: T={payload['Temperature']}C RPM={payload['RPM']} I={payload['Current']}A VibZ={payload['VibrationZ']}g")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Connection failed to {args.url}: {e}")
            
        time.sleep(1.0)

if __name__ == "__main__":
    main()
