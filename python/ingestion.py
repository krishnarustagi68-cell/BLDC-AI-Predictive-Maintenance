# Robust Serial Ingestion & Validation Worker
# File: python/ingestion.py

import sys
import time
import threading
import logging
from datetime import datetime
import serial
import serial.tools.list_ports
from config import DEFAULT_PORT, DEFAULT_BAUD, STARTUP_WAIT

logger = logging.getLogger("bldc.ingestion")

def scan_serial_ports():
    """Scans system COM ports and returns a list of devices, ignoring known Bluetooth links if possible."""
    ports = serial.tools.list_ports.comports()
    active_ports = []
    for p in ports:
        desc = p.description.lower()
        # Filter out common bluetooth or virtual ports to prevent hanging
        if "bluetooth" in desc or "standard serial" in desc:
            continue
        active_ports.append(p.device)
    return active_ports if active_ports else [p.device for p in ports]

class SerialIngestionWorker(threading.Thread):
    def __init__(self, callback, port=DEFAULT_PORT, baud=DEFAULT_BAUD):
        super().__init__(daemon=True)
        self.callback = callback
        self.port = port
        self.baud = baud
        
        self.running = False
        self.connected = False
        
        # Diagnostics
        self.total_packets = 0
        self.valid_packets = 0
        self.malformed_packets = 0
        self.hz = 0.0
        self.data_quality = 100.0
        self.last_packet_time = None
        self.last_error = None
        
        # Thread controls
        self._port_lock = threading.Lock()
        
    def set_port(self, port, baud=DEFAULT_BAUD):
        """Allows dynamically switching the serial port from the UI settings."""
        with self._port_lock:
            if self.port != port or self.baud != baud:
                logger.info(f"Switching serial port from {self.port} to {port} ({baud} baud)")
                self.port = port
                self.baud = baud
                return True
        return False

    def run(self):
        self.running = True
        logger.info("Serial Ingestion worker thread started.")
        
        packet_count = 0
        last_hz_calc = time.time()
        
        while self.running:
            # 1. Acquire current port settings
            with self._port_lock:
                current_port = self.port
                current_baud = self.baud
            
            # 2. Attempt Connection
            try:
                logger.info(f"Attempting connection to Serial Port: {current_port} at {current_baud} baud")
                ser = serial.Serial(current_port, current_baud, timeout=2)
                self.connected = True
                self.last_error = None
                logger.info(f"Successfully connected to {current_port}")
                
                # Clear buffers
                ser.reset_input_buffer()
                
                # Loop reading from the connected port
                while self.running:
                    # Check if port changed mid-loop
                    with self._port_lock:
                        if self.port != current_port or self.baud != current_baud:
                            logger.info("Serial port configuration changed. Re-opening port...")
                            break
                            
                    if ser.in_waiting > 0:
                        try:
                            raw_line = ser.readline().decode("utf-8", errors="ignore").strip()
                            self.last_packet_time = datetime.now()
                            self.total_packets += 1
                            
                            # Parse raw line
                            d = self._parse_line(raw_line)
                            if d:
                                packet_count += 1
                                self.valid_packets += 1
                                # Trigger callback
                                self.callback(d)
                            else:
                                self.malformed_packets += 1
                                
                            # Calculate Data Quality %
                            self.data_quality = round((self.valid_packets / self.total_packets) * 100.0, 1)
                            
                        except Exception as e:
                            logger.warning(f"Error reading line: {e}")
                            self.malformed_packets += 1
                            
                    # Calculate Hz packet rate once per second
                    now = time.time()
                    elapsed = now - last_hz_calc
                    if elapsed >= 1.0:
                        self.hz = round(packet_count / elapsed, 1)
                        packet_count = 0
                        last_hz_calc = now
                        
                    time.sleep(0.005) # Prevent CPU thrashing
                    
                ser.close()
                self.connected = False
                
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                logger.warning(f"Serial port {current_port} unavailable: {e}. Retrying in 3 seconds...")
                
                # Reset Hz if disconnected
                self.hz = 0.0
                
                # Wait 3 seconds before next connection retry
                for _ in range(30):
                    if not self.running:
                        break
                    time.sleep(0.1)

    def stop(self):
        """Gracefully stops the worker loop."""
        self.running = False
        self.connected = False

    def _parse_line(self, line: str):
        """Parses CSV line into validated sensor reading dictionary. Returns None if invalid."""
        if not line:
            return None
            
        # Ignore debug text headers
        if any(x in line for x in ["Temperature", "ERROR", "READY", "===", "Motor", "#"]):
            return None
            
        parts = [p.strip() for p in line.split(",")]
        # Minimum core sensor readings required: Temp, VibX/Y/Z, Current, Voltage, RPM (7 columns)
        if len(parts) < 7:
            return None
            
        try:
            def safe_float(val, default=0.0):
                try:
                    return float(val)
                except ValueError:
                    return default
            
            # Core Parameters
            temp = safe_float(parts[0])
            vx = safe_float(parts[1])
            vy = safe_float(parts[2])
            vz = safe_float(parts[3])
            curr = safe_float(parts[4])
            volt = safe_float(parts[5])
            rpm = safe_float(parts[6])
            
            # Magnetometer Optional Parameters (padded to 0.0 if not provided)
            mx = safe_float(parts[7]) if len(parts) > 7 else 0.0
            my = safe_float(parts[8]) if len(parts) > 8 else 0.0
            mz = safe_float(parts[9]) if len(parts) > 9 else 0.0
            
            # Simple Range Validations (Data sanity checks)
            if not (-40.0 <= temp <= 180.0):
                return None
            if not (0.0 <= volt <= 30.0):
                return None
            if not (0.0 <= rpm <= 6000.0):
                return None
            if not (-10.0 <= curr <= 15.0):
                return None
                
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
                "device_id": self.port,
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        except Exception as e:
            logger.debug(f"Failed to parse CSV values: {line} - {e}")
            return None
