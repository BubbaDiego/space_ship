#!/usr/bin/env python
"""
launch_pad.py
Description:
  The main Flask application for the Sonic Dashboard. This file:
    - Loads configuration and sets up logging.
    - Initializes the Flask app and SocketIO.
    - Registers blueprints for positions, alerts, prices, dashboard, **and portfolio**.
    - Defines global routes for non-dashboard-specific functionality (e.g., assets, exchanges, etc.).
    - Optionally launches a local monitor (local_monitor.py) in a new console window if
      the '--monitor' command-line flag is provided.

Usage:
  To run normally:
      python launch_pad.py
  To run with the local monitor:
      python launch_pad.py --monitor
"""

import os
import sys
import json
import logging
import sqlite3
import asyncio
import pytz
import requests
from datetime import datetime, timedelta
from uuid import uuid4

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, current_app
from flask_socketio import SocketIO, emit

# Import configuration and data modules
from config.config_constants import DB_PATH, CONFIG_PATH, BASE_DIR
from config.config_manager import load_config, update_config, deep_merge_dicts
from data.data_locker import DataLocker
from positions.position_service import PositionService
from prices.price_monitor import PriceMonitor

# Import blueprints – ensure your directories have an __init__.py file
from positions.positions_bp import positions_bp
from alerts.alerts_bp import alerts_bp
from prices.prices_bp import prices_bp
from dashboard.dashboard_bp import dashboard_bp  # Dashboard-specific routes and API endpoints

# *** NEW: Import the portfolio blueprint ***
from portfolio.portfolio_bp import portfolio_bp

# Setup logging
logger = logging.getLogger("WebAppLogger")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(levelname)s] %(asctime)s - %(name)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# Load configuration
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

# Initialize Flask app and SocketIO
app = Flask(__name__)
app.debug = False
app.secret_key = "i-like-lamp"
socketio = SocketIO(app)

# Register Blueprints
app.register_blueprint(positions_bp, url_prefix="/positions")
app.register_blueprint(alerts_bp, url_prefix="/alerts")
app.register_blueprint(prices_bp, url_prefix="/prices")
app.register_blueprint(dashboard_bp)  # Dashboard-specific routes and API endpoints

# *** NEW: Register the portfolio blueprint ***
app.register_blueprint(portfolio_bp, url_prefix="/portfolio")

# --- Alias endpoints if needed ---
# For example, if your base.html uses url_for('dashboard') we can alias it:
if "dashboard.index" in app.view_functions:
    app.add_url_rule("/dashboard", endpoint="dashboard", view_func=app.view_functions["dashboard.index"])

# Global Routes for non-dashboard-specific functionality

@app.route("/")
def index():
    theme = config.get("theme_profiles", {})
    return render_template("base.html", theme=theme, title="Sonic Dashboard")

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

@app.route("/exchanges")
def exchanges():
    dl = DataLocker.get_instance(DB_PATH)
    brokers_data = dl.read_brokers()
    return render_template("exchanges.html", brokers=brokers_data)

@app.route("/edit_wallet/<wallet_name>", methods=["GET", "POST"])
def edit_wallet(wallet_name):
    dl = DataLocker.get_instance(DB_PATH)
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
        dl.update_wallet(wallet_name, wallet_dict)
        flash(f"Wallet '{name}' updated successfully!", "success")
        return redirect(url_for("assets"))
    else:
        wallet = dl.get_wallet_by_name(wallet_name)
        if not wallet:
            flash(f"Wallet '{wallet_name}' not found.", "danger")
            return redirect(url_for("assets"))
        return render_template("edit_wallet.html", wallet=wallet)




@app.route("/console_view")
def console_view():
    log_url = "https://www.pythonanywhere.com/user/BubbaDiego/files/var/log/www.deadlypanda.com.error.log"
    return render_template("console_view.html", log_url=log_url)

@app.route("/api/get_config")
def api_get_config():
    try:
        conf = load_config()
        logger.debug("Loaded config: %s", conf)
        return jsonify(conf)
    except Exception as e:
        logger.error("Error loading config: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/save_theme", methods=["POST"])
def save_theme_route():
    try:
        new_theme_data = request.get_json()
        if not new_theme_data:
            return jsonify({"success": False, "error": "No data received"}), 400
        config_path = current_app.config.get("CONFIG_PATH", CONFIG_PATH)
        with open(config_path, 'r') as f:
            conf = json.load(f)
        conf.setdefault("theme_profiles", {})
        conf["theme_profiles"]["sidebar"] = new_theme_data.get("sidebar", conf["theme_profiles"].get("sidebar", {}))
        conf["theme_profiles"]["navbar"] = new_theme_data.get("navbar", conf["theme_profiles"].get("navbar", {}))
        with open(config_path, 'w') as f:
            json.dump(conf, f, indent=2)
        return jsonify({"success": True})
    except Exception as e:
        current_app.logger.error("Error saving theme: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


# API endpoint to update a row
@app.route('/api/update_row', methods=['POST'])
def api_update_row():
    try:
        data = request.get_json()
        table = data.get('table')
        pk_field = data.get('pk_field')
        pk_value = data.get('pk_value')
        row_data = data.get('row')

        dl = DataLocker.get_instance()

        if table == 'wallets':
            # Use the update_wallet method
            dl.update_wallet(pk_value, row_data)
        elif table == 'positions':
            # You'd implement a similar method for positions
            # Example: dl.update_position(pk_value, row_data['size'], row_data['collateral'])
            pass
        else:
            # Handle other tables as needed
            pass

        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.exception("Error updating row")
        return jsonify({"error": str(e)}), 500


# API endpoint to delete a row
@app.route('/api/delete_row', methods=['POST'])
def api_delete_row():
    try:
        data = request.get_json()
        table = data.get('table')
        pk_field = data.get('pk_field')
        pk_value = data.get('pk_value')

        dl = DataLocker.get_instance()

        if table == 'wallets':
            return jsonify({"error": "Wallet deletion is disabled"}), 400
        elif table == 'positions':
            dl.delete_position(pk_value)
        else:
            # Handle other tables as needed
            pass

        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.exception("Error deleting row")
        return jsonify({"error": str(e)}), 500


# Route to render the database viewer
@app.route('/database-viewer')
def database_viewer():
    dl = DataLocker.get_instance()

    # Build db_data for wallets (add other tables as needed)
    db_data = {
        'wallets': {
            "columns": ["name", "public_address", "private_address", "image_path", "balance"],
            "rows": dl.read_wallets()
        }
        # You can add other tables like positions, alerts, etc.
    }

    # For portfolio_data, supply an empty list or actual data if available.
    portfolio_data = []
    return render_template("database_viewer.html", db_data=db_data, portfolio_data=portfolio_data)


# ---------------------------------------------------------------------------
# Context Processor
# ---------------------------------------------------------------------------
@app.context_processor
def update_theme_context():
    config_path = current_app.config.get("CONFIG_PATH", CONFIG_PATH)
    try:
        with open(config_path, 'r') as f:
            conf = json.load(f)
    except Exception as e:
        conf = {}
    theme = {
        'sidebar': {
            'bg': conf.get('sidebar_bg', 'bg-primary'),
            'color_mode': conf.get('sidebar_mode', 'dark')
        },
        'navbar': {
            'bg': conf.get('navbar_bg', 'bg-secondary'),
            'color_mode': conf.get('navbar_mode', 'dark')
        }
    }
    return dict(theme=theme)

# ---------------------------------------------------------------------------
# Run the Application
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    monitor = True
    if len(sys.argv) > 1 and sys.argv[1] == "--monitor":
        monitor = True

    if monitor:
        import subprocess
        try:
            CREATE_NEW_CONSOLE = 0x00000010  # Windows flag for new console window
            monitor_script = os.path.join(BASE_DIR, "local_monitor.py")
            subprocess.Popen(["python", monitor_script], creationflags=CREATE_NEW_CONSOLE)
            logger.info("Launched local_monitor.py in a new console window.")
        except Exception as e:
            logger.error(f"Error launching local_monitor.py: {e}", exc_info=True)

    app.run(debug=True, host="0.0.0.0", port=5001)
