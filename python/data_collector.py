"""
================================================================
 BLDC Motor — Data Collector (Python)
 Reads CSV from ESP32 over Serial, labels it, saves to dataset
================================================================
 Usage:
   python data_collector.py --port COM3 --condition Normal      --samples 300
   python data_collector.py --port COM3 --condition Overheating  --samples 300
   python data_collector.py --port COM3 --condition Misalignment --samples 300
   python data_collector.py --port COM3 --condition Overload     --samples 300

 Linux/Mac: use /dev/ttyUSB0 or /dev/ttyACM0 instead of COM3

 How to simulate each condition:
   Normal       = Motor running freely, nothing touching it
   Overheating  = Wrap cloth around motor body to trap heat
   Misalignment = Gently tilt motor frame 10-15 degrees only
   Overload     = Light steady finger pressure on spinning shaft
================================================================
"""

import serial
import serial.tools.list_ports
import pandas as pd
import argparse
import time
import os
from datetime import datetime

DATASET_PATH    = os.path.join("..", "dataset", "motor_data.csv")
BAUD_RATE       = 115200
STARTUP_WAIT    = 14   # seconds — wait for ESC arm + ramp

VALID_CONDITIONS = ["Normal", "Overheating", "Misalignment", "Overload"]

COLUMNS = [
    "Temperature", "VibrationX", "VibrationY", "VibrationZ",
    "Current", "Voltage", "RPM", "MagX", "MagY", "MagZ",
    "Condition", "Timestamp"
]


def list_ports():
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("No serial ports found. Is ESP32 plugged in?")
        return
    print("\nAvailable serial ports:")
    for p in ports:
        print(f"  {p.device:12s}  {p.description}")


def collect(port: str, condition: str, samples: int):
    print(f"\n{'='*60}")
    print(f"  Condition : {condition}")
    print(f"  Target    : {samples} samples")
    print(f"  Port      : {port}")
    print(f"  Saving to : {DATASET_PATH}")
    print(f"{'='*60}")

    os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)

    # Load existing dataset
    existing_rows = []
    if os.path.exists(DATASET_PATH):
        try:
            df_old = pd.read_csv(DATASET_PATH)
            existing_rows = df_old.to_dict("records")
            print(f"\nExisting dataset:")
            print(df_old["Condition"].value_counts().to_string())
        except Exception as e:
            print(f"Could not load existing data: {e}")

    new_rows = []
    collected = 0
    skipped   = 0

    try:
        ser = serial.Serial(port, BAUD_RATE, timeout=3)
        print(f"\nConnected to {port}")
        print(f"Waiting {STARTUP_WAIT}s for ESC arm + motor ramp...")

        # Wait for READY signal
        start = time.time()
        ready = False
        while time.time() - start < STARTUP_WAIT + 5:
            if ser.in_waiting:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if line == "READY":
                    ready = True
                    print("ESP32 is ready!")
                    break
            time.sleep(0.1)

        if not ready:
            print("Did not receive READY signal — continuing anyway")

        ser.flushInput()

        print(f"\n*** Now physically create the {condition.upper()} condition ***")
        print("Starting collection in 3 seconds...\n")
        time.sleep(3)

        while collected < samples:
            try:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()

                # Skip non-data lines
                if (not raw
                        or "Temperature" in raw
                        or "ERROR" in raw
                        or "READY" in raw
                        or "===" in raw
                        or "Motor" in raw
                        or raw.startswith("#")):
                    continue

                parts = raw.split(",")
                if len(parts) < 7:
                    continue

                # Parse all columns (some may be missing MagX/Y/Z in older firmware)
                temp = float(parts[0])
                vx   = float(parts[1])
                vy   = float(parts[2])
                vz   = float(parts[3])
                curr = float(parts[4])
                volt = float(parts[5])
                rpm  = float(parts[6])
                mx   = float(parts[7]) if len(parts) > 7 else 0.0
                my   = float(parts[8]) if len(parts) > 8 else 0.0
                mz   = float(parts[9]) if len(parts) > 9 else 0.0

                # Sanity checks
                if temp < -10 or temp > 150:
                    skipped += 1
                    print(f"  [SKIP] Bad temperature: {temp:.1f}°C")
                    continue

                if abs(vz) >= 1.99 or abs(vx) >= 1.99 or abs(vy) >= 1.99:
                    skipped += 1
                    print(f"  [SKIP] Sensor overflow VibZ={vz:.3f} — tilt less aggressively")
                    continue

                if condition == "Misalignment" and rpm < 600:
                    skipped += 1
                    print(f"  [SKIP] RPM too low: {rpm:.0f} — keep motor spinning")
                    continue

                row = {
                    "Temperature": round(temp, 2),
                    "VibrationX":  round(vx,   4),
                    "VibrationY":  round(vy,   4),
                    "VibrationZ":  round(vz,   4),
                    "Current":     round(curr, 4),
                    "Voltage":     round(volt, 2),
                    "RPM":         round(rpm,  1),
                    "MagX":        round(mx,   3),
                    "MagY":        round(my,   3),
                    "MagZ":        round(mz,   3),
                    "Condition":   condition,
                    "Timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                new_rows.append(row)
                collected += 1

                print(f"  [{collected:3d}/{samples}]  "
                      f"T={temp:5.1f}°C  "
                      f"VibZ={vz:+.3f}  "
                      f"I={curr:.3f}A  "
                      f"V={volt:.1f}V  "
                      f"RPM={rpm:6.0f}")

            except (ValueError, UnicodeDecodeError):
                continue

        ser.close()

    except serial.SerialException as e:
        print(f"\nSerial error: {e}")
        print("\nCommon fixes:")
        print("  - Close Serial Monitor in Arduino IDE")
        print("  - Check ESP32 is plugged in")
        print("  - Check correct port number")
        if new_rows:
            print(f"\nSaving {len(new_rows)} rows collected before error...")
        else:
            return

    # Save everything
    all_rows = existing_rows + new_rows
    df = pd.DataFrame(all_rows, columns=COLUMNS)
    df.to_csv(DATASET_PATH, index=False)

    print(f"\n{'='*60}")
    print(f"  Collected : {collected} new rows")
    print(f"  Skipped   : {skipped} bad rows")
    print(f"  Saved to  : {DATASET_PATH}")
    print(f"\nDataset summary:")
    print(df["Condition"].value_counts().to_string())
    print(f"\nTotal rows: {len(df)}")
    print(f"{'='*60}")

    if len(df["Condition"].unique()) == 4:
        counts = df["Condition"].value_counts()
        if counts.min() >= 200:
            print("\n✓ All 4 conditions collected! Run train_models.py next.")
        else:
            print(f"\n! Some conditions need more samples (min is {counts.min()})")
    else:
        missing = set(VALID_CONDITIONS) - set(df["Condition"].unique())
        print(f"\n! Still need to collect: {', '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(description="BLDC Motor Data Collector")
    parser.add_argument("--port",      type=str,
                        help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--condition", type=str,
                        choices=VALID_CONDITIONS, required=True,
                        help="Fault condition to collect")
    parser.add_argument("--samples",   type=int, default=300,
                        help="Number of samples to collect (default 300)")
    parser.add_argument("--list",      action="store_true",
                        help="List available serial ports and exit")
    args = parser.parse_args()

    if args.list:
        list_ports()
        return

    if not args.port:
        list_ports()
        args.port = input("\nEnter port (e.g. COM3): ").strip()

    collect(args.port, args.condition, args.samples)


if __name__ == "__main__":
    main()
