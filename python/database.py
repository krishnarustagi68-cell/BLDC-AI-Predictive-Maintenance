# SQLite Telemetry and Alert Database Management
# File: python/database.py

import sqlite3
import os
from datetime import datetime
from config import DB_PATH

def get_db_connection():
    """Establishes an active connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema if tables do not exist."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create Telemetry Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        device_id TEXT NOT NULL,
        temperature REAL NOT NULL,
        vibration_x REAL NOT NULL,
        vibration_y REAL NOT NULL,
        vibration_z REAL NOT NULL,
        current REAL NOT NULL,
        voltage REAL NOT NULL,
        rpm REAL NOT NULL,
        mag_x REAL NOT NULL,
        mag_y REAL NOT NULL,
        mag_z REAL NOT NULL,
        health_score INTEGER NOT NULL,
        anomaly INTEGER NOT NULL,
        prediction TEXT NOT NULL,
        mode TEXT NOT NULL
    );
    """)

    # Create Alerts Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        severity TEXT NOT NULL,
        sensor TEXT NOT NULL,
        value REAL NOT NULL,
        threshold REAL NOT NULL,
        message TEXT NOT NULL,
        acknowledged INTEGER DEFAULT 0
    );
    """)
    
    conn.commit()
    conn.close()

def log_telemetry(d: dict, health_score: int, anomaly: int, prediction: str, mode: str):
    """Inserts a normalized telemetry packet into the telemetry table."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    ts = d.get("Timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    cursor.execute("""
    INSERT INTO telemetry (
        timestamp, device_id, temperature, vibration_x, vibration_y, vibration_z,
        current, voltage, rpm, mag_x, mag_y, mag_z, health_score, anomaly, prediction, mode
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ts,
        d.get("device_id", "Unknown"),
        d.get("Temperature", 0.0),
        d.get("VibrationX", 0.0),
        d.get("VibrationY", 0.0),
        d.get("VibrationZ", 0.0),
        d.get("Current", 0.0),
        d.get("Voltage", 0.0),
        d.get("RPM", 0.0),
        d.get("MagX", 0.0),
        d.get("MagY", 0.0),
        d.get("MagZ", 0.0),
        health_score,
        anomaly,
        prediction,
        mode
    ))
    
    conn.commit()
    conn.close()

def log_alert(severity: str, sensor: str, value: float, threshold: float, message: str):
    """Inserts a new alert trigger. Returns the generated alert dictionary."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Avoid duplicate unacknowledged alerts for the same sensor/severity within 3 seconds
    cursor.execute("""
    SELECT id FROM alerts 
    WHERE sensor = ? AND severity = ? AND acknowledged = 0 
    AND datetime(timestamp) >= datetime('now', '-3 seconds')
    """, (sensor, severity))
    
    if cursor.fetchone():
        conn.close()
        return None

    cursor.execute("""
    INSERT INTO alerts (timestamp, severity, sensor, value, threshold, message, acknowledged)
    VALUES (?, ?, ?, ?, ?, ?, 0)
    """, (ts, severity, sensor, value, threshold, message))
    
    alert_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return {
        "id": alert_id,
        "timestamp": ts,
        "severity": severity,
        "sensor": sensor,
        "value": value,
        "threshold": threshold,
        "message": message,
        "acknowledged": 0
    }

def get_telemetry_history(limit=100, mode=None):
    """Retrieves historical telemetry rows, ordered by timestamp ascending."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if mode:
        cursor.execute("""
        SELECT * FROM telemetry WHERE mode = ? ORDER BY id DESC LIMIT ?
        """, (mode, limit))
    else:
        cursor.execute("""
        SELECT * FROM telemetry ORDER BY id DESC LIMIT ?
        """, (limit,))
        
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    # Reverse so history starts chronological (oldest to newest)
    rows.reverse()
    return rows

def get_alerts(limit=50, severity=None, sensor=None, include_ack=True):
    """Retrieves list of alerts filtered by severity, sensor, and acknowledgment state."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = "SELECT * FROM alerts WHERE 1=1"
    params = []
    
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    if sensor:
        query += " AND sensor = ?"
        params.append(sensor)
    if not include_ack:
        query += " AND acknowledged = 0"
        
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, tuple(params))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def acknowledge_alert(alert_id: int):
    """Marks an alert event as acknowledged."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()

def clear_alerts():
    """Clears all logged alert events."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts")
    conn.commit()
    conn.close()
