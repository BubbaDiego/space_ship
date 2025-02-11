#!/usr/bin/env python
"""
Module: positions_bp.py
Description:
    A production‐ready Flask blueprint for all positions-related endpoints.
    This module handles rendering, API responses, and orchestrates business logic via PositionService.
    It also manages alert configuration, theme updates, and integrates with SocketIO and external services.
"""

import logging
import json
from datetime import datetime
import pytz
from datetime import datetime, timedelta
import asyncio

from flask import (
    Blueprint, request, jsonify, render_template, redirect, url_for, flash, current_app
)
from data.data_locker import DataLocker
from config.config_manager import load_config, update_config
from config.config_constants import DB_PATH, CONFIG_PATH
from utils.calc_services import CalcServices, get_profit_alert_class
from positions.position_service import PositionService


import asyncio  # Ensure asyncio is imported
from alerts.alert_manager import manager as alert_manager

# These helper functions and objects must be defined and imported appropriately.
# For example, update_prices and manual_check_alerts might come from other modules.
from prices.price_monitor import PriceMonitor
from alerts.alert_manager import AlertManager #manual_check_alerts, manager

# Assume that socketio is initialized in your main app and imported here.
#from your_app import socketio  # Replace with the actual import if different

logger = logging.getLogger("PositionsBlueprint")
logger.setLevel(logging.DEBUG)

positions_bp = Blueprint("positions", __name__, template_folder="templates")


def get_socketio():
    return current_app.extensions.get('socketio')

def _convert_iso_to_pst(iso_str):
    """Converts an ISO timestamp string to a formatted PST time string."""
    if not iso_str or iso_str == "N/A":
        return "N/A"
    pst = pytz.timezone("US/Pacific")
    try:
        dt_obj = datetime.fromisoformat(iso_str)
        dt_pst = dt_obj.astimezone(pst)
        return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception as e:
        logger.error(f"Error converting timestamp: {e}")
        return "N/A"



@positions_bp.route("/", methods=["GET"])
def list_positions():
    try:
        positions = PositionService.get_all_positions(DB_PATH)
        dl = DataLocker.get_instance(DB_PATH)
        for pos in positions:
            wallet_name = pos.get("wallet_name")
            pos["wallet_name"] = dl.get_wallet_by_name(wallet_name) if wallet_name else None

        config_data = load_config(CONFIG_PATH)
        alert_dict = config_data.get("alert_ranges", {})

        def get_alert_class_local(value, low, med, high):
            try:
                low = float(low) if low not in (None, "") else float('-inf')
            except:
                low = float('-inf')
            try:
                med = float(med) if med not in (None, "") else float('inf')
            except:
                med = float('inf')
            try:
                high = float(high) if high not in (None, "") else float('inf')
            except:
                high = float('inf')
            if value >= high:
                return "alert-high"
            elif value >= med:
                return "alert-medium"
            elif value >= low:
                return "alert-low"
            else:
                return ""

        liqd_cfg = alert_dict.get("liquidation_distance_ranges", {})
        liqd_low = liqd_cfg.get("low", 0.0)
        liqd_med = liqd_cfg.get("medium", 0.0)
        liqd_high = liqd_cfg.get("high", None)
        for pos in positions:
            liqd = float(pos.get("liquidation_distance") or 0.0)
            pos["liqdist_alert_class"] = get_alert_class_local(liqd, liqd_low, liqd_med, liqd_high)

        hi_cfg = alert_dict.get("heat_index_ranges", {})
        hi_low = hi_cfg.get("low", 0.0)
        hi_med = hi_cfg.get("medium", 0.0)
        hi_high = hi_cfg.get("high", None)
        for pos in positions:
            heat_val = float(pos.get("heat_index") or 0.0)
            pos["heat_alert_class"] = get_alert_class_local(heat_val, hi_low, hi_med, hi_high)

        totals_dict = CalcServices().calculate_totals(positions)
        dl = DataLocker.get_instance(DB_PATH)
        times_dict = dl.get_last_update_times() or {}
        pos_time_iso = times_dict.get("last_update_time_positions", "N/A")
        pos_time_formatted = _convert_iso_to_pst(pos_time_iso)

        return render_template("positions.html",
                               positions=positions,
                               totals=totals_dict,
                               last_update_positions=pos_time_formatted,
                               last_update_positions_source=times_dict.get("last_update_positions_source", "N/A"))
    except Exception as e:
        logger.error(f"Error in listing positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/position_trends", methods=["GET"])
def position_trends():
    """
    Renders historical trends for positions totals using data from the positions_totals_history table.
    The timeframe is determined by the 'hours' query parameter (default 24 hours).
    """
    try:
        # Use "hours" from query parameters instead of "timeframe"
        hours = request.args.get("hours", default=24, type=int)
        threshold = datetime.now() - timedelta(hours=hours)
        logger.debug(f"Querying snapshots from {threshold.isoformat()} onward (last {hours} hours).")
        dl = DataLocker.get_instance(DB_PATH)
        dl._init_sqlite_if_needed()  # Ensure connection is ready.
        cursor = dl.conn.cursor()
        cursor.execute("""
            SELECT *
              FROM positions_totals_history
             WHERE snapshot_time >= ?
             ORDER BY snapshot_time ASC
        """, (threshold.isoformat(),))
        rows = cursor.fetchall()
        cursor.close()
        logger.debug(f"Found {len(rows)} snapshot rows in the selected timeframe.")

        # Build chart_data with consistent key names for the front-end.
        chart_data = {
            "collateral": [],
            "value": [],
            "size": [],
            "avg_leverage": [],
            "avg_travel_percent": [],
            "avg_heat": []
        }

        if not rows:
            # Fallback: if no snapshot rows exist, use current totals to produce a data point.
            calc_services = CalcServices()
            positions = dl.get_positions()
            totals = calc_services.calculate_totals(positions)
            current_timestamp = int(datetime.now().timestamp() * 1000)
            chart_data = {
                "collateral": [[current_timestamp, totals.get("total_collateral", 0)]],
                "value": [[current_timestamp, totals.get("total_value", 0)]],
                "size": [[current_timestamp, totals.get("total_size", 0)]],
                "avg_leverage": [[current_timestamp, totals.get("avg_leverage", 0)]],
                "avg_travel_percent": [[current_timestamp, totals.get("avg_travel_percent", 0)]],
                "avg_heat": [[current_timestamp, totals.get("avg_heat_index", 0)]]
            }
            logger.debug("No snapshots found; using fallback current totals.")
        else:
            for row in rows:
                # Since rows are sqlite3.Row objects, use bracket notation.
                snapshot_time = datetime.fromisoformat(row["snapshot_time"])
                epoch_ms = int(snapshot_time.timestamp() * 1000)
                chart_data["collateral"].append([epoch_ms, float(row["total_collateral"] or 0)])
                chart_data["value"].append([epoch_ms, float(row["total_value"] or 0)])
                chart_data["size"].append([epoch_ms, float(row["total_size"] or 0)])
                chart_data["avg_leverage"].append([epoch_ms, float(row["avg_leverage"] or 0)])
                chart_data["avg_travel_percent"].append([epoch_ms, float(row["avg_travel_percent"] or 0)])
                chart_data["avg_heat"].append([epoch_ms, float(row["avg_heat_index"] or 0)])

        return render_template("position_trends.html", chart_data=chart_data, timeframe=hours)
    except Exception as e:
        logger.error("Error in position_trends: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500@positions_bp.route("/position_trends", methods=["GET"])
def position_trends():
    """
    Renders historical trends for positions totals using data from the positions_totals_history table.
    The timeframe is determined by the 'hours' query parameter (default 24 hours).
    """
    try:
        # Use "hours" from query parameters instead of "timeframe"
        hours = request.args.get("hours", default=24, type=int)
        threshold = datetime.now() - timedelta(hours=hours)
        logger.debug(f"Querying snapshots from {threshold.isoformat()} onward (last {hours} hours).")
        dl = DataLocker.get_instance(DB_PATH)
        dl._init_sqlite_if_needed()  # Ensure connection is ready.
        cursor = dl.conn.cursor()
        cursor.execute("""
            SELECT *
              FROM positions_totals_history
             WHERE snapshot_time >= ?
             ORDER BY snapshot_time ASC
        """, (threshold.isoformat(),))
        rows = cursor.fetchall()
        cursor.close()
        logger.debug(f"Found {len(rows)} snapshot rows in the selected timeframe.")

        # Build chart_data with consistent key names for the front-end.
        chart_data = {
            "collateral": [],
            "value": [],
            "size": [],
            "avg_leverage": [],
            "avg_travel_percent": [],
            "avg_heat": []
        }

        if not rows:
            # Fallback: if no snapshot rows exist, use current totals to produce a data point.
            calc_services = CalcServices()
            positions = dl.get_positions()
            totals = calc_services.calculate_totals(positions)
            current_timestamp = int(datetime.now().timestamp() * 1000)
            chart_data = {
                "collateral": [[current_timestamp, totals.get("total_collateral", 0)]],
                "value": [[current_timestamp, totals.get("total_value", 0)]],
                "size": [[current_timestamp, totals.get("total_size", 0)]],
                "avg_leverage": [[current_timestamp, totals.get("avg_leverage", 0)]],
                "avg_travel_percent": [[current_timestamp, totals.get("avg_travel_percent", 0)]],
                "avg_heat": [[current_timestamp, totals.get("avg_heat_index", 0)]]
            }
            logger.debug("No snapshots found; using fallback current totals.")
        else:
            for row in rows:
                # Since rows are sqlite3.Row objects, use bracket notation.
                snapshot_time = datetime.fromisoformat(row["snapshot_time"])
                epoch_ms = int(snapshot_time.timestamp() * 1000)
                chart_data["collateral"].append([epoch_ms, float(row["total_collateral"] or 0)])
                chart_data["value"].append([epoch_ms, float(row["total_value"] or 0)])
                chart_data["size"].append([epoch_ms, float(row["total_size"] or 0)])
                chart_data["avg_leverage"].append([epoch_ms, float(row["avg_leverage"] or 0)])
                chart_data["avg_travel_percent"].append([epoch_ms, float(row["avg_travel_percent"] or 0)])
                chart_data["avg_heat"].append([epoch_ms, float(row["avg_heat_index"] or 0)])

        return render_template("position_trends.html", chart_data=chart_data, timeframe=hours)
    except Exception as e:
        logger.error("Error in position_trends: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/table", methods=["GET"])
def positions_table():
    try:
        dl = DataLocker.get_instance(DB_PATH)
        positions = PositionService.get_all_positions(DB_PATH)
        totals = CalcServices().calculate_totals(positions)
        return render_template("positions_table.html", positions=positions, totals=totals)
    except Exception as e:
        logger.error(f"Error in positions_table: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/edit/<position_id>", methods=["POST"])
def edit_position(position_id):
    try:
        dl = DataLocker.get_instance(DB_PATH)
        size = float(request.form.get("size", 0.0))
        collateral = float(request.form.get("collateral", 0.0))
        dl.update_position(position_id, size, collateral)
        return redirect(url_for("positions.list_positions"))
    except Exception as e:
        logger.error(f"Error updating position {position_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/delete/<position_id>", methods=["POST"])
def delete_position(position_id):
    try:
        dl = DataLocker.get_instance(DB_PATH)
        dl.delete_position(position_id)
        return redirect(url_for("positions.list_positions"))
    except Exception as e:
        logger.error(f"Error deleting position {position_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/delete-all", methods=["POST"])
def delete_all_positions():
    try:
        dl = DataLocker.get_instance(DB_PATH)
        dl.delete_all_positions()
        return redirect(url_for("positions.list_positions"))
    except Exception as e:
        logger.error(f"Error deleting all positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/upload", methods=["POST"])
def upload_positions():
    try:
        dl = DataLocker.get_instance(DB_PATH)
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
            dl.create_position(pos_dict)
        return jsonify({"message": "Positions uploaded successfully"}), 200
    except Exception as e:
        logger.error(f"Error uploading positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/api/data", methods=["GET"])
def positions_data_api():
    try:
        dl = DataLocker.get_instance(DB_PATH)
        mini_prices = []
        for asset in ["BTC", "ETH", "SOL"]:
            row = dl.get_latest_price(asset)
            if row:
                mini_prices.append({
                    "asset_type": row["asset_type"],
                    "current_price": float(row["current_price"])
                })
        positions = PositionService.get_all_positions(DB_PATH)
        for pos in positions:
            wallet_name = pos.get("wallet_name")
            pos["wallet_name"] = dl.get_wallet_by_name(wallet_name) if wallet_name else None
        totals_dict = CalcServices().calculate_totals(positions)
        return jsonify({
            "mini_prices": mini_prices,
            "positions": positions,
            "totals": totals_dict
        })
    except Exception as e:
        logger.error(f"Error in positions_data_api: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/mobile", methods=["GET"])
def positions_mobile():
    try:
        dl = DataLocker.get_instance(DB_PATH)
        calc = CalcServices()
        positions = PositionService.get_all_positions(DB_PATH)
        for pos in positions:
            wallet_name = pos.get("wallet_name")
            pos["wallet_name"] = dl.get_wallet_by_name(wallet_name) if wallet_name else None
        config_data = load_config(CONFIG_PATH)
        alert_dict = config_data.get("alert_ranges", {})

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
        for pos in positions:
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
        times_dict = DataLocker.get_instance(DB_PATH).get_last_update_times() or {}
        pos_time_iso = times_dict.get("last_update_time_positions", "N/A")
        pos_time_formatted = _convert_iso_to_pst(pos_time_iso)
        return render_template("positions_mobile.html",
                               positions=positions,
                               totals=CalcServices().calculate_totals(positions),
                               last_update_positions=pos_time_formatted,
                               last_update_positions_source=times_dict.get("last_update_positions_source", "N/A"))
    except Exception as e:
        logger.error(f"Error in /positions_mobile: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/heat", methods=["GET"])
def heat_report():
    """
    Prepares a heat report by updating positions with the latest prices,
    formatting them for display, grouping by asset and position type,
    and calculating aggregated totals.
    """
    try:
        dl = DataLocker.get_instance(DB_PATH)
        # Get raw positions
        positions_data = dl.read_positions()
        logger.debug(f"Raw positions count: {len(positions_data)}")

        # Update positions with latest prices and prepare them for display
        positions_data = PositionService.fill_positions_with_latest_price(positions_data)
        positions_data = CalcServices().prepare_positions_for_display(positions_data)

        # Ensure a defined travel percent and normalize asset keys
        for pos in positions_data:
            if 'travel_percent' not in pos or pos['travel_percent'] is None:
                pos['travel_percent'] = pos.get('current_travel_percent', 0)
            pos['asset'] = pos.get('asset_type', 'Unknown').upper()

        totals = CalcServices().calculate_totals(positions_data)

        # Group positions by asset and position type (short/long)
        grouped = {}
        for pos in positions_data:
            asset = pos.get("asset", "UNKNOWN")
            pos_type = pos.get("position_type", "").lower()
            if asset not in grouped:
                grouped[asset] = {"short": [], "long": []}
            if pos_type in ["short", "long"]:
                grouped[asset][pos_type].append(pos)

        heat_data = {"totals": totals}
        for asset, groups in grouped.items():
            heat_data[asset] = {
                "short": groups["short"][0] if groups["short"] else {},
                "long": groups["long"][0] if groups["long"] else {}
            }
        # Ensure default keys for BTC, ETH, SOL
        for asset in ["BTC", "ETH", "SOL"]:
            if asset not in heat_data:
                heat_data[asset] = {"short": {}, "long": {}}

        logger.debug(f"Aggregated heat_data: {heat_data}")

        times_dict = dl.get_last_update_times() or {}
        pos_time_iso = times_dict.get("last_update_time_positions", "N/A")
        pos_time_formatted = _convert_iso_to_pst(pos_time_iso)

        return render_template("hedge_report.html",
                               hd=heat_data,
                               last_update_positions=pos_time_formatted,
                               last_update_positions_source=times_dict.get("last_update_positions_source", "N/A"))
    except Exception as e:
        logger.error(f"Error in /heat route: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/delete-alert/<alert_id>", methods=["POST"])
def delete_alert(alert_id):
    try:
        dl = DataLocker.get_instance(DB_PATH)
        dl.delete_alert(alert_id)
        flash("Alert deleted!", "success")
        # Assuming an alerts blueprint or endpoint exists for redirection
        return redirect(url_for("alerts.list_alerts"))
    except Exception as e:
        logger.error(f"Error deleting alert {alert_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# Helper function to retrieve the SocketIO instance from the current app
def get_socketio():
    return current_app.extensions.get('socketio')


@positions_bp.route("/update_jupiter", methods=["GET", "POST"])
def update_jupiter():
    """
    Updates Jupiter positions by:
      1. Deleting existing Jupiter positions.
      2. Fetching new positions via the external Jupiter API.
      3. Optionally updating prices and manually checking alerts.
      4. Recording a snapshot of the positions.
      5. Updating last update timestamps and emitting a SocketIO event.
    """
    def update_prices_wrapper():
        """Helper function that wraps the PriceMonitor update_prices call."""
        try:
            from prices.price_monitor import PriceMonitor  # Updated import path
            pm = PriceMonitor(db_path=DB_PATH, config_path=CONFIG_PATH)
            asyncio.run(pm.update_prices())
            class DummyResponse:
                status_code = 200
                def get_json(self):
                    return {"message": "Prices updated successfully"}
            return DummyResponse()
        except Exception as ex:
            err_msg = str(ex)
            logger.error(f"Error in update_prices_wrapper: {err_msg}", exc_info=True)
            class DummyErrorResponse:
                status_code = 500
                def get_json(self):
                    return {"error": err_msg}
            return DummyErrorResponse()

    try:
        logger.debug(">>> START update_jupiter route.")

        # Get the source parameter (default to "API")
        source = request.args.get("source") or request.form.get("source") or "API"
        logger.debug(f">>> Source parameter: {source}")

        # Step 1: Delete existing Jupiter positions.
        logger.debug(">>> Deleting existing Jupiter positions...")
        PositionService.delete_all_jupiter_positions(DB_PATH)
        logger.debug(">>> Deletion of Jupiter positions completed.")

        # Step 2: Update Jupiter positions.
        logger.debug(">>> Updating Jupiter positions via PositionService...")
        update_result = PositionService.update_jupiter_positions(DB_PATH)
        logger.debug(f">>> Update result: {update_result}")
        if "error" in update_result:
            logger.error(">>> Error during Jupiter positions update: " + str(update_result))
            return jsonify(update_result), 500

        # Step 3: Update prices and check alerts.
        logger.debug(">>> Triggering price update...")
        prices_resp = update_prices_wrapper()  # Use the helper function.
        logger.debug(f">>> Prices update response: {prices_resp.status_code} - {prices_resp.get_json()}")
        if prices_resp.status_code != 200:
            logger.error(f">>> Price update failed with status code: {prices_resp.status_code}")
            return prices_resp

        logger.debug(">>> Performing manual alert check via alert_manager.check_alerts()...")
        alert_manager.check_alerts()
        logger.debug(">>> Manual alert check completed.")

        # Step 4: Update last update timestamps.
        now = datetime.now()
        logger.debug(f">>> Current timestamp: {now.isoformat()}")
        dl = DataLocker.get_instance(DB_PATH)
        dl.set_last_update_times(
            positions_dt=now,
            positions_source=source,
            prices_dt=now,
            prices_source=source
        )
        logger.debug(">>> Last update timestamps set successfully.")

        # Step 5: Record positions snapshot.
        try:
            logger.debug(">>> Recording positions snapshot...")
            try:
                PositionService.record_positions_snapshot(DB_PATH)
                logger.debug(">>> Positions snapshot recorded successfully.")
            except AttributeError as attr_err:
                logger.warning(f">>> Snapshot method not found: {attr_err}. Skipping snapshot recording.")
            except Exception as snap_err:
                logger.error(f">>> Error recording positions snapshot: {snap_err}", exc_info=True)
        except Exception as snap_err:
            logger.error(f">>> Error in snapshot recording block: {snap_err}", exc_info=True)

        # Step 6: Emit SocketIO event.
        logger.debug(">>> Emitting SocketIO event for data update...")
        socketio_inst = get_socketio()
        if socketio_inst:
            socketio_inst.emit('data_updated', {
                'message': f"Jupiter positions + Prices updated successfully by {source}!",
                'last_update_time_positions': now.isoformat(),
                'last_update_time_prices': now.isoformat()
            })
            logger.debug(">>> SocketIO event emitted successfully.")
        else:
            logger.warning("SocketIO instance not found; skipping event emission.")

        # *** New: Print updated totals with PST timestamp to console ***
        updated_totals = dl.get_balance_vars()
        pst_timestamp = _convert_iso_to_pst(now.isoformat())
        print(f"Jupiter Update Complete: Totals = {updated_totals} at {pst_timestamp}")
        logger.debug(f">>> Jupiter Update Complete: Totals = {updated_totals} at {pst_timestamp}")

        response_data = {
            "message": f"Jupiter positions + Prices updated successfully by {source}!",
            "source": source,
            "last_update_time_positions": now.isoformat(),
            "last_update_time_prices": now.isoformat()
        }
        logger.debug(">>> update_jupiter route completed successfully.")
        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f">>> ERROR in update_jupiter: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.route("/update_alert_config", methods=["POST"])
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


@positions_bp.route("/save_theme", methods=["POST"])
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


@positions_bp.route("/top_positions", methods=["GET"])
@positions_bp.route("/top_positions", methods=["GET"])
def show_top_positions():
    try:
        all_positions = PositionService.get_all_positions(DB_PATH)
        # Filter positions with a valid current_travel_percent
        valid_positions = [pos for pos in all_positions if pos.get("current_travel_percent") is not None]

        # Sort positions for top and bottom lists
        top_positions = sorted(valid_positions, key=lambda pos: pos["current_travel_percent"], reverse=True)[:3]
        bottom_positions = sorted(valid_positions, key=lambda pos: pos["current_travel_percent"])[:3]

        # Print alert state for each top position
        print("DEBUG: Alert States for Top Positions:")
        for pos in top_positions:
            print("Top Position - ID: {}, Alert State: {}".format(pos.get("id", "unknown"),
                                                                  pos.get("alert_state", "N/A")))

        # Print alert state for each bottom position
        print("DEBUG: Alert States for Bottom Positions:")
        for pos in bottom_positions:
            print("Bottom Position - ID: {}, Alert State: {}".format(pos.get("id", "unknown"),
                                                                     pos.get("alert_state", "N/A")))

        return render_template("top_positions.html", top_positions=top_positions, bottom_positions=bottom_positions)
    except Exception as e:
        print("Error retrieving top positions: {}".format(e))
        return render_template("top_positions.html", top_positions=[], bottom_positions=[])


@positions_bp.route("/top_bottom", methods=["GET"])
def top_bottom_positions():
    try:
        # Retrieve and enrich all positions
        positions = PositionService.get_all_positions(DB_PATH)

        # Filter out any positions without a travel percent value
        valid_positions = [p for p in positions if p.get("travel_percent") is not None]

        # Sort positions for top 3 (most positive travel_percent) and bottom 3 (most negative)
        top_positions = sorted(valid_positions, key=lambda p: p["travel_percent"], reverse=True)[:3]
        bottom_positions = sorted(valid_positions, key=lambda p: p["travel_percent"])[:3]

        # Render a template that displays these two groups
        return render_template(
            "top_bottom_positions.html",
            top_positions=top_positions,
            bottom_positions=bottom_positions
        )
    except Exception as e:
        logger.error(f"Error retrieving top and bottom positions: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@positions_bp.context_processor
def update_theme_context():
    config_path = current_app.config.get("CONFIG_PATH", CONFIG_PATH)
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading theme config: {e}", exc_info=True)
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
