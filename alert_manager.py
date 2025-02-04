#!/usr/bin/env python
import os
import time
import json
import smtplib
import logging
import sqlite3
from typing import Dict, Any, Optional
from datetime import datetime
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
# from dotenv import load_dotenv  # Disabled on purpose

# load_dotenv()  # Disabled

# Twilio Configuration (set as string literals)
TWILIO_ACCOUNT_SID = "ACb606788ada5dccbfeeebed0f440099b3"
TWILIO_AUTH_TOKEN = "f2cee9e20e844a42157cfccbc5df5648"
TWILIO_FLOW_SID = "FW5b3bf49ee04af4d23a118b613bbc0df2"
TWILIO_TO_PHONE = "+16199804758"
TWILIO_FROM_PHONE = "+18336913467"

# ================================
# Logging Configuration
# ================================
logger = logging.getLogger("AlertManagerLogger")
logger.setLevel(logging.DEBUG)

# Create console handler with debug level
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)

# Create file handler which logs debug messages to alert_manager_log.txt
file_handler = logging.FileHandler("alert_manager_log.txt")
file_handler.setLevel(logging.DEBUG)

# Create formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# Add the handlers to the logger if they aren't already added
if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

# ================================
# Twilio Helper Function
# ================================
def trigger_twilio_flow(custom_message: str) -> str:
    """
    Trigger a Twilio Studio Flow execution with a custom message.
    """
    logger.debug("Triggering Twilio Flow with message: %s", custom_message)
    account_sid = TWILIO_ACCOUNT_SID
    auth_token = TWILIO_AUTH_TOKEN
    flow_sid = TWILIO_FLOW_SID
    to_phone = TWILIO_TO_PHONE
    from_phone = TWILIO_FROM_PHONE

    logger.debug("TWILIO_ACCOUNT_SID: %s", account_sid)
    logger.debug("TWILIO_AUTH_TOKEN: %s", auth_token)
    logger.debug("TWILIO_FLOW_SID: %s", flow_sid)
    logger.debug("TWILIO_TO_PHONE: %s", to_phone)
    logger.debug("TWILIO_FROM_PHONE: %s", from_phone)

    if not all([account_sid, auth_token, flow_sid, to_phone, from_phone]):
        raise ValueError("One or more Twilio configuration variables are missing.")

    client = Client(account_sid, auth_token)
    execution = client.studio.v2.flows(flow_sid).executions.create(
        to=to_phone,
        from_=from_phone,
        parameters={"custom_message": custom_message}
    )
    logger.debug("Twilio execution created with SID: %s", execution.sid)
    return execution.sid

# ================================
# Helper for Metric-Based Alert Classification
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
# AlertManager Class (Call-Only Version with Extensive Debug Logging)
# ================================
class AlertManager:
    # Mapping asset codes to full names
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

        logger.debug("Initializing AlertManager with db_path=%s, poll_interval=%s, config_path=%s",
                     db_path, poll_interval, config_path)

        # Setup DataLocker and CalcServices
        from data_locker import DataLocker  # local import if needed
        from calc_services import CalcServices
        self.data_locker = DataLocker(self.db_path)
        self.calc_services = CalcServices()

        # Load configuration using load_config
        from config_manager import load_config  # local import if needed
        db_conn = self.data_locker.get_db_connection()
        self.config = load_config(self.config_path, db_conn)
        logger.debug("Configuration loaded: %s", self.config)

        # For call alerts, we rely on Twilio configuration (set above)
        self.liquid_cfg = self.config.get("alert_ranges", {}).get(
            "travel_percent_liquid_ranges", {"low": -25.0, "medium": -50.0, "high": -75.0}
        )
        logger.debug("Liquidation configuration: %s", self.liquid_cfg)

        # Global alert cooldown in seconds (default 900 seconds)
        self.cooldown = self.config.get("alert_cooldown_seconds", 900)
        logger.debug("Alert cooldown set to %s seconds", self.cooldown)

        # Call refractory period (defaulting to 3600 seconds)
        self.call_refractory_period = self.config.get("call_refractory_period", 3600)
        logger.debug("Call refractory period set to %s seconds", self.call_refractory_period)
        self.last_call_triggered: Dict[str, float] = {}

        # Whether alert monitoring is enabled
        self.monitor_enabled = self.config.get("system_config", {}).get("alert_monitor_enabled", True)
        logger.debug("Alert monitoring enabled: %s", self.monitor_enabled)

        # Dictionary to store times of last triggers (to enforce cooldown)
        self.last_triggered: Dict[str, float] = {}

        logger.info("AlertManager started. poll_interval=%s, cooldown=%s, call_refractory_period=%s",
                    self.poll_interval, self.cooldown, self.call_refractory_period)

    def run(self):
        logger.debug("Entering AlertManager run loop.")
        while True:
            self.check_alerts()
            time.sleep(self.poll_interval)

    def check_alerts(self):
        if not self.monitor_enabled:
            logger.debug("Alert monitoring disabled. Skipping check_alerts().")
            return

        positions = self.data_locker.read_positions()
        logger.debug("Loaded %d positions for TravelPercent checks.", len(positions))
        for pos in positions:
            self.check_travel_percent_liquid(pos)

        self.check_price_alerts()

    def check_travel_percent_liquid(self, pos: Dict[str, Any]):
        try:
            val = float(pos.get("current_travel_percent", 0.0))
        except Exception as e:
            logger.error("Error converting current_travel_percent: %s", e)
            val = 0.0

        if val >= 0:
            logger.debug("Skipping travel percent check for position %s because value is >= 0", pos.get("id"))
            return

        # First, check if the overall travel_percent_liquid alerts are enabled
        tpli_config = self.config.get("alert_ranges", {}).get("travel_percent_liquid_ranges", {})
        if not tpli_config.get("enabled", False):
            logger.info("Travel Percent Liquid alerts disabled in configuration. Skipping alert for position %s", pos.get("id"))
            return

        pos_id = pos.get("id", "unknown")
        asset_code = pos.get("asset_type", "???").upper()
        asset_full = self.ASSET_FULL_NAMES.get(asset_code, asset_code)
        position_type = pos.get("position_type", "").lower().capitalize()
        wallet_info = pos.get("wallet_name")
        if isinstance(wallet_info, dict):
            wallet_name = wallet_info.get("name", "Unknown")
        else:
            wallet_name = wallet_info or "Unknown"

        try:
            low = float(tpli_config.get("low", -25.0))
        except Exception:
            low = -25.0
        try:
            medium = float(tpli_config.get("medium", -50.0))
        except Exception:
            medium = -50.0
        try:
            high = float(tpli_config.get("high", -75.0))
        except Exception:
            high = -75.0

        logger.debug("For position %s, travel percent thresholds: low=%s, medium=%s, high=%s", pos_id, low, medium, high)

        alert_level = None
        if val <= high:
            alert_level = "HIGH"
        elif val <= medium:
            alert_level = "MEDIUM"
        elif val <= low:
            alert_level = "LOW"
        else:
            logger.debug("Travel percent %s does not cross any threshold for position %s", val, pos_id)
            return

        # Granular control: check whether a call should be made for this alert level.
        call_flag = tpli_config.get("call_on_" + alert_level.lower(), False)
        if not call_flag:
            logger.info("Call alert for travel_percent_liquid %s level is disabled by configuration for position %s", alert_level, pos_id)
            return

        key = f"{pos_id}-{alert_level}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        if (now - last_time) < self.cooldown:
            logger.debug("Skipping repeated TravelPercent alert for %s => %s (cooldown).", pos_id, alert_level)
            return

        self.last_triggered[key] = now
        message = (f"Travel Percent Liquid ALERT\n"
                   f"Asset: {asset_full} {position_type}, Wallet: {wallet_name}\n"
                   f"Current Travel% = {val:.2f}% => {alert_level} zone.")
        logger.info("Triggering Travel%% alert => %s", message)
        self.send_call(message, key)

    def check_price_alerts(self):
        all_alerts = self.data_locker.get_alerts()
        price_alerts = [
            a for a in all_alerts
            if a.get("alert_type") == "PRICE_THRESHOLD" and a.get("status", "").lower() == "active"
        ]
        logger.debug("Found %d active price alerts.", len(price_alerts))

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
                logger.debug("No price info for asset %s", asset_code)
                continue

            current_price = float(price_info["current_price"])
            if condition == "ABOVE" and current_price >= trigger_val:
                self.handle_price_alert_trigger(alert, current_price, asset_full)
            elif condition != "ABOVE" and current_price <= trigger_val:
                self.handle_price_alert_trigger(alert, current_price, asset_full)

    def handle_price_alert_trigger(self, alert: dict, current_price: float, asset_full: str):
        key = f"price-alert-{asset_full}"
        now = time.time()
        last_time = self.last_triggered.get(key, 0)
        if (now - last_time) < self.cooldown:
            logger.debug("Skipping repeated Price alert for asset %s (cooldown).", asset_full)
            return

        self.last_triggered[key] = now
        cond = alert.get("condition", "ABOVE").upper()
        try:
            trig_val = float(alert.get("trigger_value", 0.0))
        except Exception:
            trig_val = 0.0
        position_type = alert.get("position_type", "").lower().capitalize()
        wallet_name = alert.get("wallet_name", "Unknown")
        message = (f"Price ALERT\n"
                   f"Asset: {asset_full} {position_type}"
                   f"{', Wallet: ' + wallet_name if wallet_name != 'Unknown' else ''}\n"
                   f"Condition: {cond}\n"
                   f"Trigger Value: {trig_val}\n"
                   f"Current Price: {current_price}\n")
        logger.info("Triggering PriceThreshold alert => %s", message)
        self.send_call(message, key)

    def send_call(self, body: str, key: str):
        """
        Triggers a phone call via Twilio using the Studio Flow.
        Checks if a call alert for the given key has been triggered within the refractory period.
        """
        now = time.time()
        last_call = self.last_call_triggered.get(key, 0)
        if (now - last_call) < self.call_refractory_period:
            logger.info("Call alert for %s suppressed due to refractory period.", key)
            return

        try:
            execution_sid = trigger_twilio_flow(body)
            self.last_call_triggered[key] = now
            logger.info("Call alert triggered for key %s, execution SID: %s", key, execution_sid)
            return execution_sid
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info("Call alert already active for this contact; suppressing new call for key: %s", key)
                self.last_call_triggered[key] = now
                return None
            else:
                logger.error("Failed to trigger call alert: %s", e, exc_info=True)
                return None
        except Exception as e:
            logger.error("Failed to trigger call alert: %s", e, exc_info=True)
            return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    manager = AlertManager(
        db_path=os.path.abspath("mother_brain.db"),
        poll_interval=60,
        config_path="sonic_config.json"
    )
    manager.run()
