#!/usr/bin/env python
import os
import json
import time
import logging
from uuid import uuid4
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from config.config_constants import DB_PATH, CONFIG_PATH
from config.config_manager import load_config, update_config
from config.config import AppConfig
from data.data_locker import DataLocker
from alerts.alert_manager import AlertManager

# Create the alerts blueprint with its own template folder (adjust path as needed)
alerts_bp = Blueprint('alerts_bp', __name__, template_folder='templates')

# Instantiate the AlertManager for use in the alert routes
manager = AlertManager(db_path=DB_PATH, poll_interval=60, config_path=CONFIG_PATH)

logger = logging.getLogger("AlertManagerLogger")

# Helper function to parse nested form data for alert configuration updates
def parse_nested_form(form_data: dict) -> dict:
    updated = {}
    for full_key, value in form_data.items():
        if full_key.startswith("alert_ranges[") and full_key.endswith("]"):
            inner = full_key[len("alert_ranges["):-1]
            parts = inner.split("][")
            if len(parts) == 2:
                metric, field = parts
                if metric not in updated:
                    updated[metric] = {}
                if field in ["enabled"]:
                    updated[metric][field] = True
                else:
                    try:
                        updated[metric][field] = float(value)
                    except ValueError:
                        updated[metric][field] = value
            elif len(parts) == 3:
                metric, subfield, field = parts
                if metric not in updated:
                    updated[metric] = {}
                if subfield not in updated[metric]:
                    updated[metric][subfield] = {}
                updated[metric][subfield][field] = True
    return updated

# -----------------------------------------------------------------------------
# Alert-Related Endpoints
# -----------------------------------------------------------------------------

# GET /alerts/ -> Display the main alerts page
@alerts_bp.route("/", methods=["GET"])
def alerts():
    try:
        config_data = load_config(CONFIG_PATH)
        data_locker = DataLocker(DB_PATH)
        all_alerts = data_locker.get_alerts()
        active_alerts_data = [a for a in all_alerts if a.get("status", "").lower() == "active"]
        recent_alerts_data = [a for a in all_alerts if a.get("status", "").lower() != "active"]
        low_alert_count = 0
        medium_alert_count = 0
        high_alert_count = 0
        mini_prices = []
        for asset in ["BTC", "ETH", "SOL"]:
            row = data_locker.get_latest_price(asset)
            if row:
                mini_prices.append({
                    "asset_type": row["asset_type"],
                    "current_price": float(row["current_price"]),
                })
        return render_template(
            "alerts.html",
            mini_prices=mini_prices,
            low_alert_count=low_alert_count,
            medium_alert_count=medium_alert_count,
            high_alert_count=high_alert_count,
            active_alerts=active_alerts_data,
            recent_alerts=recent_alerts_data,
            alert_ranges=config_data.get("alert_ranges", {})
        )
    except Exception as e:
        logger.exception(f"Error in alerts route: {e}")
        flash(f"Error loading alerts: {e}", "danger")
        return redirect(url_for("index"))

# POST /alerts/create -> Create a new alert
@alerts_bp.route("/create", methods=["POST"])
def alerts_create():
    alert_type = request.form.get("alert_type", "")
    asset_type = request.form.get("asset_type", "")
    trigger_value_str = request.form.get("trigger_value", "0")
    condition = request.form.get("condition", "ABOVE")
    status = request.form.get("status", "Active")
    notification_type = request.form.get("notification_type", "SMS")
    position_ref = request.form.get("position_reference_id", "")
    try:
        trigger_value = float(trigger_value_str)
    except ValueError:
        trigger_value = 0.0
    new_alert = {
        "id": str(uuid4()),
        "alert_type": alert_type,
        "asset_type": asset_type,
        "trigger_value": trigger_value,
        "condition": condition,
        "notification_type": notification_type,
        "last_triggered": None,
        "status": status,
        "frequency": 0,
        "counter": 0,
        "liquidation_distance": 0.0,
        "target_travel_percent": 0.0,
        "liquidation_price": 0.0,
        "notes": "",
        "position_reference_id": position_ref
    }
    data_locker = DataLocker(DB_PATH)
    data_locker.create_alert(new_alert)
    flash("New alert created successfully!", "success")
    return redirect(url_for("alerts_bp.alerts"))

# POST /alerts/delete/<alert_id> -> Delete an alert
@alerts_bp.route("/delete/<alert_id>", methods=["POST"])
def delete_alert(alert_id):
    data_locker = DataLocker(DB_PATH)
    data_locker.delete_alert(alert_id)
    flash("Alert deleted!", "success")
    return redirect(url_for("alerts_bp.alerts"))

# GET/POST /alerts/options -> Display and update alert threshold options
@alerts_bp.route("/options", methods=["GET", "POST"])
def alert_options():
    try:
        if not os.path.exists(CONFIG_PATH):
            return jsonify({"error": "sonic_config.json not found"}), 404
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        config_data = AppConfig(**data)
        if request.method == "POST":
            new_low = float(request.form.get("heat_index_low", 0.0))
            new_med = float(request.form.get("heat_index_medium", 0.0))
            new_high = float(request.form.get("heat_index_high", 0.0))
            config_data.alert_ranges["heat_index_ranges"]["low"] = new_low
            config_data.alert_ranges["heat_index_ranges"]["medium"] = new_med
            config_data.alert_ranges["heat_index_ranges"]["high"] = new_high
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config_data.model_dump(), f, indent=2)
            flash("Alert settings updated!", "success")
            return redirect(url_for("alerts_bp.alert_options"))
        return render_template("alert_options.html", config=config_data)
    except FileNotFoundError:
        return jsonify({"error": "sonic_config.json not found"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON in config file"}), 400
    except Exception as e:
        logger.error(f"Error in alert_options: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# GET /alerts/config -> Render the alert manager configuration page
@alerts_bp.route("/config", methods=["GET"])
def alert_config_page():
    return render_template("alert_manager_config.html")

# POST /alerts/update_config -> Update the alert configuration
@alerts_bp.route("/update_config", methods=["POST"])
def update_alert_config():
    try:
        config = load_config(CONFIG_PATH)
        form_data = request.form.to_dict(flat=True)
        updated_alerts = parse_nested_form(form_data)
        config["alert_ranges"] = updated_alerts
        updated_config = update_config(config, CONFIG_PATH)
        manager.reload_config()
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Error updating alert config: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500

# POST /alerts/manual_check -> Manually trigger an alert check
@alerts_bp.route("/manual_check", methods=["POST"])
def manual_check_alerts():
    manager.check_alerts()
    return jsonify({"status": "success", "message": "Alerts have been manually checked!"}), 200
