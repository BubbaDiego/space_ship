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

# Console handler: INFO level for concise output to console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

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
# AlertManager Class with Debug Logging
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

        logger.info("Initializing AlertManager...")
        logger.info("  DB Path       : %s", db_path)
        logger.info("  Poll Interval : %d seconds", poll_interval)
        logger.info("  Config Path   : %s", os.path.abspath(config_path))

        # Import dependencies from your project.
        from data_locker import DataLocker
        from calc_services import CalcServices
        self.data_locker = DataLocker(self.db_path)
        self.calc_services = CalcServices()

        from config_manager import load_config
        db_conn = self.data_locker.get_db_connection()
        self.config = load_config(self.config_path, db_conn)
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

    def run(self):
        logger.info("Starting alert monitoring loop.")
        while True:
            self.check_alerts()
            time.sleep(self.poll_interval)

    def check_alerts(self):
        if not self.monitor_enabled:
            logger.info("Alert monitoring disabled; skipping checks.")
            return

        positions = self.data_locker.read_positions()
        logger.info("Checking alerts for %d positions.", len(positions))
        for pos in positions:
            self.check_travel_percent_liquid(pos)
            self.check_profit(pos)
        self.check_price_alerts()

    def check_travel_percent_liquid(self, pos: Dict[str, Any]):
        asset_code = pos.get("asset_type", "???").upper()
        asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
        position_type = pos.get("position_type", "").capitalize()
        try:
            current_val = float(pos.get("current_travel_percent", 0.0))
        except Exception as e:
            logger.error("Error converting travel percent for %s %s: %s", asset_full, position_type, e)
            return

        logger.debug("%s %s: current_travel_percent = %.2f", asset_full, position_type, current_val)
        if current_val >= 0:
            logger.debug("%s %s: No alert, travel percent non-negative.", asset_full, position_type)
            return

        tpli_config = self.config.get("alert_ranges", {}).get("travel_percent_liquid_ranges", {})
        if not tpli_config.get("enabled", False):
            logger.debug("%s %s: Travel percent alerts disabled.", asset_full, position_type)
            return

        try:
            low = float(tpli_config.get("low", -25.0))
            medium = float(tpli_config.get("medium", -50.0))
            high = float(tpli_config.get("high", -75.0))
        except Exception as e:
            logger.error("Error parsing travel percent thresholds for %s %s: %s", asset_full, position_type, e)
            return

        logger.debug("%s %s: Thresholds -> low: %.2f, medium: %.2f, high: %.2f", asset_full, position_type, low, medium, high)

        alert_level = None
        if current_val <= high:
            alert_level = "HIGH"
        elif current_val <= medium:
            alert_level = "MEDIUM"
        elif current_val <= low:
            alert_level = "LOW"
        else:
            logger.debug("%s %s: Travel percent %.2f does not breach any threshold.", asset_full, position_type, current_val)
            return

        logger.debug("%s %s: Determined travel percent alert level: %s", asset_full, position_type, alert_level)
        key = f"{asset_full}-{position_type}-travel-{alert_level}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        logger.debug("Alert key '%s' last triggered at %.2f; now=%.2f; delta=%.2f", key, last_time, now, now - last_time)
        if now - last_time < self.cooldown:
            logger.debug("%s %s: Alert '%s' skipped due to cooldown.", asset_full, position_type, alert_level)
            return

        self.last_triggered[key] = now
        wallet_name = pos.get("wallet_name", "Unknown")
        msg = (f"Travel Percent Liquid ALERT: {asset_full} {position_type} (Wallet: {wallet_name}) - "
               f"Current Travel% = {current_val:.2f}%, Level = {alert_level}")
        logger.info("%s %s: Triggering alert: %s", asset_full, position_type, msg)
        self.send_call(msg, key)

    def check_profit(self, pos: Dict[str, Any]):
        asset_code = pos.get("asset_type", "???").upper()
        asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
        position_type = pos.get("position_type", "").capitalize()
        raw_profit = pos.get("profit")
        logger.debug("%s %s: Raw profit = %s", asset_full, position_type, raw_profit)
        try:
            profit_val = float(raw_profit) if raw_profit is not None else 0.0
        except Exception as e:
            logger.error("Error converting profit for %s %s: %s", asset_full, position_type, e)
            return

        logger.debug("%s %s: Converted profit = %.2f", asset_full, position_type, profit_val)
        if profit_val == 0.0:
            try:
                computed_profit = float(pos.get("value", 0)) - float(pos.get("collateral", 0))
                logger.debug("%s %s: Computed fallback profit = %.2f", asset_full, position_type, computed_profit)
                profit_val = computed_profit
            except Exception as e:
                logger.error("Error computing fallback profit for %s %s: %s", asset_full, position_type, e)
                return

        profit_config = self.config.get("alert_ranges", {}).get("profit_ranges", {})
        if not profit_config.get("enabled", False):
            logger.debug("Profit alerts disabled in config.")
            return

        try:
            low_thresh = float(profit_config.get("low", 0.0))
            med_thresh = float(profit_config.get("medium", 0.0))
            high_thresh = float(profit_config.get("high", 0.0))
        except Exception as e:
            logger.error("Error parsing profit thresholds for %s %s: %s", asset_full, position_type, e)
            return

        logger.debug("%s %s: Profit thresholds -> low: %.2f, med: %.2f, high: %.2f", asset_full, position_type, low_thresh, med_thresh, high_thresh)
        logger.debug("%s %s: Profit value = %.2f", asset_full, position_type, profit_val)

        current_level = "none"
        if profit_val >= high_thresh and high_thresh > 0:
            current_level = "high"
        elif profit_val >= med_thresh and med_thresh > 0:
            current_level = "medium"
        elif profit_val >= low_thresh and low_thresh > 0:
            current_level = "low"
        else:
            logger.debug("%s %s: Profit %.2f does not trigger any threshold.", asset_full, position_type, profit_val)
            self.last_profit[asset_full + position_type] = "none"
            return

        logger.debug("%s %s: Profit alert level determined: %s", asset_full, position_type, current_level)
        last_level = self.last_profit.get(asset_full + position_type, "none")
        logger.debug("%s %s: Last profit alert level = %s", asset_full, position_type, last_level)
        level_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
        if level_order[current_level] <= level_order[last_level]:
            logger.debug("%s %s: No upward transition in profit level (current: %s, last: %s); skipping alert.", asset_full, position_type, current_level, last_level)
            self.last_profit[asset_full + position_type] = current_level
            return

        key = f"profit-{asset_full}-{position_type}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        logger.debug("Profit alert key '%s' last triggered at %.2f; now=%.2f; delta=%.2f", key, last_time, now, now - last_time)
        if now - last_time < self.cooldown:
            logger.debug("%s %s: Profit alert skipped due to cooldown.", asset_full, position_type)
            self.last_profit[asset_full + position_type] = current_level
            return

        self.last_triggered[key] = now
        msg = (f"Profit ALERT: {asset_full} {position_type} profit of {profit_val:.2f} "
               f"(Level: {current_level.upper()}).")
        logger.info("%s %s: Triggering profit alert: %s", asset_full, position_type, msg)
        self.send_call(msg, key)
        self.last_profit[asset_full + position_type] = current_level

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
                logger.debug("No latest price info for asset %s.", asset_full)
                continue
            current_price = float(price_info.get("current_price", 0.0))
            logger.debug("%s: current_price=%.2f, trigger_val=%.2f, condition=%s", asset_full, current_price, trigger_val, condition)
            if (condition == "ABOVE" and current_price >= trigger_val) or (condition != "ABOVE" and current_price <= trigger_val):
                self.handle_price_alert_trigger(alert, current_price, asset_full)
            else:
                logger.debug("%s: Price alert condition not met (current: %.2f, trigger: %.2f, condition: %s)", asset_full, current_price, trigger_val, condition)

    def handle_price_alert_trigger(self, alert: dict, current_price: float, asset_full: str):
        key = f"price-alert-{asset_full}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        logger.debug("Price alert key '%s' last triggered at %.2f; now=%.2f; delta=%.2f", key, last_time, now, now - last_time)
        if now - last_time < self.cooldown:
            logger.info("Price alert for '%s' skipped due to cooldown.", asset_full)
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
        logger.info("Triggering price alert: %s", msg)
        self.send_call(msg, key)

    def send_call(self, body: str, key: str):
        now = time.time()
        last_call_time = self.last_call_triggered.get(key, 0)
        logger.debug("Call alert key '%s' last triggered at %.2f; now=%.2f; delta=%.2f", key, last_call_time, now, now - last_call_time)
        if now - last_call_time < self.call_refractory_period:
            logger.info("Call alert for '%s' suppressed (refractory period active).", key)
            return
        try:
            execution_sid = trigger_twilio_flow(body, self.twilio_config)
            self.last_call_triggered[key] = now
            logger.info("Call alert sent for '%s'; Execution SID: %s", key, execution_sid)
            return execution_sid
        except Exception as e:
            logger.error("Error sending call for '%s': %s", key, e, exc_info=True)
            return None


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
