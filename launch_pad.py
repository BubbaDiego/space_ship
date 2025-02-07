#!/usr/bin/env python
import os
import time
import json
import smtplib
import logging
import sqlite3
from config_manager import load_json_config
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from flask_socketio import SocketIO, emit
from flask import current_app
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import asyncio
import pytz
import requests

from flask import Flask, Blueprint, request, jsonify, render_template, redirect, url_for, flash, send_file
from uuid import uuid4

# Panda Stuff
from models import Position
from data_locker import DataLocker
from config_manager import load_config  # local import if needed
from config import AppConfig
from calc_services import CalcServices
from price_monitor import PriceMonitor
from alert_manager import AlertManager

# Build absolute paths
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "sonic_config.json")
DB_PATH = os.path.join(BASE_DIR, "mother_brain.db")

# Load configuration from JSON file
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

# Extract Twilio configuration from the JSON data
twilio_config = config.get("twilio_config", {})
TWILIO_ACCOUNT_SID = twilio_config.get("account_sid")
TWILIO_AUTH_TOKEN = twilio_config.get("auth_token")
TWILIO_FLOW_SID = twilio_config.get("flow_sid")
TWILIO_TO_PHONE = twilio_config.get("to_phone")
TWILIO_FROM_PHONE = twilio_config.get("from_phone")

# ================================
# Logging Configuration
# ================================
logger = logging.getLogger("WebAppLogger")
logger.setLevel(logging.DEBUG)
app = Flask(__name__)
app.debug = False
app.secret_key = "i-like-lamp"

# Build absolute paths
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "mother_brain.db")
CONFIG_PATH = os.path.join(BASE_DIR, "sonic_config.json")

MINT_TO_ASSET = {
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
    "So11111111111111111111111111111111111111112": "SOL"
}

# ================================
# AlertManager Instance
# ================================
manager = AlertManager(
    db_path=DB_PATH,
    poll_interval=60,
    config_path=CONFIG_PATH
)

# Initialize SocketIO with your app
socketio = SocketIO(app)


@app.route('/')
def index():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    theme = config.get("theme_profiles", {})
    return render_template("base.html", theme=theme, title="Sonic Dashboard")


@app.route('/theme')
def theme_options():
    return render_template('theme.html')


def get_alert_class(value, low_thresh, med_thresh, high_thresh):
    if high_thresh is None:
        high_thresh = float('inf')
    if med_thresh is None:
        med_thresh = float('inf')
    if low_thresh is None:
        low_thresh = float('-inf')
    if value >= high_thresh:
        return "alert-high"
    elif value >= med_thresh:
        return "alert-medium"
    elif value >= low_thresh:
        return "alert-low"
    else:
        return "alert-high"


# Dedicated profit function for alert evaluation.
def get_profit_alert_class(profit, low_thresh, med_thresh, high_thresh):
    try:
        low = float(low_thresh) if low_thresh not in (None, "") else float('inf')
    except:
        low = float('inf')
    try:
        med = float(med_thresh) if med_thresh not in (None, "") else float('inf')
    except:
        med = float('inf')
    try:
        high = float(high_thresh) if high_thresh not in (None, "") else float('inf')
    except:
        high = float('inf')
    if profit < low:
        return ""  # No alert if profit is below low threshold.
    elif profit < med:
        return "alert-low"
    elif profit < high:
        return "alert-medium"
    else:
        return "alert-high"


@app.route("/positions")
def positions():
    data_locker = DataLocker(DB_PATH)
    calc_services = CalcServices()

    # 1) Read positions from DB and fill missing prices.
    positions_data = data_locker.read_positions()
    positions_data = fill_positions_with_latest_price(positions_data)
    updated_positions = calc_services.aggregator_positions(positions_data, DB_PATH)

    # 2) Attach wallet info if available.
    for pos in updated_positions:
        pos["collateral"] = float(pos.get("collateral") or 0.0)
        wallet_name = pos.get("wallet_name")
        if wallet_name:
            w = data_locker.get_wallet_by_name(wallet_name)
            pos["wallet_name"] = w
        else:
            pos["wallet_name"] = None

    # 3) Load alert configuration.
    config_data = load_app_config()
    alert_dict = config_data.alert_ranges or {}

    # Local helper to compute an alert class for non-profit fields.
    def get_alert_class_local(value, low_thresh, med_thresh, high_thresh):
        try:
            low_thresh = float(low_thresh) if low_thresh not in (None, "") else float('-inf')
        except ValueError:
            low_thresh = float('-inf')
        try:
            med_thresh = float(med_thresh) if med_thresh not in (None, "") else float('inf')
        except ValueError:
            med_thresh = float('inf')
        try:
            high_thresh = float(high_thresh) if high_thresh not in (None, "") else float('inf')
        except ValueError:
            high_thresh = float('inf')
        if value >= high_thresh:
            return "alert-high"
        elif value >= med_thresh:
            return "alert-medium"
        elif value >= low_thresh:
            return "alert-low"
        else:
            return ""

    # 4) Extract thresholds from the config.
    hi_cfg = alert_dict.get("heat_index_ranges", {})
    hi_low = hi_cfg.get("low", 0.0)
    hi_med = hi_cfg.get("medium", 0.0)
    hi_high = hi_cfg.get("high", None)

    coll_cfg = alert_dict.get("collateral_ranges", {})
    coll_low = coll_cfg.get("low", 0.0)
    coll_med = coll_cfg.get("medium", 0.0)
    coll_high = coll_cfg.get("high", None)

    val_cfg = alert_dict.get("value_ranges", {})
    val_low = val_cfg.get("low", 0.0)
    val_med = val_cfg.get("medium", 0.0)
    val_high = val_cfg.get("high", None)

    size_cfg = alert_dict.get("size_ranges", {})
    size_low = size_cfg.get("low", 0.0)
    size_med = size_cfg.get("medium", 0.0)
    size_high = size_cfg.get("high", None)

    lev_cfg = alert_dict.get("leverage_ranges", {})
    lev_low = lev_cfg.get("low", 0.0)
    lev_med = lev_cfg.get("medium", 0.0)
    lev_high = lev_cfg.get("high", None)

    liqd_cfg = alert_dict.get("liquidation_distance_ranges", {})
    liqd_low = liqd_cfg.get("low", 0.0)
    liqd_med = liqd_cfg.get("medium", 0.0)
    liqd_high = liqd_cfg.get("high", None)

    tliq_cfg = alert_dict.get("travel_percent_liquid_ranges", {})
    tliq_low = tliq_cfg.get("low", 0.0)
    tliq_med = tliq_cfg.get("medium", 0.0)
    tliq_high = tliq_cfg.get("high", None)

    tprof_cfg = alert_dict.get("travel_percent_profit_ranges", {})
    tprof_low = tprof_cfg.get("low", 0.0)
    tprof_med = tprof_cfg.get("medium", 0.0)
    tprof_high = tprof_cfg.get("high", None)

    profit_cfg = alert_dict.get("profit_ranges", {})
    profit_low = profit_cfg.get("low", 0.0)
    profit_med = profit_cfg.get("medium", 0.0)
    profit_high = profit_cfg.get("high", None)

    # 5) Calculate alert classes for each position.
    for pos in updated_positions:
        heat_val = float(pos.get("heat_index", 0.0))
        pos["heat_alert_class"] = get_alert_class_local(heat_val, hi_low, hi_med, hi_high)
        coll_val = float(pos.get("collateral", 0.0))
        pos["collateral_alert_class"] = get_alert_class_local(coll_val, coll_low, coll_med, coll_high)
        val = float(pos.get("value", 0.0))
        pos["value_alert_class"] = get_alert_class_local(val, val_low, val_med, val_high)
        sz = float(pos.get("size", 0.0))
        pos["size_alert_class"] = get_alert_class_local(sz, size_low, size_med, size_high)
        lev = float(pos.get("leverage", 0.0))
        pos["leverage_alert_class"] = get_alert_class_local(lev, lev_low, lev_med, lev_high)
        liqd = float(pos.get("liquidation_distance", 0.0))
        pos["liqdist_alert_class"] = get_alert_class_local(liqd, liqd_low, liqd_med, liqd_high)
        tliq_val = float(pos.get("current_travel_percent", 0.0))
        pos["travel_liquid_alert_class"] = get_alert_class_local(tliq_val, tliq_low, tliq_med, tliq_high)
        tprof_val = float(pos.get("current_travel_percent", 0.0))
        pos["travel_profit_alert_class"] = get_alert_class_local(tprof_val, tprof_low, tprof_med, tprof_high)
        profit_val = float(pos.get("pnl_after_fees_usd", 0.0))
        pos["profit_alert_class"] = get_profit_alert_class(profit_val, profit_low, profit_med, profit_high)

    totals_dict = calc_services.calculate_totals(updated_positions)
    times_dict = data_locker.get_last_update_times() or {}
    pos_time_iso = times_dict.get("last_update_time_positions", "N/A")

    # Helper: Convert ISO timestamp to PST string.
    def _convert_iso_to_pst(iso_str):
        if not iso_str or iso_str == "N/A":
            return "N/A"
        pst = pytz.timezone("US/Pacific")
        try:
            dt_obj = datetime.fromisoformat(iso_str)
            dt_pst = dt_obj.astimezone(pst)
            return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
        except:
            return "N/A"

    pos_time_formatted = _convert_iso_to_pst(pos_time_iso)
    return render_template("positions.html",
                           positions=updated_positions,
                           totals=totals_dict,
                           last_update_positions=pos_time_formatted,
                           last_update_positions_source=times_dict.get("last_update_positions_source", "N/A"))


def _convert_iso_to_pst(iso_str):
    if not iso_str or iso_str == "N/A":
        return "N/A"
    pst = pytz.timezone("US/Pacific")
    try:
        dt_obj = datetime.fromisoformat(iso_str)
        dt_pst = dt_obj.astimezone(pst)
        return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except:
        return "N/A"


@app.route("/exchanges")
def exchanges():
    data_locker = DataLocker(DB_PATH)
    brokers_data = data_locker.read_brokers()
    return render_template("exchanges.html", brokers=brokers_data)


@app.route("/edit-position/<position_id>", methods=["POST"])
def edit_position(position_id):
    data_locker = DataLocker(DB_PATH)
    logger.debug(f"Editing position {position_id}.")
    try:
        size = float(request.form.get("size", 0.0))
        collateral = float(request.form.get("collateral", 0.0))
        data_locker.update_position(position_id, size, collateral)
        data_locker.sync_calc_services()
        return redirect(url_for("positions"))
    except Exception as e:
        logger.error(f"Error updating position {position_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/delete-position/<position_id>", methods=["POST"])
def delete_position(position_id):
    data_locker = DataLocker(DB_PATH)
    logger.debug(f"Deleting position {position_id}")
    try:
        data_locker.cursor.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        data_locker.conn.commit()
        return redirect(url_for("positions"))
    except Exception as e:
        logger.error(f"Error deleting position {position_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/delete-all-positions", methods=["POST"])
def delete_all_positions():
    data_locker = DataLocker(DB_PATH)
    logger.debug("Deleting ALL positions")
    try:
        data_locker.delete_all_positions()
        return redirect(url_for("positions"))
    except Exception as e:
        logger.error(f"Error deleting all positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/upload-positions", methods=["POST"])
def upload_positions():
    data_locker = DataLocker(DB_PATH)
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file part in request"}), 400
        file = request.files["file"]
        if not file:
            return jsonify({"error": "Empty file"}), 400
        file_contents = file.read().decode("utf-8").strip()
        if not file_contents:
            return jsonify({"error": "Uploaded file is empty"}), 400
        positions_list = json.loads(file_contents)
        if not isinstance(positions_list, list):
            return jsonify({"error": "Top-level JSON must be a list"}), 400
        for pos_dict in positions_list:
            if "wallet_name" in pos_dict:
                pos_dict["wallet"] = pos_dict["wallet_name"]
            data_locker.create_position(pos_dict)
        return jsonify({"message": "Positions uploaded successfully"}), 200
    except Exception as e:
        app.logger.error(f"Error uploading positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def update_wallet(self, wallet_name, wallet_dict):
    query = """
        UPDATE wallets 
           SET name = ?,
               public_address = ?,
               private_address = ?,
               image_path = ?,
               balance = ?
         WHERE name = ?
    """
    self.cursor.execute(query, (
        wallet_dict.get("name"),
        wallet_dict.get("public_address"),
        wallet_dict.get("private_address"),
        wallet_dict.get("image_path"),
        wallet_dict.get("balance"),
        wallet_name
    ))
    self.conn.commit()


@app.route("/edit_wallet/<wallet_name>", methods=["GET", "POST"])
def edit_wallet(wallet_name):
    data_locker = DataLocker(DB_PATH)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        public_addr = request.form.get("public_address", "").strip()
        private_addr = request.form.get("private_address", "").strip()
        image_path = request.form.get("image_path", "").strip()
        balance_str = request.form.get("balance", "0.0").strip()
        try:
            balance_val = float(balance_str)
        except ValueError:
            balance_val = 0.0
        wallet_dict = {
            "name": name,
            "public_address": public_addr,
            "private_address": private_addr,
            "image_path": image_path,
            "balance": balance_val
        }
        data_locker.update_wallet(wallet_name, wallet_dict)
        flash(f"Wallet '{name}' updated successfully!", "success")
        return redirect(url_for("assets"))
    else:
        wallet = data_locker.get_wallet_by_name(wallet_name)
        if not wallet:
            flash(f"Wallet '{wallet_name}' not found.", "danger")
            return redirect(url_for("assets"))
        return render_template("edit_wallet.html", wallet=wallet)


@app.route("/prices", methods=["GET", "POST"])
def prices():
    data_locker = DataLocker(DB_PATH)
    if request.method == "POST":
        asset = request.form.get("asset", "BTC")
        raw_price = request.form.get("price", "0.0")
        price_val = float(raw_price)
        data_locker.insert_or_update_price(
            asset_type=asset,
            current_price=price_val,
            source="Manual",
            timestamp=datetime.now()
        )
        return redirect(url_for("prices"))
    top_prices = _get_top_prices_for_assets(DB_PATH, ["BTC", "ETH", "SOL"])
    recent_prices = _get_recent_prices(DB_PATH, limit=15)
    api_counters = data_locker.read_api_counters()
    return render_template(
        "prices.html",
        prices=top_prices,
        recent_prices=recent_prices,
        api_counters=api_counters
    )


@app.route("/update-prices", methods=["POST"])
def update_prices():
    source = request.args.get("source") or request.form.get("source") or "API"
    pm = PriceMonitor(db_path=DB_PATH, config_path=CONFIG_PATH)
    try:
        asyncio.run(pm.update_prices())
        data_locker = DataLocker(db_path=DB_PATH)
        now = datetime.now()
        data_locker.set_last_update_times(
            prices_dt=now,
            prices_source=source
        )
    except Exception as e:
        logger.exception(f"Error updating prices: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "ok", "message": "Prices updated successfully"})


@app.route("/alert-options", methods=["GET", "POST"])
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
            return redirect(url_for("alert_options"))
        return render_template("alert_options.html", config=config_data)
    except FileNotFoundError:
        return jsonify({"error": "sonic_config.json not found"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON in config file"}), 400
    except Exception as e:
        app.logger.error(f"Error in /alert-options: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/show-updates")
def show_updates():
    data_locker = DataLocker(DB_PATH)
    times = data_locker.get_last_update_times()
    return render_template("some_template.html", update_times=times)


@app.route("/system-options", methods=["GET", "POST"])
def system_options():
    data_locker = DataLocker(DB_PATH)
    if request.method == "POST":
        config = load_app_config()
        form_action = request.form.get("action")
        if form_action == "reset_counters":
            data_locker.reset_api_counters()
            flash("API report counters have been reset!", "success")
            return redirect(url_for("system_options"))
        else:
            config.system_config["log_level"] = request.form.get("log_level", "INFO")
            config.system_config["db_path"] = request.form.get("db_path", config.system_config.get("db_path"))
            assets_str = request.form.get("assets", "")
            config.price_config["assets"] = [a.strip() for a in assets_str.split(",") if a.strip()]
            config.price_config["currency"] = request.form.get("currency", "USD")
            config.price_config["fetch_timeout"] = int(request.form.get("fetch_timeout", 10))
            config.api_config["coingecko_api_enabled"] = request.form.get("coingecko_api_enabled", "ENABLE")
            config.api_config["binance_api_enabled"] = request.form.get("binance_api_enabled", "ENABLE")
            config.api_config["coinmarketcap_api_key"] = request.form.get("coinmarketcap_api_key", "")
            save_app_config(config)
            flash("System options saved!", "success")
            return redirect(url_for("system_options"))
    config = load_app_config()
    return render_template("system_options.html", config=config)


@app.route("/export-config")
def export_config():
    return send_file(
        CONFIG_PATH,
        as_attachment=True,
        download_name="sonic_config.json",
        mimetype="application/json"
    )


@app.route('/console_view')
def console_view():
    log_url = "https://www.pythonanywhere.com/user/BubbaDiego/files/var/log/www.deadlypanda.com.error.log"
    return render_template("console_view.html", log_url=log_url)


@app.route("/heat", methods=["GET"])
def heat():
    data_locker = DataLocker(DB_PATH)
    calc_services = CalcServices()
    positions_data = data_locker.read_positions()
    positions_data = fill_positions_with_latest_price(positions_data)
    positions_data = calc_services.prepare_positions_for_display(positions_data)
    structure = {
        "BTC": {"short": {}, "long": {}},
        "ETH": {"short": {}, "long": {}},
        "SOL": {"short": {}, "long": {}},
        "totals": {
            "short": {"asset": "Short", "collateral": 0, "value": 0, "leverage": 0, "travel_percent": 0,
                      "heat_index": 0, "size": 0},
            "long": {"asset": "Long", "collateral": 0, "value": 0, "leverage": 0, "travel_percent": 0, "heat_index": 0,
                     "size": 0}
        }
    }
    for asset in ["BTC", "ETH", "SOL"]:
        asset_positions = [p for p in positions_data if p["asset_type"].upper() == asset]
        short_positions = [p for p in asset_positions if p["position_type"].lower() == "short"]
        long_positions = [p for p in asset_positions if p["position_type"].lower() == "long"]
        short_totals = calc_services.calculate_totals(short_positions)
        structure[asset]["short"] = {
            "asset": asset,
            "collateral": short_totals["total_collateral"],
            "value": short_totals["total_value"],
            "leverage": short_totals["avg_leverage"],
            "travel_percent": short_totals["avg_travel_percent"],
            "heat_index": short_totals["avg_heat_index"],
            "size": short_totals["total_size"],
        }
        long_totals = calc_services.calculate_totals(long_positions)
        structure[asset]["long"] = {
            "asset": asset,
            "collateral": long_totals["total_collateral"],
            "value": long_totals["total_value"],
            "leverage": long_totals["avg_leverage"],
            "travel_percent": long_totals["avg_travel_percent"],
            "heat_index": long_totals["avg_heat_index"],
            "size": long_totals["total_size"],
        }
    all_short_positions = [p for p in positions_data if p["position_type"].lower() == "short"]
    all_long_positions = [p for p in positions_data if p["position_type"].lower() == "long"]
    total_short = calc_services.calculate_totals(all_short_positions)
    total_long = calc_services.calculate_totals(all_long_positions)
    structure["totals"]["short"].update({
        "collateral": total_short["total_collateral"],
        "value": total_short["total_value"],
        "leverage": total_short["avg_leverage"],
        "travel_percent": total_short["avg_travel_percent"],
        "heat_index": total_short["avg_heat_index"],
        "size": total_short["total_size"],
    })
    structure["totals"]["long"].update({
        "collateral": total_long["total_collateral"],
        "value": total_long["total_value"],
        "leverage": total_long["avg_leverage"],
        "travel_percent": total_long["avg_travel_percent"],
        "heat_index": total_long["avg_heat_index"],
        "size": total_long["total_size"],
    })
    return render_template("hedge_report.html", chart_data=structure)


@app.route("/alerts")
def alerts():
    try:
        config_data = load_app_config()
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
            alert_ranges=config_data.alert_ranges
        )
    except Exception as e:
        logger.exception(f"Error in /alerts route: {e}")
        flash(f"Error loading alerts: {e}", "danger")
        return redirect(url_for("index"))


@app.route("/alerts/create", methods=["POST"])
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
    return redirect(url_for("alerts"))


@app.route("/api/get_config")
def api_get_config():
    try:
        config = load_config()
        return jsonify(config)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/update_system_options', methods=['POST'])
def update_system_options():
    try:
        config = load_config()
        if "alert_cooldown_seconds" in request.form:
            config["alert_cooldown_seconds"] = int(request.form.get("alert_cooldown_seconds"))
        if "call_refractory_period" in request.form:
            config["call_refractory_period"] = int(request.form.get("call_refractory_period"))
        save_config(config)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def parse_nested_form(form_dict):
    nested = {}
    for full_key, value in form_dict.items():
        if full_key.startswith("alert_ranges["):
            key_body = full_key[len("alert_ranges["):-1]
            parts = key_body.split("][")
            if len(parts) == 1:
                nested[parts[0]] = value
            elif len(parts) == 2:
                metric, subkey = parts
                if metric not in nested:
                    nested[metric] = {}
                nested[metric][subkey] = value
    return nested


@app.route('/reset_refractory', methods=['POST'])
def reset_refractory():
    try:
        config = load_config()
        config["last_refractory_reset"] = "now"
        save_config(config)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/manual_check_alerts", methods=["POST"])
def manual_check_alerts():
    manager.check_alerts()
    return jsonify({"status": "success", "message": "Alerts have been manually checked!"}), 200


@app.route("/jupiter-perps-proxy", methods=["GET"])
def jupiter_perps_proxy():
    wallet_address = request.args.get("walletAddress", "6vMjsGU63evYuwwGsDHBRnKs1stALR7SYN5V57VZLXca")
    jupiter_url = f"https://perps-api.jup.ag/v1/positions?walletAddress={wallet_address}&showTpslRequests=true"
    try:
        response = requests.get(jupiter_url)
        response.raise_for_status()
        data = response.json()
        return jsonify(data), 200
    except requests.exceptions.HTTPError as http_err:
        app.logger.error(f"HTTP error fetching Jupiter positions: {http_err}")
        return jsonify({"error": f"HTTP {response.status_code}: {response.text}"}), 500
    except Exception as e:
        app.logger.error(f"Error fetching Jupiter positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


import json
from datetime import datetime
import requests
from flask import jsonify


def update_jupiter_positions():
    """
    Updates Jupiter positions by fetching data from the Jupiter API for each wallet.
    New positions are inserted into the database, and duplicate positions (based on the 'id'
    field populated with Jupiter's "positionPubkey") are skipped.
    Logs a summary of how many positions were created versus duplicates.
    """
    data_locker = DataLocker(DB_PATH)
    try:
        wallets_list = data_locker.read_wallets()
        if not wallets_list:
            app.logger.info("No wallets found in DB.")
            return jsonify({"message": "No wallets found in DB"}), 200

        new_positions = []
        for w in wallets_list:
            public_addr = w.get("public_address", "").strip()
            if not public_addr:
                app.logger.info(f"Skipping wallet {w['name']} (no public_address).")
                continue

            jupiter_url = f"https://perps-api.jup.ag/v1/positions?walletAddress={public_addr}&showTpslRequests=true"
            resp = requests.get(jupiter_url)
            resp.raise_for_status()
            data = resp.json()
            print(json.dumps(data, indent=2))
            data_list = data.get("dataList", [])
            if not data_list:
                app.logger.info(f"No positions for wallet {w['name']} ({public_addr}).")
                continue

            for item in data_list:
                try:
                    pos_pubkey = item.get("positionPubkey")
                    if not pos_pubkey:
                        app.logger.warning(f"Skipping item for wallet {w['name']} because positionPubkey is missing")
                        continue
                    epoch_time = float(item.get("updatedTime", 0))
                    updated_dt = datetime.fromtimestamp(epoch_time)
                    mint = item.get("marketMint", "")
                    asset_type = MINT_TO_ASSET.get(mint, "BTC")
                    side = item.get("side", "short").capitalize()
                    travel_pct_value = item.get("pnlChangePctAfterFees")
                    travel_percent = float(travel_pct_value) if travel_pct_value is not None else 0.0
                    pos_dict = {
                        "id": pos_pubkey,
                        "asset_type": asset_type,
                        "position_type": side,
                        "entry_price": float(item.get("entryPrice", 0.0)),
                        "liquidation_price": float(item.get("liquidationPrice", 0.0)),
                        "collateral": float(item.get("collateral", 0.0)),
                        "size": float(item.get("size", 0.0)),
                        "leverage": float(item.get("leverage", 0.0)),
                        "value": float(item.get("value", 0.0)),
                        "last_updated": updated_dt.isoformat(),
                        "wallet_name": w["name"],
                        "pnl_after_fees_usd": float(item.get("pnlAfterFeesUsd", 0.0)),
                        "current_travel_percent": travel_percent
                    }
                    new_positions.append(pos_dict)
                except Exception as map_err:
                    app.logger.warning(f"Skipping item for wallet {w['name']} due to mapping error: {map_err}")

        new_count = 0
        duplicate_count = 0
        for p in new_positions:
            cursor = data_locker.conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM positions
                 WHERE id = ?
            """, (p["id"],))
            dup_count = cursor.fetchone()
            cursor.close()
            if dup_count[0] == 0:
                data_locker.create_position(p)
                new_count += 1
            else:
                duplicate_count += 1
                app.logger.info(f"Skipping duplicate Jupiter position: {p}")

        app.logger.info(f"Imported {new_count} new Jupiter position(s); Skipped {duplicate_count} duplicate(s).")
        all_positions = data_locker.get_positions()
        total_brokerage_value = sum(pos["value"] for pos in all_positions)
        balance_vars = data_locker.get_balance_vars()
        old_wallet_balance = balance_vars["total_wallet_balance"]
        new_total_balance = old_wallet_balance + total_brokerage_value
        data_locker.set_balance_vars(
            brokerage_balance=total_brokerage_value,
            total_balance=new_total_balance
        )
        msg = (f"Imported {new_count} new Jupiter position(s). "
               f"BrokerageBalance={total_brokerage_value:.2f}, TotalBalance={new_total_balance:.2f}")
        app.logger.info(msg)
        return jsonify({"message": msg}), 200
    except Exception as e:
        app.logger.error(f"Error in update_jupiter_positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/delete-all-jupiter-positions", methods=["POST"])
def delete_all_jupiter_positions():
    data_locker = DataLocker(DB_PATH)
    data_locker.cursor.execute("DELETE FROM positions WHERE wallet_name IS NOT NULL")
    data_locker.conn.commit()
    return jsonify({"message": "All Jupiter positions deleted."}), 200


@app.route("/delete-alert/<alert_id>", methods=["POST"])
def delete_alert(alert_id):
    data_locker = DataLocker(DB_PATH)
    data_locker.delete_alert(alert_id)
    flash("Alert deleted!", "success")
    return redirect(url_for("alerts"))


@app.route("/update_jupiter", methods=["POST"])
def update_jupiter():
    source = request.args.get("source") or request.form.get("source") or "API"
    data_locker = DataLocker(DB_PATH)
    delete_all_positions()
    jupiter_resp, jupiter_code = update_jupiter_positions()
    if jupiter_code != 200:
        return jupiter_resp, jupiter_code
    prices_resp = update_prices()
    if prices_resp.status_code != 200:
        return prices_resp
    manual_check_alerts()
    now = datetime.now()
    data_locker.set_last_update_times(
        positions_dt=now,
        positions_source=source,
        prices_dt=now,
        prices_source=source
    )
    socketio.emit('data_updated', {
        'message': f"Jupiter positions + Prices updated successfully by {source}!",
        'last_update_time_positions': now.isoformat(),
        'last_update_time_prices': now.isoformat()
    })
    return jsonify({
        "message": f"Jupiter positions + Prices updated successfully by {source}!",
        "source": source,
        "last_update_time_positions": now.isoformat(),
        "last_update_time_prices": now.isoformat()
    }), 200


@app.route("/latest_update_info")
def latest_update_info():
    data_locker = DataLocker.get_instance(DB_PATH)
    times = data_locker.get_last_update_times()
    if times is not None:
        times = dict(times)
    else:
        times = {}
    return jsonify({
        "last_update_time_positions": times.get("last_update_time_positions", "No Data"),
        "last_update_time_prices": times.get("last_update_time_prices", "No Data"),
        "last_update_time_jupiter": times.get("last_update_time_jupiter", "No Data")
    })


@app.route("/api/positions_data", methods=["GET"])
def positions_data_api():
    data_locker = DataLocker(DB_PATH)
    calc_services = CalcServices()
    mini_prices = []
    for asset in ["BTC", "ETH", "SOL"]:
        row = data_locker.get_latest_price(asset)
        if row:
            mini_prices.append({
                "asset_type": row["asset_type"],
                "current_price": float(row["current_price"])
            })
    positions_data = data_locker.read_positions()
    positions_data = fill_positions_with_latest_price(positions_data)
    updated_positions = calc_services.aggregator_positions(positions_data, DB_PATH)
    for pos in updated_positions:
        pos["collateral"] = float(pos.get("collateral") or 0.0)
        w_name = pos.get("wallet_name")
        if w_name:
            w_info = data_locker.get_wallet_by_name(w_name)
            pos["wallet_name"] = w_info or None
        else:
            pos["wallet_name"] = None

    config_data = load_app_config()
    alert_dict = config_data.alert_ranges or {}

    def get_alert_class(value, low, med, high):
        if high is None:
            high = float('inf')
        if med is None:
            med = float('inf')
        if low is None:
            low = float('-inf')
        if value >= high:
            return "alert-high"
        elif value >= med:
            return "alert-medium"
        elif value >= low:
            return "alert-low"
        else:
            return ""

    hi_cfg = alert_dict.get("heat_index_ranges", {})
    hi_low = hi_cfg.get("low", 0.0)
    hi_med = hi_cfg.get("medium", 0.0)
    hi_high = hi_cfg.get("high", None)

    coll_cfg = alert_dict.get("collateral_ranges", {})
    coll_low = coll_cfg.get("low", 0.0)
    coll_med = coll_cfg.get("medium", 0.0)
    coll_high = coll_cfg.get("high", None)

    val_cfg = alert_dict.get("value_ranges", {})
    val_low = val_cfg.get("low", 0.0)
    val_med = val_cfg.get("medium", 0.0)
    val_high = val_cfg.get("high", None)

    size_cfg = alert_dict.get("size_ranges", {})
    size_low = size_cfg.get("low", 0.0)
    size_med = size_cfg.get("medium", 0.0)
    size_high = size_cfg.get("high", None)

    lev_cfg = alert_dict.get("leverage_ranges", {})
    lev_low = lev_cfg.get("low", 0.0)
    lev_med = lev_cfg.get("medium", 0.0)
    lev_high = lev_cfg.get("high", None)

    liqd_cfg = alert_dict.get("liquidation_distance_ranges", {})
    liqd_low = liqd_cfg.get("low", 0.0)
    liqd_med = liqd_cfg.get("medium", 0.0)
    liqd_high = liqd_cfg.get("high", None)

    tliq_cfg = alert_dict.get("travel_percent_liquid_ranges", {})
    tliq_low = tliq_cfg.get("low", 0.0)
    tliq_med = tliq_cfg.get("medium", 0.0)
    tliq_high = tliq_cfg.get("high", None)

    tprof_cfg = alert_dict.get("travel_percent_profit_ranges", {})
    tprof_low = tprof_cfg.get("low", 0.0)
    tprof_med = tprof_cfg.get("medium", 0.0)
    tprof_high = tprof_cfg.get("high", None)

    profit_cfg = alert_dict.get("profit_ranges", {})
    profit_low = profit_cfg.get("low", 0.0)
    profit_med = profit_cfg.get("medium", 0.0)
    profit_high = profit_cfg.get("high", None)

    for pos in updated_positions:
        heat_val = float(pos.get("heat_index", 0.0))
        pos["heat_alert_class"] = get_alert_class(heat_val, hi_low, hi_med, hi_high)
        coll_val = float(pos.get("collateral", 0.0))
        pos["collateral_alert_class"] = get_alert_class(coll_val, coll_low, coll_med, coll_high)
        val = float(pos.get("value", 0.0))
        pos["value_alert_class"] = get_alert_class(val, val_low, val_med, val_high)
        sz = float(pos.get("size", 0.0))
        pos["size_alert_class"] = get_alert_class(sz, size_low, size_med, size_high)
        lev = float(pos.get("leverage", 0.0))
        pos["leverage_alert_class"] = get_alert_class(lev, lev_low, lev_med, lev_high)
        liqd = float(pos.get("liquidation_distance", 0.0))
        pos["liqdist_alert_class"] = get_alert_class(liqd, liqd_low, liqd_med, liqd_high)
        tliq_val = float(pos.get("current_travel_percent", 0.0))
        pos["travel_liquid_alert_class"] = get_alert_class(tliq_val, tliq_low, tliq_med, tliq_high)
        tprof_val = float(pos.get("current_travel_percent", 0.0))
        pos["travel_profit_alert_class"] = get_alert_class(tprof_val, tprof_low, tprof_med, tprof_high)
        profit_val = float(pos.get("pnl_after_fees_usd", 0.0))
        pos["profit_alert_class"] = get_alert_class(profit_val, profit_low, profit_med, profit_high)

    totals_dict = calc_services.calculate_totals(updated_positions)
    return jsonify({
        "mini_prices": mini_prices,
        "positions": updated_positions,
        "totals": totals_dict
    })


@app.route("/database-viewer")
def database_viewer():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT name 
          FROM sqlite_master
         WHERE type='table'
           AND name NOT LIKE 'sqlite_%'
         ORDER BY name
    """)
    tables = [row["name"] for row in cur.fetchall()]
    db_data = {}
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        columns = [col["name"] for col in cur.fetchall()]
        cur.execute(f"SELECT * FROM {table}")
        rows_raw = cur.fetchall()
        rows = [dict(r) for r in rows_raw]
        db_data[table] = {"columns": columns, "rows": rows}
    conn.close()
    return render_template("database_viewer.html", db_data=db_data)


@app.route("/test-jupiter-perps-proxy")
def test_jupiter_perps_proxy():
    return render_template("test_jupiter_perps.html")


@app.route("/console-test")
def console_test():
    return render_template("console_test.html")


@app.route("/test-jupiter-swap", methods=["GET"])
def test_jupiter_swap():
    return render_template("test_jupiter_swap.html")


def load_app_config() -> AppConfig:
    if not os.path.exists(CONFIG_PATH):
        return AppConfig()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AppConfig(**data)


def save_app_config(config: AppConfig):
    data = config.model_dump()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def fill_positions_with_latest_price(positions: List[dict]) -> List[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    for pos in positions:
        asset = pos.get("asset_type", "BTC").upper()
        if pos.get("current_price", 0.0) > 0:
            continue
        row = cursor.execute("""
            SELECT current_price
              FROM prices
             WHERE asset_type = ?
             ORDER BY last_update_time DESC
             LIMIT 1
        """, (asset,)).fetchone()
        if row:
            pos["current_price"] = float(row["current_price"])
        else:
            pos["current_price"] = 0.0
    conn.close()
    return positions


def _get_top_prices_for_assets(db_path, assets=None):
    if assets is None:
        assets = ["BTC", "ETH", "SOL"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    results = []
    for asset in assets:
        row = cur.execute("""
            SELECT asset_type, current_price, last_update_time
              FROM prices
             WHERE asset_type = ?
             ORDER BY last_update_time DESC
             LIMIT 1
        """, (asset,)).fetchone()
        if row:
            iso = row["last_update_time"]
            results.append({
                "asset_type": row["asset_type"],
                "current_price": row["current_price"],
                "last_update_time_pst": _convert_iso_to_pst(iso)
            })
        else:
            results.append({
                "asset_type": asset,
                "current_price": 0.0,
                "last_update_time_pst": "N/A"
            })
    conn.close()
    return results


def _get_recent_prices(db_path, limit=15):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(f"""
        SELECT asset_type, current_price, last_update_time
          FROM prices
         ORDER BY last_update_time DESC
         LIMIT {limit}
    """)
    rows = cur.fetchall()
    conn.close()
    results = []
    for r in rows:
        iso = r["last_update_time"]
        results.append({
            "asset_type": r["asset_type"],
            "current_price": r["current_price"],
            "last_update_time_pst": _convert_iso_to_pst(iso)
        })
    return results


def build_heat_data(positions: List[dict]) -> dict:
    structure = {
        "BTC": {"short": {}, "long": {}},
        "ETH": {"short": {}, "long": {}},
        "SOL": {"short": {}, "long": {}},
        "totals": {
            "short": {"asset": "Short", "collateral": 0.0, "value": 0.0, "leverage": 0.0, "travel_percent": 0.0,
                      "heat_index": 0.0, "size": 0.0},
            "long": {"asset": "Long", "collateral": 0.0, "value": 0.0, "leverage": 0.0, "travel_percent": 0.0,
                     "heat_index": 0.0, "size": 0.0}
        }
    }
    for pos in positions:
        asset = pos.get("asset_type", "BTC").upper()
        side = pos.get("position_type", "LONG").lower()
        if asset not in ["BTC", "ETH", "SOL"]:
            continue
        if side not in ["short", "long"]:
            continue
        row = {
            "asset": asset,
            "collateral": float(pos.get("collateral", 0.0)),
            "value": float(pos.get("value", 0.0)),
            "leverage": float(pos.get("leverage", 0.0)),
            "travel_percent": float(pos.get("current_travel_percent", 0.0)),
            "heat_index": float(pos.get("heat_index", 0.0)),
            "size": float(pos.get("size", 0.0))
        }
        structure[asset][side] = row
        totals_side = structure["totals"][side]
        totals_side["collateral"] += row["collateral"]
        totals_side["value"] += row["value"]
        totals_side["size"] += row["size"]
        totals_side["travel_percent"] += row["travel_percent"]
        totals_side["heat_index"] += row["heat_index"]
    return structure


@app.route("/assets")
@app.route("/assets")
def assets():
    data_locker = DataLocker(DB_PATH)
    brokers = data_locker.read_brokers()
    brokers.sort(key=lambda b: b["total_holding"], reverse=True)
    wallets = data_locker.read_wallets()
    wallets.sort(key=lambda w: w["balance"], reverse=True)
    balance_dict = data_locker.get_balance_vars()
    return render_template(
        "assets.html",
        brokers=brokers,
        wallets=wallets,
        total_brokerage_balance=balance_dict["total_brokerage_balance"],
        total_balance=balance_dict["total_balance"],
        total_wallet_balance=balance_dict["total_wallet_balance"]
    )


@app.route("/add_wallet", methods=["POST"])
def add_wallet():
    data_locker = DataLocker(DB_PATH)
    name = request.form.get("name", "").strip()
    public_addr = request.form.get("public_address", "").strip()
    private_addr = request.form.get("private_address", "").strip()
    image_path = request.form.get("image_path", "").strip()
    balance_str = request.form.get("balance", "0.0").strip()
    try:
        balance_val = float(balance_str)
    except ValueError:
        balance_val = 0.0
    wallet_dict = {
        "name": name,
        "public_address": public_addr,
        "private_address": private_addr,
        "image_path": image_path,
        "balance": balance_val
    }
    data_locker.create_wallet(wallet_dict)
    flash(f"Wallet '{name}' added!", "success")
    return redirect(url_for("assets"))


@app.route("/add_broker", methods=["POST"])
def add_broker():
    data_locker = DataLocker(DB_PATH)
    name = request.form.get("name", "").strip()
    image_path = request.form.get("image_path", "").strip()
    web_address = request.form.get("web_address", "").strip()
    holding_str = request.form.get("total_holding", "0.0").strip()
    try:
        holding_val = float(holding_str)
    except ValueError:
        holding_val = 0.0
    broker_dict = {
        "name": name,
        "image_path": image_path,
        "web_address": web_address,
        "total_holding": holding_val
    }
    data_locker.create_broker(broker_dict)
    flash(f"Broker '{name}' added!", "success")
    return redirect(url_for("assets"))


@app.route("/delete_wallet/<wallet_name>", methods=["POST"])
def delete_wallet(wallet_name):
    data_locker = DataLocker(DB_PATH)
    conn = data_locker.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM wallets WHERE name = ?", (wallet_name,))
    conn.commit()
    flash(f"Deleted wallet '{wallet_name}'.", "info")
    return redirect(url_for("assets"))


@app.route("/delete_broker/<broker_name>", methods=["POST"])
def delete_broker(broker_name):
    data_locker = DataLocker(DB_PATH)
    conn = data_locker.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM brokers WHERE name = ?", (broker_name,))
    conn.commit()
    flash(f"Deleted broker '{broker_name}'.", "info")
    return redirect(url_for("assets"))


@app.route("/price_charts")
@app.route("/price_charts")
def price_charts():
    hours = request.args.get("hours", default=6, type=int)
    cutoff_time = datetime.now() - timedelta(hours=hours)
    cutoff_iso = cutoff_time.isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    chart_data = {"BTC": [], "ETH": [], "SOL": []}
    for asset in ["BTC", "ETH", "SOL"]:
        cur.execute(
            """
            SELECT current_price, last_update_time
              FROM prices
             WHERE asset_type = ?
               AND last_update_time >= ?
             ORDER BY last_update_time ASC
            """,
            (asset, cutoff_iso)
        )
        rows = cur.fetchall()
        for row in rows:
            iso_str = row["last_update_time"]
            price = float(row["current_price"])
            dt_obj = datetime.fromisoformat(iso_str)
            epoch_ms = int(dt_obj.timestamp() * 1000)
            chart_data[asset].append([epoch_ms, price])
    conn.close()
    return render_template("price_charts.html", chart_data=chart_data, timeframe=hours)


@app.route('/update_alert_config', methods=['POST'])
def update_alert_config():
    try:
        config = load_json_config("sonic_config.json")
        form_data = request.form.to_dict(flat=True)
        updated_alerts = parse_alert_config_form(form_data)
        config["alert_ranges"] = updated_alerts
        save_config(config, "sonic_config.json")
        manager.reload_config()
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Error updating alert config: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/hedge-report")
def hedge_report():
    data_locker = DataLocker(DB_PATH)
    conn = data_locker.get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    def fetch_asset_data(asset):
        cur.execute(
            """
            SELECT current_price, last_update_time
              FROM prices
             WHERE asset_type = ?
               AND last_update_time >= datetime('now','-24 hours')
             ORDER BY last_update_time ASC
            """, (asset,)
        )
        return cur.fetchall()

    chart_data = {"BTC": [], "ETH": [], "SOL": []}
    for asset in ["BTC", "ETH", "SOL"]:
        rows = fetch_asset_data(asset)
        for r in rows:
            iso_str = r["last_update_time"]
            price = float(r["current_price"])
            dt_obj = datetime.fromisoformat(iso_str)
            epoch_ms = int(dt_obj.timestamp() * 1000)
            chart_data[asset].append([epoch_ms, price])
    print("DEBUG: chart_data =>", chart_data)
    return render_template("hedge_report.html", chart_data=chart_data)


@app.route("/alert-config", methods=["GET"])
def alert_config_page():
    return render_template("alert_manager_config.html")


@app.route("/positions_mobile")
def positions_mobile():
    data_locker = DataLocker(DB_PATH)
    calc_services = CalcServices()
    positions_data = data_locker.read_positions()
    positions_data = fill_positions_with_latest_price(positions_data)
    updated_positions = calc_services.aggregator_positions(positions_data, DB_PATH)
    for pos in updated_positions:
        pos["collateral"] = float(pos.get("collateral") or 0.0)
        wallet_name = pos.get("wallet_name")
        if wallet_name:
            w = data_locker.get_wallet_by_name(wallet_name)
            pos["wallet_name"] = w
        else:
            pos["wallet_name"] = None
    config_data = load_app_config()
    alert_dict = config_data.alert_ranges or {}

    def get_alert_class_local(value, low_thresh, med_thresh, high_thresh):
        try:
            low_thresh = float(low_thresh) if low_thresh not in (None, "") else float('-inf')
        except ValueError:
            low_thresh = float('-inf')
        try:
            med_thresh = float(med_thresh) if med_thresh not in (None, "") else float('inf')
        except ValueError:
            med_thresh = float('inf')
        try:
            high_thresh = float(high_thresh) if high_thresh not in (None, "") else float('inf')
        except ValueError:
            high_thresh = float('inf')
        if value >= high_thresh:
            return "alert-high"
        elif value >= med_thresh:
            return "alert-medium"
        elif value >= low_thresh:
            return "alert-low"
        else:
            return ""

    hi_cfg = alert_dict.get("heat_index_ranges", {})
    hi_low = hi_cfg.get("low", 0.0)
    hi_med = hi_cfg.get("medium", 0.0)
    hi_high = hi_cfg.get("high", None)
    coll_cfg = alert_dict.get("collateral_ranges", {})
    coll_low = coll_cfg.get("low", 0.0)
    coll_med = coll_cfg.get("medium", 0.0)
    coll_high = coll_cfg.get("high", None)
    val_cfg = alert_dict.get("value_ranges", {})
    val_low = val_cfg.get("low", 0.0)
    val_med = val_cfg.get("medium", 0.0)
    val_high = val_cfg.get("high", None)
    size_cfg = alert_dict.get("size_ranges", {})
    size_low = size_cfg.get("low", 0.0)
    size_med = size_cfg.get("medium", 0.0)
    size_high = size_cfg.get("high", None)
    lev_cfg = alert_dict.get("leverage_ranges", {})
    lev_low = lev_cfg.get("low", 0.0)
    lev_med = lev_cfg.get("medium", 0.0)
    lev_high = lev_cfg.get("high", None)
    liqd_cfg = alert_dict.get("liquidation_distance_ranges", {})
    liqd_low = liqd_cfg.get("low", 0.0)
    liqd_med = liqd_cfg.get("medium", 0.0)
    liqd_high = liqd_cfg.get("high", None)
    tliq_cfg = alert_dict.get("travel_percent_liquid_ranges", {})
    tliq_low = tliq_cfg.get("low", 0.0)
    tliq_med = tliq_cfg.get("medium", 0.0)
    tliq_high = tliq_cfg.get("high", None)
    tprof_cfg = alert_dict.get("travel_percent_profit_ranges", {})
    tprof_low = tprof_cfg.get("low", 0.0)
    tprof_med = tprof_cfg.get("medium", 0.0)
    tprof_high = tprof_cfg.get("high", None)
    profit_cfg = alert_dict.get("profit_ranges", {})
    profit_low = profit_cfg.get("low", 0.0)
    profit_med = profit_cfg.get("medium", 0.0)
    profit_high = profit_cfg.get("high", None)
    for pos in updated_positions:
        heat_val = float(pos.get("heat_index", 0.0))
        pos["heat_alert_class"] = get_alert_class_local(heat_val, hi_low, hi_med, hi_high)
        coll_val = float(pos.get("collateral", 0.0))
        pos["collateral_alert_class"] = get_alert_class_local(coll_val, coll_low, coll_med, coll_high)
        val = float(pos.get("value", 0.0))
        pos["value_alert_class"] = get_alert_class_local(val, val_low, val_med, val_high)
        sz = float(pos.get("size", 0.0))
        pos["size_alert_class"] = get_alert_class_local(sz, size_low, size_med, size_high)
        lev = float(pos.get("leverage", 0.0))
        pos["leverage_alert_class"] = get_alert_class_local(lev, lev_low, lev_med, lev_high)
        liqd = float(pos.get("liquidation_distance", 0.0))
        pos["liqdist_alert_class"] = get_alert_class_local(liqd, liqd_low, liqd_med, liqd_high)
        tliq_val = float(pos.get("current_travel_percent", 0.0))
        pos["travel_liquid_alert_class"] = get_alert_class_local(tliq_val, tliq_low, tliq_med, tliq_high)
        tprof_val = float(pos.get("current_travel_percent", 0.0))
        pos["travel_profit_alert_class"] = get_alert_class_local(tprof_val, tprof_low, tprof_med, tprof_high)
        profit_val = float(pos.get("pnl_after_fees_usd", 0.0))
        pos["profit_alert_class"] = get_profit_alert_class_local(profit_val, profit_low, profit_med, profit_high)
        times_dict = data_locker.get_last_update_times()
        if times_dict is not None:
            times_dict = dict(times_dict)
        else:
            times_dict = {}
        pos_time_iso = times_dict.get("last_update_time_positions", "N/A")
    pos_time_iso = times_dict.get("last_update_time_positions", "N/A")

    def _convert_iso_to_pst(iso_str):
        if not iso_str or iso_str == "N/A":
            return "N/A"
        pst = pytz.timezone("US/Pacific")
        try:
            dt_obj = datetime.fromisoformat(iso_str)
            dt_pst = dt_obj.astimezone(pst)
            return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
        except:
            return "N/A"

    pos_time_formatted = _convert_iso_to_pst(pos_time_iso)
    return render_template("positions_mobile.html",
                           positions=updated_positions,
                           totals=totals_dict,
                           last_update_positions=pos_time_formatted,
                           last_update_positions_source=times_dict.get("last_update_positions_source", "N/A"))


def parse_alert_config_form(form_data: dict) -> dict:
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


@app.route("/api/update_config", methods=["POST"])
@app.route('/api/update_config', methods=['POST'])
def api_update_config():
    try:
        new_config = request.get_json()
        if not new_config or "alert_ranges" not in new_config:
            return jsonify({"error": "No alert configuration data provided"}), 400
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        else:
            existing_data = {}
        existing_data["alert_ranges"] = new_config["alert_ranges"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2)
        manager.reload_config()
        return jsonify({"message": "Alert configuration updated and reloaded successfully"}), 200
    except Exception as e:
        logger.exception("Error updating alert configuration")
        return jsonify({"error": str(e)}), 500


@app.route('/save_theme', methods=['POST'])
def save_theme():
    try:
        new_theme_data = request.get_json()
        if not new_theme_data:
            return jsonify({"success": False, "error": "No data received"}), 400
        config_path = current_app.config.get("CONFIG_PATH", CONFIG_PATH)
        with open(config_path, 'r') as f:
            config = json.load(f)
        config.setdefault("theme_profiles", {})
        config["theme_profiles"]["sidebar"] = new_theme_data.get(
            "sidebar", config["theme_profiles"].get("sidebar", {})
        )
        config["theme_profiles"]["navbar"] = new_theme_data.get(
            "navbar", config["theme_profiles"].get("navbar", {})
        )
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        return jsonify({"success": True})
    except Exception as e:
        current_app.logger.error("Error saving theme: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.context_processor
def update_theme():
    config_path = current_app.config.get("CONFIG_PATH", CONFIG_PATH)
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except Exception as e:
        config = {}
    theme = {
        'sidebar': {
            'bg': config.get('sidebar_bg', 'bg-primary'),
            'color_mode': config.get('sidebar_mode', 'dark')
        },
        'navbar': {
            'bg': config.get('navbar_bg', 'bg-secondary'),
            'color_mode': config.get('navbar_mode', 'dark')
        }
    }
    return dict(theme=theme)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)


def parse_alert_config_form(form_data: dict) -> dict:
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
