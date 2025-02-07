#!/usr/bin/env python
import os
import time
import json
import logging
import sqlite3
from typing import Dict, Any, List
from datetime import datetime
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

# ================================
# Logging Configuration
# ================================
logger = logging.getLogger("AlertManagerLogger")
logger.setLevel(logging.DEBUG)

# Console handler: DEBUG level for detailed output
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)

# File handler: DEBUG level for detailed records
file_handler = logging.FileHandler("alert_manager_log.txt")
file_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

# ================================
# Twilio Helper Function (Simplified)
# ================================
def trigger_twilio_flow(custom_message: str, twilio_config: dict) -> str:
    """
    Trigger a Twilio Studio Flow execution with a custom message.
    """
    account_sid = twilio_config.get("account_sid")
    auth_token = twilio_config.get("auth_token")
    flow_sid = twilio_config.get("flow_sid")
    to_phone = twilio_config.get("to_phone")
    from_phone = twilio_config.get("from_phone")

    if not all([account_sid, auth_token, flow_sid, to_phone, from_phone]):
        raise ValueError("Missing one or more Twilio configuration variables.")

    client = Client(account_sid, auth_token)
    execution = client.studio.v2.flows(flow_sid).executions.create(
        to=to_phone,
        from_=from_phone,
        parameters={"custom_message": custom_message}
    )
    logger.debug("Twilio flow triggered with message: %s", custom_message)
    logger.info("Twilio alert sent.")
    return execution.sid

# ================================
# Helper for Alert Classification
# ================================
METRIC_DIRECTIONS = {
    "size": "increasing_bad",
}

def get_alert_class(value: float, low_thresh: float, med_thresh: float, high_thresh: float,
                    metric: str) -> str:
    direction = METRIC_DIRECTIONS.get(metric, "increasing_bad")
    logger.debug("get_alert_class: value=%f, thresholds=(low=%f, med=%f, high=%f), direction=%s",
                 value, low_thresh, med_thresh, high_thresh, direction)
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
# AlertManager Class with Aggregated Alerts and Extensive Debug Logging
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
        # For state latching profit alert levels per position; stored as strings.
        self.last_profit: Dict[str, str] = {}
        self.last_triggered: Dict[str, float] = {}
        self.last_call_triggered: Dict[str, float] = {}

        logger.debug("Initializing AlertManager with db_path=%s, poll_interval=%d, config_path=%s",
                     db_path, poll_interval, os.path.abspath(config_path))

        # Import dependencies from your project.
        from data_locker import DataLocker
        from calc_services import CalcServices
        self.data_locker = DataLocker(self.db_path)
        self.calc_services = CalcServices()

        from config_manager import load_config
        db_conn = self.data_locker.get_db_connection()
        self.config = load_config(self.config_path, db_conn)
        logger.debug("Loaded configuration: %s", json.dumps(self.config, indent=2))
        logger.info("Configuration: Alert Cooldown=%s sec, Call Refractory=%s sec",
                    self.config.get("alert_cooldown_seconds"),
                    self.config.get("call_refractory_period"))

        self.twilio_config = self.config.get("twilio_config", {})
        logger.info("Twilio configuration: %s", "Present" if self.twilio_config else "Missing")

        self.cooldown = self.config.get("alert_cooldown_seconds", 900)
        self.call_refractory_period = self.config.get("call_refractory_period", 3600)
        logger.info("Alert settings: Cooldown=%d sec, Refractory Period=%d sec",
                    self.cooldown, self.call_refractory_period)

        self.monitor_enabled = self.config.get("system_config", {}).get("alert_monitor_enabled", True)
        logger.info("AlertManager is ready.")

    def reload_config(self):
        """
        Reload configuration from the JSON file to update alert thresholds.
        """
        from config_manager import load_config
        db_conn = self.data_locker.get_db_connection()
        self.config = load_config(self.config_path, db_conn)
        self.cooldown = self.config.get("alert_cooldown_seconds", 900)
        self.call_refractory_period = self.config.get("call_refractory_period", 3600)
        logger.debug("Reloaded configuration: %s", json.dumps(self.config, indent=2))
        logger.info("Alert configuration reloaded: Cooldown=%d sec, Refractory Period=%d sec",
                    self.cooldown, self.call_refractory_period)

    def run(self):
        logger.info("Starting alert monitoring loop.")
        while True:
            self.check_alerts()
            time.sleep(self.poll_interval)

    def check_alerts(self):
        if not self.monitor_enabled:
            logger.info("Alert monitoring disabled; skipping checks.")
            return

        aggregated_alerts: List[str] = []

        positions = self.data_locker.read_positions()
        logger.debug("Fetched positions: %s", json.dumps(positions, indent=2))
        logger.info("Checking alerts for %d positions.", len(positions))
        for pos in positions:
            profit_alert = self.check_profit(pos)
            if profit_alert:
                logger.debug("Profit alert triggered: %s", profit_alert)
                aggregated_alerts.append(profit_alert)
            travel_alert = self.check_travel_percent_liquid(pos)
            if travel_alert:
                logger.debug("Travel alert triggered: %s", travel_alert)
                aggregated_alerts.append(travel_alert)

        price_alerts = self.check_price_alerts()
        aggregated_alerts.extend(price_alerts)
        logger.debug("Aggregated alerts: %s", aggregated_alerts)

        if aggregated_alerts:
            message = f"{len(aggregated_alerts)} alerts triggered:\n" + "\n".join(aggregated_alerts)
            logger.debug("Sending aggregated alert call with message: %s", message)
            self.send_call(message, "aggregated-alert")
        else:
            logger.info("No alerts triggered in this cycle.")

    def check_travel_percent_liquid(self, pos: Dict[str, Any]) -> str:
        asset_code = pos.get("asset_type", "???").upper()
        asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
        position_type = pos.get("position_type", "").capitalize()
        position_id = pos.get("position_id") or pos.get("id") or "unknown"
        try:
            current_val = float(pos.get("current_travel_percent", 0.0))
        except Exception as e:
            logger.error("%s %s (ID: %s): Error converting travel percent: %s", asset_full, position_type, position_id, e)
            return ""

        logger.debug("%s %s (ID: %s): current_travel_percent = %f", asset_full, position_type, position_id, current_val)

        if current_val >= 0:
            logger.debug("%s %s (ID: %s): current_travel_percent is non-negative; no alert.", asset_full, position_type, position_id)
            return ""

        tpli_config = self.config.get("alert_ranges", {}).get("travel_percent_liquid_ranges", {})
        logger.debug("%s %s (ID: %s): travel_percent_liquid config: %s", asset_full, position_type, position_id, tpli_config)
        if not tpli_config.get("enabled", False):
            logger.debug("%s %s (ID: %s): Travel alert disabled in config.", asset_full, position_type, position_id)
            return ""

        try:
            low = float(tpli_config.get("low", -25.0))
            medium = float(tpli_config.get("medium", -50.0))
            high = float(tpli_config.get("high", -75.0))
        except Exception as e:
            logger.error("%s %s (ID: %s): Error parsing travel percent thresholds: %s", asset_full, position_type, position_id, e)
            return ""

        logger.debug("%s %s (ID: %s): Thresholds - Low: %f, Medium: %f, High: %f", asset_full, position_type, position_id, low, medium, high)

        alert_level = ""
        if current_val <= high:
            alert_level = "HIGH"
        elif current_val <= medium:
            alert_level = "MEDIUM"
        elif current_val <= low:
            alert_level = "LOW"
        else:
            logger.debug("%s %s (ID: %s): current_travel_percent does not breach any thresholds.", asset_full, position_type, position_id)
            return ""

        key = f"{asset_full}-{position_type}-{position_id}-travel-{alert_level}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        if now - last_time < self.cooldown:
            logger.info("%s %s (ID: %s): Travel alert '%s' suppressed due to cooldown. (Elapsed: %f sec)", asset_full, position_type, position_id, alert_level, now - last_time)
            return ""
        self.last_triggered[key] = now
        wallet_name = pos.get("wallet_name", "Unknown")
        msg = (f"Travel Percent Liquid ALERT: {asset_full} {position_type} (Wallet: {wallet_name}) - "
               f"Travel% = {current_val:.2f}%, Level = {alert_level}")
        logger.debug("%s %s (ID: %s): Triggering travel alert with message: %s", asset_full, position_type, position_id, msg)
        return msg

    def check_profit(self, pos: Dict[str, Any]) -> str:
        asset_code = pos.get("asset_type", "???").upper()
        asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
        position_type = pos.get("position_type", "").capitalize()
        position_id = pos.get("position_id") or pos.get("id") or "unknown"
        raw_profit = pos.get("profit")
        try:
            profit_val = float(raw_profit) if raw_profit is not None else 0.0
        except Exception as e:
            logger.error("%s %s (ID: %s): Error converting profit: %s", asset_full, position_type, position_id, e)
            return ""

        if profit_val == 0.0:
            try:
                computed_profit = float(pos.get("value", 0)) - float(pos.get("collateral", 0))
                profit_val = computed_profit
            except Exception as e:
                logger.error("%s %s (ID: %s): Error computing fallback profit: %s", asset_full, position_type, position_id, e)
                return ""

        logger.debug("%s %s (ID: %s): profit_val = %f", asset_full, position_type, position_id, profit_val)

        profit_config = self.config.get("alert_ranges", {}).get("profit_ranges", {})
        logger.debug("%s %s (ID: %s): profit config: %s", asset_full, position_type, position_id, profit_config)
        if not profit_config.get("enabled", False):
            logger.debug("%s %s (ID: %s): Profit alert disabled in config.", asset_full, position_type, position_id)
            return ""

        try:
            low_thresh = float(profit_config.get("low", 0.0))
            med_thresh = float(profit_config.get("medium", 0.0))
            high_thresh = float(profit_config.get("high", 0.0))
        except Exception as e:
            logger.error("%s %s (ID: %s): Error parsing profit thresholds: %s", asset_full, position_type, position_id, e)
            return ""

        logger.debug("%s %s (ID: %s): Profit thresholds - Low: %f, Medium: %f, High: %f",
                     asset_full, position_type, position_id, low_thresh, med_thresh, high_thresh)

        if profit_val < low_thresh:
            logger.debug("%s %s (ID: %s): profit_val below low_thresh; no alert.", asset_full, position_type, position_id)
            return ""
        elif profit_val < med_thresh:
            current_level = "low"
        elif profit_val < high_thresh:
            current_level = "medium"
        else:
            current_level = "high"

        profit_key = f"profit-{asset_full}-{position_type}-{position_id}"
        last_level = self.last_profit.get(profit_key, "none")
        level_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
        logger.debug("%s %s (ID: %s): Profit alert levels - last: %s, current: %s",
                     asset_full, position_type, position_id, last_level, current_level)
        if level_order[current_level] <= level_order[last_level]:
            self.last_profit[profit_key] = current_level
            logger.debug("%s %s (ID: %s): No upward breach in profit levels; no alert triggered.", asset_full, position_type, position_id)
            return ""

        now = time.time()
        last_time = self.last_triggered.get(profit_key, 0)
        if now - last_time < self.cooldown:
            self.last_profit[profit_key] = current_level
            logger.info("%s %s (ID: %s): Profit alert suppressed due to cooldown. (Elapsed: %f sec)",
                        asset_full, position_type, position_id, now - last_time)
            return ""
        self.last_triggered[profit_key] = now
        msg = (f"Profit ALERT: {asset_full} {position_type} profit of {profit_val:.2f} "
               f"(Level: {current_level.upper()})")
        logger.debug("%s %s (ID: %s): Triggering profit alert with message: %s",
                     asset_full, position_type, position_id, msg)
        self.last_profit[profit_key] = current_level
        return msg

    def check_price_alerts(self) -> List[str]:
        alerts = self.data_locker.get_alerts()
        messages: List[str] = []
        price_alerts = [
            a for a in alerts
            if a.get("alert_type") == "PRICE_THRESHOLD" and a.get("status", "").lower() == "active"
        ]
        logger.info("Found %d active price alerts.", len(price_alerts))
        for alert in price_alerts:
            logger.debug("Processing price alert: %s", json.dumps(alert, indent=2))
            asset_code = alert.get("asset_type", "BTC").upper()
            asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
            position_id = alert.get("position_id") or alert.get("id") or "unknown"
            try:
                trigger_val = float(alert.get("trigger_value", 0.0))
            except Exception:
                trigger_val = 0.0
            condition = alert.get("condition", "ABOVE").upper()
            price_info = self.data_locker.get_latest_price(asset_code)
            if not price_info:
                logger.debug("%s: No price info found; skipping alert.", asset_full)
                continue
            current_price = float(price_info.get("current_price", 0.0))
            logger.debug("%s: Condition: %s, Trigger: %f, Current Price: %f", asset_full, condition, trigger_val, current_price)
            if (condition == "ABOVE" and current_price >= trigger_val) or (condition != "ABOVE" and current_price <= trigger_val):
                msg = self.handle_price_alert_trigger(alert, current_price, asset_full)
                if msg:
                    messages.append(msg)
        return messages

    def handle_price_alert_trigger(self, alert: dict, current_price: float, asset_full: str) -> str:
        position_id = alert.get("position_id") or alert.get("id") or "unknown"
        key = f"price-alert-{asset_full}-{position_id}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        if now - last_time < self.cooldown:
            logger.info("%s: Price alert suppressed due to cooldown. (Elapsed: %f sec)", asset_full, now - last_time)
            return ""
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
        logger.debug("%s: Triggering price alert with message: %s", asset_full, msg)
        return msg

    def send_call(self, body: str, key: str):
        now = time.time()
        last_call_time = self.last_call_triggered.get(key, 0)
        if now - last_call_time < self.call_refractory_period:
            logger.info("Call alert for '%s' suppressed due to refractory period. (Elapsed: %f sec)", key, now - last_call_time)
            return
        try:
            logger.debug("Sending call alert with message: %s", body)
            trigger_twilio_flow(body, self.twilio_config)
            self.last_call_triggered[key] = now
        except Exception as e:
            logger.error("Error sending call for '%s': %s", key, e, exc_info=True)

# ================================
# Configuration Helpers
# ================================
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

# ================================
# Main Execution
# ================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    manager = AlertManager(
        db_path=os.path.abspath("mother_brain.db"),
        poll_interval=60,
        config_path="sonic_config.json"
    )
    manager.run()
