import os
import json
import logging
from flask import Blueprint, request, jsonify, render_template

# Set up logger
logger = logging.getLogger("AlertManagerLogger")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)


# -------------------------------
# Deep Merge Function
# -------------------------------
def deep_merge(source: dict, updates: dict) -> dict:
    """
    Recursively merge 'updates' into 'source'.
    """
    for key, value in updates.items():
        if key in source and isinstance(source[key], dict) and isinstance(value, dict):
            logger.debug("Deep merging key: %s", key)
            source[key] = deep_merge(source[key], value)
        else:
            logger.debug("Updating key: %s with value: %s", key, value)
            source[key] = value
    return source


# -------------------------------
# SonicConfigManager Class
# -------------------------------
class SonicConfigManager:
    def __init__(self, config_path: str, lock_path: str = "sonic_config.lock"):
        self.config_path = config_path
        self.lock_path = lock_path

    def load_config(self) -> dict:
        if not os.path.exists(self.config_path):
            logger.error("Configuration file not found: %s", self.config_path)
            raise FileNotFoundError("Configuration file not found")
        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        logger.debug("Loaded config: %s", config)
        return config

    def save_config(self, config: dict) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        logger.info("Configuration saved to %s", self.config_path)

    def update_alert_config(self, new_alerts: dict) -> None:
        config = self.load_config()
        current_alerts = config.get("alert_ranges", {})
        logger.debug("Existing alert config: %s", current_alerts)
        merged = deep_merge(current_alerts, new_alerts)
        config["alert_ranges"] = merged
        self.save_config(config)
        logger.info("Alert configuration updated successfully.")


# -------------------------------
# Blueprint Setup and Helpers
# -------------------------------
alerts_bp = Blueprint('alerts_bp', __name__, url_prefix='/alerts')

# Set the configuration file path (adjust as needed)
CONFIG_PATH = os.path.join(os.getcwd(), "sonic_config.json")
config_mgr = SonicConfigManager(CONFIG_PATH)


def parse_nested_form(form) -> dict:
    """
    Convert flat form keys (like:
       alert_ranges[profit_ranges][enabled]
    ) into a nested dictionary.
    """
    updated = {}
    for full_key, value in form.items():
        full_key = full_key.strip()
        keys = []
        part = ""
        in_brackets = False
        for char in full_key:
            if char == '[':
                if part:
                    keys.append(part)
                    part = ""
                in_brackets = True
            elif char == ']':
                if part:
                    keys.append(part)
                    part = ""
                in_brackets = False
            else:
                part += char
        if part:
            keys.append(part)
        # Remove the outer "alert_ranges" if present.
        if keys and keys[0] == "alert_ranges":
            keys = keys[1:]
        # Build the nested dict.
        current = updated
        for i, key in enumerate(keys):
            if i == len(keys) - 1:
                if isinstance(value, str):
                    lower_val = value.lower()
                    if lower_val == "true":
                        val = True
                    elif lower_val == "false":
                        val = False
                    else:
                        try:
                            val = float(value)
                        except Exception:
                            val = value
                else:
                    val = value
                current[key] = val
            else:
                if key not in current:
                    current[key] = {}
                current = current[key]
    return updated


def format_alert_config_table(alert_ranges: dict) -> str:
    """
    Returns an HTML table string summarizing the alert ranges.
    """
    metrics = [
        "heat_index_ranges", "collateral_ranges", "value_ranges",
        "size_ranges", "leverage_ranges", "liquidation_distance_ranges",
        "travel_percent_liquid_ranges", "travel_percent_profit_ranges", "profit_ranges"
    ]
    html = "<table border='1' style='border-collapse: collapse; width:100%;'>"
    html += "<tr><th>Metric</th><th>Enabled</th><th>Low</th><th>Medium</th><th>High</th></tr>"
    for m in metrics:
        data = alert_ranges.get(m, {})
        enabled = data.get("enabled", False)
        low = data.get("low", "")
        medium = data.get("medium", "")
        high = data.get("high", "")
        html += f"<tr><td>{m}</td><td>{enabled}</td><td>{low}</td><td>{medium}</td><td>{high}</td></tr>"
    html += "</table>"
    return html


# -------------------------------
# Routes
# -------------------------------
@alerts_bp.route('/config', methods=['GET'], endpoint="alert_config_page")
def config():
    """
    Render the alert configuration page.
    """
    try:
        config_data = config_mgr.load_config()
        return render_template("alert_manager_config.html", alert_ranges=config_data.get("alert_ranges", {}))
    except Exception as e:
        logger.error("Error loading config: %s", str(e))
        return "Error loading config", 500


@alerts_bp.route('/update_config', methods=['POST'])
def update_alert_config():
    """
    Update the alert configuration from the form data.
    """
    try:
        raw_form = request.form.to_dict(flat=False)
        logger.debug("POST Data Received:\n%s", json.dumps(raw_form, indent=2))

        nested_update = parse_nested_form(request.form)
        logger.debug("Parsed Nested Form Data:\n%s", json.dumps(nested_update, indent=2))

        config_mgr.update_alert_config(nested_update)

        updated_config = config_mgr.load_config()
        logger.debug("New Config Loaded After Update:\n%s", json.dumps(updated_config, indent=2))

        formatted_table = format_alert_config_table(updated_config.get("alert_ranges", {}))
        return jsonify({"success": True, "formatted_table": formatted_table})
    except Exception as e:
        logger.error("Error updating alert config: %s", str(e))
        return jsonify({"error": str(e)}), 500


# -------------------------------
# For running this file directly (for testing)
# -------------------------------
if __name__ == "__main__":
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(alerts_bp)
    app.run(debug=True, port=5001)
