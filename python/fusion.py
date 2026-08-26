# Diagnostic Consensus Fusion & Anomaly Engine
# File: python/fusion.py

import logging
from config import THRESHOLDS

logger = logging.getLogger("bldc.fusion")

class DiagnosticFusionEngine:
    @staticmethod
    def calculate_health_score(d: dict) -> int:
        """
        Calculates a physical health score (0-100) based on continuous sensor deviations
        exceeding warning thresholds towards critical boundaries.
        """
        health = 100.0
        
        # 1. Temperature Deduction (Max -30 pts)
        temp = d.get("Temperature", 20.0)
        t_warn = THRESHOLDS["Temperature"]["warning"]
        t_crit = THRESHOLDS["Temperature"]["critical"]
        if temp > t_warn:
            deduction = ((temp - t_warn) / (t_crit - t_warn)) * 30.0
            health -= min(30.0, deduction)
            
        # 2. Vibration Z Deduction (Max -30 pts)
        vz = abs(d.get("VibrationZ", 0.0))
        v_warn = THRESHOLDS["VibrationZ"]["warning"]
        v_crit = THRESHOLDS["VibrationZ"]["critical"]
        if vz > v_warn:
            deduction = ((vz - v_warn) / (v_crit - v_warn)) * 30.0
            health -= min(30.0, deduction)

        # 3. Vibration X Deduction (Max -10 pts)
        vx = abs(d.get("VibrationX", 0.0))
        vx_warn = THRESHOLDS["VibrationX"]["warning"]
        vx_crit = THRESHOLDS["VibrationX"]["critical"]
        if vx > vx_warn:
            deduction = ((vx - vx_warn) / (vx_crit - vx_warn)) * 10.0
            health -= min(10.0, deduction)

        # 4. Current Deduction (Max -20 pts)
        curr = d.get("Current", 0.0)
        c_warn = THRESHOLDS["Current"]["warning"]
        c_crit = THRESHOLDS["Current"]["critical"]
        if curr > c_warn:
            deduction = ((curr - c_warn) / (c_crit - c_warn)) * 20.0
            health -= min(20.0, deduction)

        # 5. Voltage Drop Deduction (Max -10 pts)
        volt = d.get("Voltage", 8.2)
        volt_warn = THRESHOLDS["Voltage"]["warning"]
        volt_crit = THRESHOLDS["Voltage"]["critical"]
        if volt < volt_warn and volt > 1.0: # ignore 0V if completely off
            deduction = ((volt_warn - volt) / (volt_warn - volt_crit)) * 10.0
            health -= min(10.0, deduction)

        # 6. RPM Underload Deduction (Max -10 pts)
        rpm = d.get("RPM", 0.0)
        rpm_warn = THRESHOLDS["RPM"]["warning"]
        rpm_crit = THRESHOLDS["RPM"]["critical"]
        if 10.0 < rpm < rpm_warn:
            deduction = ((rpm_warn - rpm) / (rpm_warn - rpm_crit)) * 10.0
            health -= min(10.0, deduction)
            
        return max(0, min(100, int(round(health))))

    @staticmethod
    def detect_anomalies(d: dict, history: list) -> dict:
        """
        Runs statistical and logical anomaly checks on the current packet.
        Returns a dict: {'detected': bool, 'message': str, 'severity': str}
        """
        temp = d.get("Temperature", 0.0)
        vz = abs(d.get("VibrationZ", 0.0))
        curr = d.get("Current", 0.0)
        rpm = d.get("RPM", 0.0)
        volt = d.get("Voltage", 0.0)
        
        # 1. Thermal Rate-of-Change Anomaly
        if len(history) >= 3:
            prev_t = history[-1].get("sensors", {}).get("temperature", temp)
            dT_dt = temp - prev_t
            if dT_dt > 1.5:  # Temperature jumped > 1.5°C in one reading cycle (usually 1-2s)
                return {
                    "detected": True,
                    "sensor": "Temperature",
                    "value": temp,
                    "message": f"Thermal shock anomaly: Sudden temperature jump of +{dT_dt:.2f}°C/sec",
                    "severity": "CRITICAL",
                    "action": "Reduce motor load immediately and check cooling efficiency."
                }

        # 2. Sensor Dropout Anomaly
        if rpm > 100.0 and curr == 0.0:
            return {
                "detected": True,
                "sensor": "Current",
                "value": curr,
                "message": "Current sensor dropout anomaly: Motor spinning but current reads 0.0A",
                "severity": "WARNING",
                "action": "Check INA219 sensor connections and I2C SDA1/SCL1 wiring."
            }

        # 3. Vibration Instability Anomaly
        if vz > 0.4:
            return {
                "detected": True,
                "sensor": "VibrationZ",
                "value": vz,
                "message": f"Severe vibration anomaly: Z-axis vibration spikes to {vz:.3f}g",
                "severity": "CRITICAL",
                "action": "Shut down the motor. Inspect shaft coupling alignment and tighten engine mounts."
            }
            
        # 4. Under-voltage Anomaly
        if 0.5 < volt < THRESHOLDS["Voltage"]["critical"]:
            return {
                "detected": True,
                "sensor": "Voltage",
                "value": volt,
                "message": f"Voltage critical anomaly: LiPo voltage dropped to {volt:.1f}V (under safe cell limits)",
                "severity": "CRITICAL",
                "action": "Unplug battery immediately to avoid cell swelling and permanent damage."
            }

        return {"detected": False}

    @staticmethod
    def fuse_diagnostics(km_pred: str, lr_pred: str, dt_pred: str, rule_pred: str) -> dict:
        """
        Combines model predictions using consensus voting. Rules-based results act
        as a hard override for safety states (e.g. Motor OFF).
        """
        # Hard override for Motor OFF state
        if rule_pred == "Motor OFF":
            return {
                "final_prediction": "Motor OFF",
                "confidence": 100,
                "consensus_state": "Consensus",
                "explanation": "Motor is stationary (RPM < 10). Hardcoded rule override activated.",
                "recommendation": "System standby. Ensure physical parameters are safe before arming throttle."
            }
            
        # Model Voting List
        votes = [km_pred, lr_pred, dt_pred, rule_pred]
        
        # Calculate counts
        counts = {}
        for v in votes:
            if v and v != "—":
                counts[v] = counts.get(v, 0) + 1
                
        if not counts:
            return {
                "final_prediction": "Unknown",
                "confidence": 0,
                "consensus_state": "No Data",
                "explanation": "No diagnostic outputs are available.",
                "recommendation": "Ensure data collection is active."
            }
            
        # Find maximum voted class
        max_vote = max(counts, key=counts.get)
        vote_count = counts[max_vote]
        total_votes = sum(counts.values())
        
        # Confidence derived from consensus ratio
        confidence = int((vote_count / total_votes) * 100)
        
        # Decide consensus state
        if vote_count == total_votes:
            consensus = "Consensus"
        elif vote_count >= 2:
            consensus = "Majority"
        else:
            consensus = "Disagreement"
            # Tie breaker: Default to Decision Tree prediction which holds highest validation accuracy
            max_vote = dt_pred if dt_pred in counts else max_vote
            
        # Build explanation
        agreed_models = [name for name, pred in [("K-Means", km_pred), ("Logistic Regression", lr_pred), ("Decision Tree", dt_pred), ("Rule-based", rule_pred)] if pred == max_vote]
        disagreed_models = [name for name, pred in [("K-Means", km_pred), ("Logistic Regression", lr_pred), ("Decision Tree", dt_pred), ("Rule-based", rule_pred)] if pred != max_vote]
        
        explanation = f"Class '{max_vote}' selected by {len(agreed_models)} models ({', '.join(agreed_models)})."
        if disagreed_models:
            explanation += f" Disagreement from: {', '.join(disagreed_models)}."
            
        # Recommendations
        recs = {
            "Normal": "System healthy. Monitor live data feeds.",
            "Overheating": "Possible thermal overload. Check cooling vents and reduce throttle level.",
            "Misalignment": "Possible mechanical misalignment. Check shaft coupling and tighten mounting fasteners.",
            "Overload": "High mechanical load. Check for mechanical drag or rotor friction."
        }
        rec = recs.get(max_vote, "Monitor system parameters.")
        
        return {
            "final_prediction": max_vote,
            "confidence": confidence,
            "consensus_state": consensus,
            "explanation": explanation,
            "recommendation": rec
        }
