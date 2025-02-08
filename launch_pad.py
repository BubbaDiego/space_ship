#!/usr/bin/env python
import os
import time
import json
import smtplib
import logging
import sqlite3
from config.config_manager import load_config, load_json_config, update_config, deep_merge_dicts
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
from data.data_locker import DataLocker
from config.config_manager import load_config  # local import if needed
from config.config import AppConfig
from utils.calc_services import CalcServices
from price_monitor import PriceMonitor
from alerts.alert_manager import AlertManager

# NEW: Import the PositionService for positions-related functionality.
from positions.position_service import PositionService

from config.config_constants import DB_PATH, CONFIG_PATH, BASE_DIR

# Import the positions blueprint from positions_bp.py.
from positions.positions_bp import positions_bp
print(positions_bp)  # For debugging: confirms blueprint import

# =============================================================================
# Setup: Paths, configuration, and Twilio/Logging
# =============================================================================
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

twilio_config = config.get("twilio_config", {})
TWILIO_ACCOUNT_SID = twilio_config.get("account_sid")
TWILIO_AUTH_TOKEN = twilio_config.get("auth_token")
TWILIO_FLOW_SID = twilio_config.get("flow_sid")
TWILIO_TO_PHONE = twilio_config.get("to_phone")
TWILIO_FROM_PHONE = twilio_config.get("from_phone")

logger = logging.getLogger("WebAppLogger")
logger.setLevel(logging.DEBUG)

app = Flask(__name__)
app.debug = False
app.secret_key = "i-like-lamp"

# Register the positions blueprint with the prefix "/positions"
app.register_blueprint(positions_bp, url_prefix="/positions")

MINT_TO_ASSET = {
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
    "So11111111111111111111111111111111111111112": "SOL"
}

manager = AlertManager(
    db_path=DB_PATH,
    poll_interval=60,
    config_path=CONFIG_PATH
)

socketio = SocketIO(app)

# =============================================================================
# Main Application Routes (Non‑Positions)
# =============================================================================
@app.route('/')
def index():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    theme = config.get("theme_profiles", {})
    return render_template("base.html", theme=theme, title="Sonic Dashboard")

@app.route('/theme')
def theme_options():
    return render_template('theme.html')

@app.route("/add_broker", methods=["POST"])
def add_broker():
    dl = DataLocker.get_instance(DB_PATH)
    broker_dict = {
        "name": request.form.get("name"),
        "image_path": request.form.get("image_path"),
        "web_address": request.form.get("web_address"),
        "total_holding": float(request.form.get("total_holding", 0.0))
    }
    try:
        dl.create_broker(broker_dict)
        flash(f"Broker {broker_dict['name']} added successfully!", "success")
    except Exception as e:
        flash(f"Error adding broker: {e}", "danger")
    return redirect(url_for("assets"))

@app.route("/delete_wallet/<wallet_name>", methods=["POST"])
def delete_wallet(wallet_name):
    dl = DataLocker.get_instance(DB_PATH)
    try:
        wallet = dl.get_wallet_by_name(wallet_name)
        if wallet is None:
            flash(f"Wallet '{wallet_name}' not found.", "danger")
        else:
            dl._init_sqlite_if_needed()
            dl.cursor.execute("DELETE FROM wallets WHERE name=?", (wallet_name,))
            dl.conn.commit()
            flash(f"Wallet '{wallet_name}' deleted successfully!", "success")
    except Exception as e:
        flash(f"Error deleting wallet: {e}", "danger")
    return redirect(url_for("assets"))

@app.route("/add_wallet", methods=["POST"])
def add_wallet():
    dl = DataLocker.get_instance(DB_PATH)
    balance_str = request.form.get("balance", "0.0")
    if balance_str.strip() == "":
        balance_str = "0.0"
    try:
        balance_value = float(balance_str)
    except ValueError:
        balance_value = 0.0

    wallet = {
        "name": request.form.get("name"),
        "public_address": request.form.get("public_address"),
        "private_address": request.form.get("private_address"),
        "image_path": request.form.get("image_path"),
        "balance": balance_value
    }
    try:
        dl.create_wallet(wallet)
        flash(f"Wallet {wallet['name']} added successfully!", "success")
    except Exception as e:
        flash(f"Error adding wallet: {e}", "danger")
    return redirect(url_for("assets"))

@app.route("/assets")
def assets():
    dl = DataLocker.get_instance(DB_PATH)
    balance_vars = dl.get_balance_vars()
    total_brokerage_balance = balance_vars.get("total_brokerage_balance", 0.0)
    total_wallet_balance = balance_vars.get("total_wallet_balance", 0.0)
    total_balance = balance_vars.get("total_balance", 0.0)
    brokers = dl.read_brokers()
    wallets = dl.read_wallets()
    return render_template("assets.html",
                           total_brokerage_balance=total_brokerage_balance,
                           total_wallet_balance=total_wallet_balance,
                           total_balance=total_balance,
                           brokers=brokers,
                           wallets=wallets)

# ---------------------------------------------------------------------------
# Dedicated Price Charts Route
# ---------------------------------------------------------------------------
@app.route("/price_charts", endpoint="price_charts_view")
def price_charts_view():
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
        return ""
    elif profit < med:
        return "alert-low"
    elif profit < high:
        return "alert-medium"
    else:
        return "alert-high"

@app.route("/exchanges")
def exchanges():
    data_locker = DataLocker(DB_PATH)
    brokers_data = data_locker.read_brokers()
    return render_template("exchanges.html", brokers=brokers_data)

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
    return render_template("prices.html", prices=top_prices, recent_prices=recent_prices, api_counters=api_counters)

@app.route("/update-prices", methods=["POST"])
def update_prices():
    source = request.args.get("source") or request.form.get("source") or "API"
    pm = PriceMonitor(db_path=DB_PATH, config_path=CONFIG_PATH)
    try:
        asyncio.run(pm.update_prices())
        data_locker = DataLocker(DB_PATH)
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

@app.route('/console_view')
def console_view():
    log_url = "https://www.pythonanywhere.com/user/BubbaDiego/files/var/log/www.deadlypanda.com.error.log"
    return render_template("console_view.html", log_url=log_url)

# ---------------------------------------------------------------------------
# Updated /heat Route: Redirect to positions blueprint's heat route.
# ---------------------------------------------------------------------------
@app.route("/heat", methods=["GET"], endpoint="heat")
def heat_redirect():
    return redirect(url_for("positions.heat"))

@app.route("/alert-config", methods=["GET"])
def alert_config_page():
    return render_template("alert_manager_config.html")

@app.route("/alerts")
def alerts():
    try:
        config_data = load_config()
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
        save_app_config(config)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def parse_nested_form(form_dict):
    nested = {}
    for full_key, value in form_dict.items():
        if full_key.startswith("alert_ranges[") and full_key.endswith("]"):
            inner = full_key[len("alert_ranges["):-1]
            parts = inner.split("][")
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
        save_app_config(config)
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

def record_positions_snapshot(db_path: str):
    """
    Retrieves all enriched positions, calculates aggregated totals (including average
    liquidation distance), and stores a snapshot in the positions_totals_history table.
    """
    positions = PositionService.get_all_positions(db_path)
    totals = calculate_positions_totals_with_liquidation(positions)
    dl = DataLocker.get_instance(db_path)
    dl.record_positions_totals_snapshot(totals)

@app.route("/position_trends")
@app.route("/position_trends")
def position_trends():
    dl = DataLocker.get_instance(DB_PATH)
    positions = dl.get_positions()
    calc_services = CalcServices()
    totals = calc_services.calculate_totals(positions)
    current_timestamp = int(datetime.now().timestamp() * 1000)
    chart_data = {
        "collateral": [[current_timestamp, totals.get("total_collateral", 0)]],
        "value": [[current_timestamp, totals.get("total_value", 0)]],
        "size": [[current_timestamp, totals.get("total_size", 0)]],
        "leverage": [[current_timestamp, totals.get("avg_leverage", 0)]],
        "travel_percent": [[current_timestamp, totals.get("avg_travel_percent", 0)]],
        "heat": [[current_timestamp, totals.get("avg_heat_index", 0)]],
        "liquidation_distance": [[current_timestamp, totals.get("avg_liquidation_distance", 0)]]
    }
    timeframe = 24
    return render_template("position_trends.html", chart_data=chart_data, timeframe=timeframe)

@app.route("/delete-alert/<alert_id>", methods=["POST"])
def delete_alert(alert_id):
    data_locker = DataLocker(DB_PATH)
    data_locker.delete_alert(alert_id)
    flash("Alert deleted!", "success")
    return redirect(url_for("alerts"))

@app.route("/update_jupiter", methods=["GET", "POST"])
def update_jupiter():
    source = request.args.get("source") or request.form.get("source") or "API"
    data_locker = DataLocker(DB_PATH)
    delete_all_jupiter_positions()
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
    try:
        record_positions_snapshot(DB_PATH)
    except Exception as e:
        logger.error(f"Error recording positions snapshot: {e}", exc_info=True)
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

@app.route('/update_alert_config', methods=['POST'])
def update_alert_config():
    try:
        config = load_config("sonic_config.json")
        form_data = request.form.to_dict(flat=True)
        updated_alerts = parse_nested_form(form_data)
        config["alert_ranges"] = updated_alerts
        updated_config = update_config(config, "sonic_config.json")
        manager.reload_config()
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Error updating alert config: %s", e, exc_info=True)
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
        config["theme_profiles"]["sidebar"] = new_theme_data.get("sidebar", config["theme_profiles"].get("sidebar", {}))
        config["theme_profiles"]["navbar"] = new_theme_data.get("navbar", config["theme_profiles"].get("navbar", {}))
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

# ---------------------------------------------------------------------------
# Ensure all routes are defined before running the server.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)

# ---------------------------------------------------------------------------
# Helper: Parse Alert Config Form
# ---------------------------------------------------------------------------
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
