#!/usr/bin/env python
"""
dashboard_bp.py
Description:
    Flask blueprint for all dashboard-specific routes and API endpoints.
    This includes:
      - The index route.
      - The main dashboard view.
      - Theme options.
      - API endpoints for chart data (size_composition, value_composition, collateral_composition, size_balance).
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

dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates")


# Helper: Convert ISO timestamp to PST formatted string.
def _convert_iso_to_pst(iso_str):
    if not iso_str or iso_str == "N/A":
        return "N/A"
    try:
        # fromisoformat works for both aware and naive; if naive, we assume it's in local time.
        dt_obj = datetime.fromisoformat(iso_str)
        # Force conversion to PST.
        pst = pytz.timezone("US/Pacific")
        if dt_obj.tzinfo is None:
            # Assume dt_obj is in PST already if naive.
            dt_obj = pst.localize(dt_obj)
        dt_pst = dt_obj.astimezone(pst)
        return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception as e:
        logger.error(f"Error converting timestamp: {e}")
        return "N/A"


# Helper: Compute Size Composition.
def compute_size_composition():
    positions = PositionService.get_all_positions(DB_PATH) or []
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
    positions = PositionService.get_all_positions(DB_PATH) or []
    logger.debug(f"[Value Composition] Retrieved positions: {positions}")
    long_total = 0.0
    short_total = 0.0
    for p in positions:
        try:
            entry_price = float(p.get("entry_price", 0))
            current_price = float(p.get("current_price", 0))
            collateral = float(p.get("collateral", 0))
            size = float(p.get("size", 0))
            if entry_price > 0:
                token_count = size / entry_price
                if p.get("position_type", "").upper() == "LONG":
                    pnl = (current_price - entry_price) * token_count
                else:
                    pnl = (entry_price - current_price) * token_count
            else:
                pnl = 0.0
            value = collateral + pnl
            logger.debug(f"[Value Composition] Position {p.get('id', 'unknown')}: entry_price={entry_price}, current_price={current_price}, size={size}, collateral={collateral}, pnl={pnl}, value={value}")
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
    positions = PositionService.get_all_positions(DB_PATH) or []
    logger.debug(f"[Collateral Composition] Retrieved positions: {positions}")
    long_total = sum(float(p.get("collateral", 0)) for p in positions if p.get("position_type", "").upper() == "LONG")
    short_total = sum(float(p.get("collateral", 0)) for p in positions if p.get("position_type", "").upper() == "SHORT")
    total = long_total + short_total
    logger.debug(f"[Collateral Composition] Totals: long_total={long_total}, short_total={short_total}, overall total: {total}")
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
@dashboard_bp.route("/dashboard")
def dashboard():
    try:
        # Retrieve all positions.
        all_positions = PositionService.get_all_positions(DB_PATH) or []
        positions = all_positions  # Use all positions for positions table.
        liquidation_positions = all_positions  # Use all positions for liquidation bars.
        # Sort positions for top and bottom based on current_travel_percent (defaulting to 0 if missing)
        top_positions = sorted(all_positions, key=lambda pos: float(pos.get("current_travel_percent", 0)), reverse=True)
        bottom_positions = sorted(all_positions, key=lambda pos: float(pos.get("current_travel_percent", 0)))[:3]

        # Compute totals.
        totals = {}
        totals["total_collateral"] = sum(float(pos.get("collateral", 0)) for pos in positions)
        totals["total_value"] = sum(float(pos.get("value", 0)) for pos in positions)
        totals["total_size"] = sum(float(pos.get("size", 0)) for pos in positions)
        if positions:
            totals["avg_leverage"] = sum(float(pos.get("leverage", 0)) for pos in positions) / len(positions)
            totals["avg_travel_percent"] = sum(float(pos.get("current_travel_percent", 0)) for pos in positions) / len(positions)
        else:
            totals["avg_leverage"] = 0
            totals["avg_travel_percent"] = 0

        # Portfolio history and stats.
        dl = DataLocker.get_instance()
        portfolio_history = dl.get_portfolio_history() or []
        portfolio_value_num = portfolio_history[-1].get("total_value", 0) if portfolio_history else 0
        portfolio_change = 0
        if portfolio_history:
            cutoff = datetime.now() - timedelta(hours=24)
            filtered_history = []
            for entry in portfolio_history:
                snapshot = entry.get("snapshot_time")
                if snapshot:
                    try:
                        t = datetime.fromisoformat(snapshot)
                        if t >= cutoff:
                            filtered_history.append(entry)
                    except Exception as e:
                        logger.error(f"Error parsing snapshot_time: {e}")
            if filtered_history:
                first_val = filtered_history[0].get("total_value", 0)
                if first_val:
                    portfolio_change = ((filtered_history[-1].get("total_value", 0) - first_val) / first_val) * 100
            else:
                first_val = portfolio_history[0].get("total_value", 0)
                if first_val:
                    portfolio_change = ((portfolio_history[-1].get("total_value", 0) - first_val) / first_val) * 100

        formatted_portfolio_value = "{:,.2f}".format(portfolio_value_num)
        formatted_portfolio_change = "{:,.1f}".format(portfolio_change)

        # Latest price data.
        btc_data = dl.get_latest_price("BTC") or {}
        eth_data = dl.get_latest_price("ETH") or {}
        sol_data = dl.get_latest_price("SOL") or {}
        sp500_data = dl.get_latest_price("S&P 500") or {}

        btc_price = float(btc_data.get("current_price", 0))
        eth_price = float(eth_data.get("current_price", 0))
        sol_price = float(sol_data.get("current_price", 0))
        sp500_value = float(sp500_data.get("current_price", 0))

        formatted_btc_price = "{:,.2f}".format(btc_price)
        formatted_eth_price = "{:,.2f}".format(eth_price)
        formatted_sol_price = "{:,.2f}".format(sol_price)
        formatted_sp500_value = "{:,.2f}".format(sp500_value)

        # Retrieve last update times for positions.
        update_times = dl.get_last_update_times() or {}
        raw_last_update = update_times.get("last_update_time_positions")
        last_update_positions_source = update_times.get("last_update_positions_source", "N/A")
        if raw_last_update:
            converted_last_update = _convert_iso_to_pst(raw_last_update)
            if converted_last_update != "N/A":
                try:
                    dt_obj = datetime.strptime(converted_last_update, "%m/%d/%Y %I:%M:%S %p %Z")
                    last_update_time_only = dt_obj.strftime("%I:%M %p %Z").lstrip("0")
                    last_update_date_only = f"{dt_obj.month}/{dt_obj.day}/{dt_obj.strftime('%y')}"
                except Exception as ex:
                    logger.error(f"Error parsing converted last update time: {ex}")
                    last_update_time_only = "N/A"
                    last_update_date_only = "N/A"
            else:
                last_update_time_only = "N/A"
                last_update_date_only = "N/A"
        else:
            last_update_time_only = "N/A"
            last_update_date_only = "N/A"

        return render_template(
            "dashboard.html",
            top_positions=top_positions,
            bottom_positions=bottom_positions,
            liquidation_positions=liquidation_positions,
            portfolio_data=portfolio_history,
            portfolio_value=formatted_portfolio_value,
            portfolio_change=formatted_portfolio_change,
            btc_price=formatted_btc_price,
            eth_price=formatted_eth_price,
            sol_price=formatted_sol_price,
            sp500_value=formatted_sp500_value,
            positions=positions,
            totals=totals,
            last_update_time_only=last_update_time_only,
            last_update_date_only=last_update_date_only,
            last_update_positions_source=last_update_positions_source
        )
    except Exception as e:
        logger.error("Error retrieving dashboard data: %s", e, exc_info=True)
        return render_template(
            "dashboard.html",
            top_positions=[],
            bottom_positions=[],
            liquidation_positions=[],
            portfolio_data=[],
            portfolio_value="0.00",
            portfolio_change="0.0",
            btc_price="0.00",
            eth_price="0.00",
            sol_price="0.00",
            sp500_value="0.00",
            positions=[],
            totals={},
            last_update_time_only="N/A",
            last_update_date_only="N/A",
            last_update_positions_source="N/A"
        )


@dashboard_bp.route("/dash_performance")
def dash_performance():
    portfolio_data = DataLocker.get_instance().get_portfolio_history() or []
    return render_template("dash_performance.html", portfolio_data=portfolio_data)


@dashboard_bp.route("/theme")
def theme_options():
    return render_template("theme.html")


# -------------------------------
# API Endpoints for Chart Data (Real Data)
# -------------------------------

@dashboard_bp.route("/api/size_composition")
def api_size_composition():
    try:
        series = compute_size_composition()
        return jsonify({"series": series})
    except Exception as e:
        logger.error(f"Error in api_size_composition: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/api/value_composition")
def api_value_composition():
    try:
        series = compute_value_composition()
        return jsonify({"series": series})
    except Exception as e:
        logger.error(f"Error in api_value_composition: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/api/size_balance")
def api_size_balance():
    try:
        positions = PositionService.get_all_positions(DB_PATH) or []
        groups = {}
        for pos in positions:
            wallet = pos.get("wallet", "ObiVault")
            asset = pos.get("asset_type", "BTC").upper()
            if asset not in ["BTC", "ETH", "SOL"]:
                continue
            if wallet not in ["ObiVault", "R2Vault"]:
                wallet = "ObiVault"
            key = (wallet, asset)
            if key not in groups:
                groups[key] = {"long": 0, "short": 0}
            try:
                size = float(pos.get("size", 0))
            except Exception:
                size = 0
            position_type = pos.get("position_type", "").upper()
            if position_type == "LONG":
                groups[key]["long"] += size
            elif position_type == "SHORT":
                groups[key]["short"] += size
        groups_list = []
        for (wallet, asset), values in groups.items():
            total = values["long"] + values["short"]
            if total > 0:
                groups_list.append({
                    "wallet": wallet,
                    "asset": asset,
                    "long": values["long"],
                    "short": values["short"],
                    "total": total
                })
        return jsonify({"groups": groups_list})
    except Exception as e:
        logger.error(f"Error in api_size_balance: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/api/collateral_composition")
def api_collateral_composition():
    try:
        series = compute_collateral_composition()
        return jsonify({"series": series})
    except Exception as e:
        logger.error(f"Error in api_collateral_composition: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
