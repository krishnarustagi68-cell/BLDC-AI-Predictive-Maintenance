# Central Configuration Settings
# File: python/config.py

import os

# Database Path
DB_PATH = os.getenv("DB_PATH", "motor_telemetry.db")

# Serial Port Defaults
DEFAULT_PORT = "COM4"
DEFAULT_BAUD = 115200
STARTUP_WAIT = 14  # Seconds to wait for ESC arm + motor ramp

# Models Directory
MODELS_DIR = os.getenv("MODELS_DIR", os.path.join("..", "models"))

# Engineering Units Configuration
UNITS = {
    "Temperature": "°C",
    "VibrationX": "g",
    "VibrationY": "g",
    "VibrationZ": "g",
    "Current": "A",
    "Voltage": "V",
    "RPM": "rpm",
    "MagX": "G",
    "MagY": "G",
    "MagZ": "G",
}

# Sensor Safe Ranges & Action Thresholds
# Limits are based on your real physical telemetry dataset profiles
THRESHOLDS = {
    "Temperature": {
        "warning": 45.0,     # °C
        "critical": 50.0,    # °C
        "low_limit": 15.0    # °C
    },
    "VibrationZ": {
        "warning": 0.05,     # g (Normal is typically < 0.02g)
        "critical": 0.35,    # g (Severe misalignment or structural looseness)
    },
    "VibrationX": {
        "warning": 0.0085,   # g
        "critical": 0.015,   # g
    },
    "Current": {
        "warning": 0.800,    # A (Load warning)
        "critical": 1.800,   # A (Overcurrent/Rotor lock protection)
    },
    "Voltage": {
        "warning": 7.0,      # V (Low battery warnings for 2S LiPo)
        "critical": 6.6,     # V (Danger zone - cell damage risk)
    },
    "RPM": {
        "warning": 1290,     # rpm (Underload speed warning)
        "critical": 1250,    # rpm (Severe mechanical drag)
    },
    "MagZ": {
        "warning": -0.125,   # G (Magnetic field alignment warning)
        "critical": -0.090,  # G (Critical magnetic field drift)
    }
}
