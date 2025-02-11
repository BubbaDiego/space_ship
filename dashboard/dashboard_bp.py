#!/usr/bin/env python
"""
dashboard_bp.py
Description:
    Flask blueprint for all dashboard-specific routes and API endpoints.
    This includes:
      - The index route.
      - The main dashboard view.
      - Theme options.
      - API endpoints for chart data (size_composition, value_composition, collateral_composition).
Usage:
    Import and register this blueprint in your main application.
"""

import json
import logging
import sqlite3
import pytz
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, current_app
from config.config_constants import DB_PATH, CONFIG_PATH
from config.config_manager import load_config
from data.data_locker import DataLocker
from positions.position_service import PositionService
from utils.calc_services import CalcServices

logger = logging.getLogger("DashboardBlueprint")
logger.setLevel(logging.DEBUG)

# Create the blueprint object.
dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates")

# Helper: Convert ISO timestamp to PST formatted string.
def _convert_iso_to_pst(iso_str):
    if not iso_str or iso_str == "N/A":
        return "N/A"
    try:
        pst = pytz.timezone("US/Pacific")
        dt_obj = datetime.fromisoformat(iso_str)
        dt_pst = dt_obj.astimezone(pst)
        return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception as e:
        logger.error(f"Error converting timestamp: {e}")
        return "N/A"

# Helper: Compute Size Composition.
def compute_size_composition():
    """
    Computes the composition of positions by size.
    Returns percentages for LONG vs. SHORT sizes.
    """
    positions = PositionService.get_all_positions(DB_PATH)
    logger.debug(f"[Size Composition] Retrieved positions: {positions}")
    long_total = sum(float(p.get("size", 0)) for p in positions if p.get("position_type", "").upper() == "LONG")
    short_total = sum(float(p.get("size", 0)) for p in positions if p.get("position_type", "").upper() == "SHORT")
    total = long_total + short_total
    logger.debug(f"[Size Composition] Long total: {long_total}, Short total: {short_total}, Overall total: {total}")
    if total > 0:
        series = [round(long_total / total * 100), round(short_total / total * 100)]
    else:
        series = [0, 0]
    logger.debug(f"[Size Composition] Computed series: {series}")
    return series

# Helper: Compute Value Composition.
def compute_value_composition():
    """
    Computes the composition by value.
    For each position, calculates the value as:
      value = collateral + pnl,
    where pnl is computed as:
      pnl = (current_price - entry_price) * (size / entry_price) for LONG,
            (entry_price - current_price) * (size / entry_price) for SHORT.
    Returns percentages for LONG vs. SHORT values.
    """
    positions = PositionService.get_all_positions(DB_PATH)
    logger.debug(f"[Value Composition] Retrieved positions: {positions}")
    long_total = 0.0
    short_total = 0.0
    for p in positions:
        try:
            entry_price = float(p.get("entry_price", 0))
            current_price = float(p.get("current_price", 0))
            collateral = float(p.get("collateral", 0))
            size = float(p.get("size", 0))
            token_count = 0
            if entry_price > 0:
                token_count = size / entry_price
                if p.get("position_type", "").upper() == "LONG":
                    pnl = (current_price - entry_price) * token_count
                else:
                    pnl = (entry_price - current_price) * token_count
            else:
                pnl = 0.0
            value = collateral + pnl
            logger.debug(f"[Value Composition] Position {p.get('id', 'unknown')}: entry_price={entry_price}, current_price={current_price}, size={size}, collateral={collateral}, token_count={token_count if entry_price > 0 else 'N/A'}, pnl={pnl}, value={value}")
        except Exception as calc_err:
            logger.error(f"Error calculating value for position {p.get('id', 'unknown')}: {calc_err}", exc_info=True)
            value = 0.0
        if p.get("position_type", "").upper() == "LONG":
            long_total += value
        elif p.get("position_type", "").upper() == "SHORT":
            short_total += value
    total = long_total + short_total
    logger.debug(f"[Value Composition] Totals: long_total={long_total}, short_total={short_total}, overall total={total}")
    if total > 0:
        series = [round(long_total / total * 100), round(short_total / total * 100)]
    else:
        series = [0, 0]
    logger.debug(f"[Value Composition] Computed series: {series}")
    return series

# Helper: Compute Collateral Composition.
def compute_collateral_composition():
    """
    Computes the composition by collateral.
    Returns percentages for LONG vs. SHORT collateral.
    """
    positions = PositionService.get_all_positions(DB_PATH)
    logger.debug(f"[Collateral Composition] Retrieved positions: {positions}")
    long_total = sum(float(p.get("collateral", 0)) for p in positions if p.get("position_type", "").upper() == "LONG")
    short_total = sum(float(p.get("collateral", 0)) for p in positions if p.get("position_type", "").upper() == "SHORT")
    total = long_total + short_total
    logger.debug(f"[Collateral Composition] Totals: long_total={long_total}, short_total={short_total}, overall total={total}")
    if total > 0:
        series = [round(long_total / total * 100), round(short_total / total * 100)]
    else:
        series = [0, 0]
    logger.debug(f"[Collateral Composition] Computed series: {series}")
    return series

# -------------------------------
# Dashboard Routes
# -------------------------------

@dashboard_bp.route("/dashboard")
def dashboard():
    try:
        all_positions = PositionService.get_all_positions(DB_PATH)
        valid_positions = [pos for pos in all_positions if pos.get("current_travel_percent") is not None]
        top_positions = sorted(valid_positions, key=lambda pos: pos["current_travel_percent"], reverse=True)[:3]
        bottom_positions = sorted(valid_positions, key=lambda pos: pos["current_travel_percent"])[:3]
        liquidation_positions = valid_positions  # Use all valid positions for the liquidation bar
        print("Dashboard: Found {} valid positions.".format(len(valid_positions)))
        for pos in top_positions:
            print("Top Position - ID: {}, current_travel_percent: {}, alert_state: {}".format(
                pos.get("id", "unknown"), pos.get("current_travel_percent"), pos.get("alert_state", "N/A")
            ))
        for pos in bottom_positions:
            print("Bottom Position - ID: {}, current_travel_percent: {}, alert_state: {}".format(
                pos.get("id", "unknown"), pos.get("current_travel_percent"), pos.get("alert_state", "N/A")
            ))
        return render_template("dashboard.html",
                               top_positions=top_positions,
                               bottom_positions=bottom_positions,
                               liquidation_positions=liquidation_positions)
    except Exception as e:
        print("Error retrieving dashboard data:", e)
        return render_template("dashboard.html", top_positions=[], bottom_positions=[], liquidation_positions=[])

@dashboard_bp.route("/theme")
def theme_options():
    return render_template("theme.html")

# -------------------------------
# API Endpoints for Chart Data (Real Data)
# -------------------------------

@dashboard_bp.route("/api/size_composition")
def api_size_composition():
    """
    Computes the composition of positions by size.
    Returns percentages for LONG vs. SHORT sizes.
    """
    try:
        series = compute_size_composition()
        return jsonify({"series": series})
    except Exception as e:
        logger.error(f"Error in api_size_composition: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@dashboard_bp.route("/api/value_composition")
def api_value_composition():
    """
    Computes the composition by value.
    Calculates value = collateral + pnl, where pnl is:
      (current_price - entry_price) * (size / entry_price) for LONG,
      (entry_price - current_price) * (size / entry_price) for SHORT.
    Returns percentages for LONG vs. SHORT values.
    """
    try:
        series = compute_value_composition()
        return jsonify({"series": series})
    except Exception as e:
        logger.error(f"Error in api_value_composition: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@dashboard_bp.route("/api/collateral_composition")
def api_collateral_composition():
    """
    Computes the composition by collateral.
    Returns percentages for LONG vs. SHORT collateral.
    """
    try:
        series = compute_collateral_composition()
        return jsonify({"series": series})
    except Exception as e:
        logger.error(f"Error in api_collateral_composition: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
