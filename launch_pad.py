#!/usr/bin/env python
import os
import json
import logging
import sqlite3
import asyncio
import pytz
import requests
from datetime import datetime, timedelta
from uuid import uuid4

from flask import (
    Flask, request, jsonify, render_template, redirect, url_for, flash, current_app
)
from flask_socketio import SocketIO, emit
from utils.calc_services import CalcServices

# Import configuration and data modules
from config.config_manager import load_config, update_config
from config.config_constants import DB_PATH, CONFIG_PATH, BASE_DIR
from data.data_locker import DataLocker

# Import blueprints – positions, alerts, and prices are now handled in their own modules.
from positions.positions_bp import positions_bp
from alerts.alerts_bp import alerts_bp
from prices.prices_bp import prices_bp

# -----------------------------------------------------------------------------
# Logging & Configuration
# -----------------------------------------------------------------------------
logger = logging.getLogger("WebAppLogger")
logger.setLevel(logging.DEBUG)

# Load configuration file
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

# -----------------------------------------------------------------------------
# Initialize Flask App & SocketIO
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.debug = False
app.secret_key = "i-like-lamp"

socketio = SocketIO(app)

# Register Blueprints – each encapsulates domain-specific routes.
app.register_blueprint(positions_bp, url_prefix="/positions")
app.register_blueprint(alerts_bp, url_prefix="/alerts")
app.register_blueprint(prices_bp, url_prefix="/prices")

# -----------------------------------------------------------------------------
# Global Application Routes (Generic Routes)
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    """Render the main dashboard."""
    theme = config.get("theme_profiles", {})
    return render_template("base.html", theme=theme, title="Sonic Dashboard")

@app.route("/theme")
def theme_options():
    """Render the theme options page."""
    return render_template("theme.html")

@app.route("/add_broker", methods=["POST"])
def add_broker():
    """Endpoint for adding a broker."""
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
    """Endpoint for deleting a wallet."""
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
    """Endpoint for adding a wallet."""
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
    """Renders the assets page with balance info, brokers, and wallets."""
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
    """Render the exchanges page with broker information."""
    dl = DataLocker.get_instance(DB_PATH)
    brokers_data = dl.read_brokers()
    return render_template("exchanges.html", brokers=brokers_data)

@app.route("/edit_wallet/<wallet_name>", methods=["GET", "POST"])
def edit_wallet(wallet_name):
    """Endpoint for editing an existing wallet."""
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

@app.route("/database-viewer")
def database_viewer():
    """Renders a viewer for the SQLite database tables."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
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

@app.route("/console_view")
def console_view():
    """Renders a page to view the server console log (for debugging)."""
    log_url = "https://www.pythonanywhere.com/user/BubbaDiego/files/var/log/www.deadlypanda.com.error.log"
    return render_template("console_view.html", log_url=log_url)

@app.route("/api/get_config")
def api_get_config():
    """API endpoint to retrieve the current configuration."""
    try:
        conf = load_config()
        logger.debug("Loaded config: %s", conf)
        return jsonify(conf)
    except Exception as e:
        logger.error("Error loading config: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/save_theme", methods=["POST"])
def save_theme_route():
    """Endpoint to save updated theme settings."""
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

@app.context_processor
def update_theme_context():
    """Injects theme configuration into the template context."""
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

# -----------------------------------------------------------------------------
# Run the Application
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
