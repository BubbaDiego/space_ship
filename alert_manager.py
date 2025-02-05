#!/usr/bin/env python
import os
import time
import json
import logging
import sqlite3
from typing import Dict, Any
from datetime import datetime
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

# ================================
# Logging Configuration
# ================================
logger = logging.getLogger("AlertManagerLogger")
logger.setLevel(logging.DEBUG)

# Console handler: use INFO level for concise output to console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# File handler: keep DEBUG level logs for detailed records
file_handler = logging.FileHandler("alert_manager_log.txt")
file_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

# ================================
# Twilio Helper Function
# ================================
def trigger_twilio_flow(custom_message: str, twilio_config: dict) -> str:
    """
    Trigger a Twilio Studio Flow execution with a custom message using JSON-based config.
    """
    account_sid = twilio_config.get("account_sid")
    auth_token = twilio_config.get("auth_token")
    flow_sid = twilio_config.get("flow_sid")
    to_phone = twilio_config.get("to_phone")
    from_phone = twilio_config.get("from_phone")

    logger.debug("Triggering Twilio Flow with message: '%s'", custom_message)
    logger.debug("TWILIO_CONFIG: account_sid=%s, flow_sid=%s, to=%s, from=%s",
                 account_sid, flow_sid, to_phone, from_phone)

    if not all([account_sid, auth_token, flow_sid, to_phone, from_phone]):
        raise ValueError("Missing one or more Twilio configuration variables.")

    client = Client(account_sid, auth_token)
    execution = client.studio.v2.flows(flow_sid).executions.create(
        to=to_phone,
        from_=from_phone,
        parameters={"custom_message": custom_message}
    )
    logger.debug("Twilio Flow execution created: SID=%s", execution.sid)
    return execution.sid

# ================================
# Helper for Alert Classification
# ================================
METRIC_DIRECTIONS = {
    "size": "increasing_bad",
}

def get_alert_class(value: float, low_thresh: float, med_thresh: float, high_thresh: float,
                    metric: str) -> str:
    """
    Determine the alert class based on thresholds and metric direction.
    """
    direction = METRIC_DIRECTIONS.get(metric, "increasing_bad")
    if direction == "increasing_bad":
        if value < low_thresh:
            return "alert-low"
        elif value < med_thresh:
            return "alert-medium"
        else:
            return "alert-high"
    elif direction == "decreasing_bad":
        if value > low_thresh:
            return "alert-low"
        elif value > med_thresh:
            return "alert-medium"
        else:
            return "alert-high"
    else:
        return "alert-low"

# ================================
# AlertManager Class with Added Support for Heat Index and Profit
# ================================
class AlertManager:
    ASSET_FULL_NAMES = {
        "BTC": "Bitcoin",
        "ETH": "Ethereum",
        "SOL": "Solana"
    }

    def __init__(self, db_path: str = "mother_brain.db", poll_interval: int = 60,
                 config_path: str = "sonic_config.json"):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.config_path = config_path

        logger.info("Initializing AlertManager...")
        logger.info("  DB Path       : %s", db_path)
        logger.info("  Poll Interval : %d seconds", poll_interval)
        logger.info("  Config Path   : %s", os.path.abspath(config_path))

        # Setup DataLocker and CalcServices (assumed available in your project)
        from data_locker import DataLocker
        from calc_services import CalcServices
        self.data_locker = DataLocker(self.db_path)
        self.calc_services = CalcServices()

        # Load configuration (merging JSON file with any DB overrides)
        from config_manager import load_config
        db_conn = self.data_locker.get_db_connection()
        self.config = load_config(self.config_path, db_conn)
        logger.info("Configuration: Alert Cooldown=%s sec, Call Refractory=%s sec",
                    self.config.get("alert_cooldown_seconds"),
                    self.config.get("call_refractory_period"))

        # Extract Twilio configuration (sensitive details hidden)
        self.twilio_config = self.config.get("twilio_config", {})
        logger.info("Twilio configuration: %s", "Present" if self.twilio_config else "Missing")

        # Global alert settings
        self.cooldown = self.config.get("alert_cooldown_seconds", 900)
        self.call_refractory_period = self.config.get("call_refractory_period", 3600)
        logger.info("Alert settings: Cooldown=%d sec, Refractory Period=%d sec",
                    self.cooldown, self.call_refractory_period)

        self.last_call_triggered: Dict[str, float] = {}
        self.monitor_enabled = self.config.get("system_config", {}).get("alert_monitor_enabled", True)
        self.last_triggered: Dict[str, float] = {}

        logger.info("AlertManager is ready.")

    def run(self):
        logger.info("Starting alert monitoring loop.")
        while True:
            self.check_alerts()
            time.sleep(self.poll_interval)

    def check_alerts(self):
        if not self.monitor_enabled:
            logger.info("Alert monitoring is disabled; skipping checks.")
            return

        positions = self.data_locker.read_positions()
        logger.info("Checking alerts for %d positions.", len(positions))
        for pos in positions:
            self.check_travel_percent_liquid(pos)
            self.check_heat_index(pos)
            self.check_profit(pos)
        self.check_price_alerts()

    def check_travel_percent_liquid(self, pos: Dict[str, Any]):
        pos_id = pos.get("id", "unknown")
        try:
            current_val = float(pos.get("current_travel_percent", 0.0))
        except Exception as e:
            logger.error("Pos %s: Error converting travel percent: %s", pos_id, e)
            return

        if current_val >= 0:
            logger.info("Pos %s: Travel percent (%.2f%%) is non-negative; no alert.", pos_id, current_val)
            return

        tpli_config = self.config.get("alert_ranges", {}).get("travel_percent_liquid_ranges", {})
        if not tpli_config.get("enabled", False):
            logger.info("Pos %s: Travel percent alerts disabled in configuration.", pos_id)
            return

        asset_code = pos.get("asset_type", "???").upper()
        asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
        position_type = pos.get("position_type", "").capitalize()
        wallet_name = pos.get("wallet_name", "Unknown")

        try:
            low = float(tpli_config.get("low", -25.0))
            medium = float(tpli_config.get("medium", -50.0))
            high = float(tpli_config.get("high", -75.0))
        except Exception as e:
            logger.error("Pos %s: Error parsing travel percent thresholds: %s", pos_id, e)
            return

        logger.info("Pos %s: Travel%%=%.2f%%; Thresholds: LOW=%.2f, MEDIUM=%.2f, HIGH=%.2f",
                    pos_id, current_val, low, medium, high)

        alert_level = None
        if current_val <= high:
            alert_level = "HIGH"
        elif current_val <= medium:
            alert_level = "MEDIUM"
        elif current_val <= low:
            alert_level = "LOW"
        else:
            logger.info("Pos %s: No travel percent threshold breached.", pos_id)
            return

        # Use the notifications mapping from the JSON
        if not tpli_config.get(alert_level.lower() + "_notifications", {}).get("call", False):
            logger.info("Pos %s: Travel percent call alert for level '%s' is disabled.", pos_id, alert_level)
            return

        key = f"{pos_id}-travel-{alert_level}"
        now = time.time()
        if now - self.last_triggered.get(key, 0) < self.cooldown:
            logger.info("Pos %s: Travel percent alert '%s' skipped (cooldown).", pos_id, alert_level)
            return

        self.last_triggered[key] = now
        msg = (f"Travel Percent Liquid ALERT: {asset_full} {position_type} (Wallet: {wallet_name}) "
               f"- Current Travel%% = {current_val:.2f}%, Level = {alert_level}")
        logger.info("Pos %s: %s", pos_id, msg)
        self.send_call(msg, key)

    def check_heat_index(self, pos: Dict[str, Any]):
        pos_id = pos.get("id", "unknown")
        try:
            current_heat = float(pos.get("heat_index", 0.0))
        except Exception as e:
            logger.error("Pos %s: Error converting heat index: %s", pos_id, e)
            return

        hi_config = self.config.get("alert_ranges", {}).get("heat_index_ranges", {})
        if not hi_config.get("enabled", False):
            logger.info("Pos %s: Heat index alerts disabled.", pos_id)
            return

        try:
            low = float(hi_config.get("low", 0.0))
            medium = float(hi_config.get("medium", 0.0))
            high = float(hi_config.get("high", 0.0))
        except Exception as e:
            logger.error("Pos %s: Error parsing heat index thresholds: %s", pos_id, e)
            return

        logger.info("Pos %s: Heat Index=%.2f; Thresholds: LOW=%.2f, MEDIUM=%.2f, HIGH=%.2f",
                    pos_id, current_heat, low, medium, high)

        alert_level = None
        # Assuming higher heat index values trigger alerts:
        if high != 0.0 and current_heat >= high:
            alert_level = "HIGH"
        elif medium != 0.0 and current_heat >= medium:
            alert_level = "MEDIUM"
        elif low != 0.0 and current_heat >= low:
            alert_level = "LOW"
        else:
            logger.info("Pos %s: Heat index %.2f does not trigger any alert.", pos_id, current_heat)
            return

        if not hi_config.get(alert_level.lower() + "_notifications", {}).get("call", False):
            logger.info("Pos %s: Heat index call alert for level '%s' is disabled.", pos_id, alert_level)
            return

        key = f"{pos_id}-heat-{alert_level}"
        now = time.time()
        if now - self.last_triggered.get(key, 0) < self.cooldown:
            logger.info("Pos %s: Heat index alert '%s' skipped (cooldown).", pos_id, alert_level)
            return

        self.last_triggered[key] = now
        msg = (f"Heat Index ALERT: Pos {pos_id} - Heat Index={current_heat:.2f}, Level={alert_level}")
        logger.info("Pos %s: %s", pos_id, msg)
        self.send_call(msg, key)

    def check_profit(self, pos: Dict[str, Any]):
        pos_id = pos.get("id", "unknown")
        try:
            profit_val = float(pos.get("profit", 0.0))
        except Exception as e:
            logger.error("Pos %s: Error converting profit: %s", pos_id, e)
            return

        profit_config = self.config.get("alert_ranges", {}).get("profit_ranges", {})
        if not profit_config.get("enabled", False):
            logger.info("Pos %s: Profit alerts disabled.", pos_id)
            return

        try:
            low = float(profit_config.get("low", 0.0))
            medium = float(profit_config.get("medium", 0.0))
            high = float(profit_config.get("high", 0.0))
        except Exception as e:
            logger.error("Pos %s: Error parsing profit thresholds: %s", pos_id, e)
            return

        logger.info("Pos %s: Profit=%.2f; Thresholds: LOW=%.2f, MEDIUM=%.2f, HIGH=%.2f",
                    pos_id, profit_val, low, medium, high)

        alert_level = None
        # For profit, assume lower profit is worse.
        if high != 0.0 and profit_val <= high:
            alert_level = "HIGH"
        elif medium != 0.0 and profit_val <= medium:
            alert_level = "MEDIUM"
        elif low != 0.0 and profit_val <= low:
            alert_level = "LOW"
        else:
            logger.info("Pos %s: Profit %.2f does not trigger any alert.", pos_id, profit_val)
            return

        if not profit_config.get(alert_level.lower() + "_notifications", {}).get("call", False):
            logger.info("Pos %s: Profit call alert for level '%s' is disabled.", pos_id, alert_level)
            return

        key = f"{pos_id}-profit-{alert_level}"
        now = time.time()
        if now - self.last_triggered.get(key, 0) < self.cooldown:
            logger.info("Pos %s: Profit alert '%s' skipped (cooldown).", pos_id, alert_level)
            return

        self.last_triggered[key] = now
        msg = (f"Profit ALERT: Pos {pos_id} - Profit={profit_val:.2f}, Level={alert_level}")
        logger.info("Pos %s: %s", pos_id, msg)
        self.send_call(msg, key)

    def check_price_alerts(self):
        alerts = self.data_locker.get_alerts()
        price_alerts = [
            a for a in alerts
            if a.get("alert_type") == "PRICE_THRESHOLD" and a.get("status", "").lower() == "active"
        ]
        logger.info("Found %d active price alerts.", len(price_alerts))
        for alert in price_alerts:
            asset_code = alert.get("asset_type", "BTC").upper()
            asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
            try:
                trigger_val = float(alert.get("trigger_value", 0.0))
            except Exception:
                trigger_val = 0.0
            condition = alert.get("condition", "ABOVE").upper()
            price_info = self.data_locker.get_latest_price(asset_code)
            if not price_info:
                logger.info("Asset %s: No latest price info available.", asset_full)
                continue
            current_price = float(price_info.get("current_price", 0.0))
            logger.info("Asset %s: Price=%.2f, Trigger=%.2f, Condition=%s",
                        asset_full, current_price, trigger_val, condition)
            if (condition == "ABOVE" and current_price >= trigger_val) or \
               (condition != "ABOVE" and current_price <= trigger_val):
                self.handle_price_alert_trigger(alert, current_price, asset_full)
            else:
                logger.info("Asset %s: No price alert triggered.", asset_full)

    def handle_price_alert_trigger(self, alert: dict, current_price: float, asset_full: str):
        key = f"price-alert-{asset_full}"
        now = time.time()
        if now - self.last_triggered.get(key, 0) < self.cooldown:
            logger.info("Price alert for '%s' skipped (cooldown).", asset_full)
            return

        self.last_triggered[key] = now
        cond = alert.get("condition", "ABOVE").upper()
        try:
            trig_val = float(alert.get("trigger_value", 0.0))
        except Exception:
            trig_val = 0.0
        position_type = alert.get("position_type", "").capitalize()
        wallet_name = alert.get("wallet_name", "Unknown")
        msg = (f"Price ALERT: {asset_full} {position_type}" +
               (f", Wallet: {wallet_name}" if wallet_name != "Unknown" else "") +
               f" - Condition: {cond}, Trigger: {trig_val}, Current: {current_price}")
        logger.info("Price Alert: %s", msg)
        self.send_call(msg, key)

    def send_call(self, body: str, key: str):
        now = time.time()
        if now - self.last_call_triggered.get(key, 0) < self.call_refractory_period:
            logger.info("Call alert for '%s' suppressed (refractory period active).", key)
            return
        try:
            execution_sid = trigger_twilio_flow(body, self.twilio_config)
            self.last_call_triggered[key] = now
            logger.info("Call alert sent for '%s'; Execution SID: %s", key, execution_sid)
            return execution_sid
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info("Call alert for '%s' already active; new call suppressed.", key)
                self.last_call_triggered[key] = now
                return None
            else:
                logger.error("Twilio error for '%s': %s", key, e, exc_info=True)
                return None
        except Exception as e:
            logger.error("Error sending call for '%s': %s", key, e, exc_info=True)
            return None

def load_json_config(json_path: str) -> dict:
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.debug("Configuration loaded from: %s", os.path.abspath(json_path))
        return config
    except FileNotFoundError:
        logger.warning("Config file '%s' not found. Returning empty dict.", json_path)
        return {}
    except json.JSONDecodeError as e:
        logger.error("Error parsing JSON from '%s': %s", json_path, e)
        return {}

def save_config(config: dict, json_path: str):
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        logger.debug("Configuration saved to: %s", os.path.abspath(json_path))
    except Exception as e:
        logger.error("Error saving configuration to '%s': %s", json_path, e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    manager = AlertManager(
        db_path=os.path.abspath("mother_brain.db"),
        poll_interval=60,
        config_path="sonic_config.json"
    )
    manager.run()
