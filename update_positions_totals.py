#!/usr/bin/env python
"""
Module: update_positions_totals.py
Description: This module periodically reads the current positions, computes aggregated totals
(including an average liquidation distance), and stores a snapshot in the database table
"positions_totals_history" for trending purposes.
"""

import os
import sqlite3
import json
import logging
from datetime import datetime
from uuid import uuid4
from typing import Dict, Any, List

from config_constants import DB_PATH, CONFIG_PATH
from data_locker import DataLocker
from position_service import PositionService
from calc_services import CalcServices

logger = logging.getLogger("PositionsTotalsUpdater")
logger.setLevel(logging.DEBUG)

# Table name for storing historical totals
POSITIONS_TOTALS_HISTORY_TABLE = "positions_totals_history"


def initialize_totals_history_table(db_path: str = DB_PATH) -> None:
    """
    Create the positions_totals_history table if it does not exist.
    The table stores a snapshot of the following fields:
      - total_collateral
      - total_value
      - total_size
      - avg_leverage
      - avg_travel_percent
      - avg_heat_index
      - avg_liquidation_distance
    along with a timestamp.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {POSITIONS_TOTALS_HISTORY_TABLE} (
            id TEXT PRIMARY KEY,
            snapshot_time DATETIME,
            total_collateral REAL,
            total_value REAL,
            total_size REAL,
            avg_leverage REAL,
            avg_travel_percent REAL,
            avg_heat_index REAL,
            avg_liquidation_distance REAL
        )
    """)
    conn.commit()
    conn.close()
    logger.debug("Initialized table: %s", POSITIONS_TOTALS_HISTORY_TABLE)


def calculate_totals_with_liquidation(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculate aggregated totals from a list of positions.
    Reuses CalcServices.calculate_totals for most metrics and additionally
    computes the average liquidation distance over all positions that have a value.
    """
    calc = CalcServices()
    totals = calc.calculate_totals(positions)
    total_liquidation = 0.0
    count_liquidation = 0

    for pos in positions:
        liqd = pos.get("liquidation_distance")
        if liqd is not None:
            try:
                total_liquidation += float(liqd)
                count_liquidation += 1
            except Exception:
                continue

    avg_liquidation = total_liquidation / count_liquidation if count_liquidation > 0 else 0.0
    totals["avg_liquidation_distance"] = avg_liquidation
    return totals


def update_positions_totals(db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    Reads all current positions from the database, computes aggregated totals (including
    average liquidation distance), and inserts a snapshot row into the positions_totals_history table.
    
    Returns:
        A dictionary representing the snapshot that was inserted.
    """
    # Ensure the history table exists
    initialize_totals_history_table(db_path)
    
    # Get the current positions (the PositionService already enriches positions)
    positions = PositionService.get_all_positions(db_path)
    if not positions:
        logger.warning("No positions found to snapshot.")
    
    # Calculate totals (including our new average liquidation distance)
    totals = calculate_totals_with_liquidation(positions)
    
    # Build the snapshot row data
    snapshot = {
        "id": str(uuid4()),
        "snapshot_time": datetime.now().isoformat(),
        "total_collateral": totals.get("total_collateral", 0.0),
        "total_value": totals.get("total_value", 0.0),
        "total_size": totals.get("total_size", 0.0),
        "avg_leverage": totals.get("avg_leverage", 0.0),
        "avg_travel_percent": totals.get("avg_travel_percent", 0.0),
        "avg_heat_index": totals.get("avg_heat_index", 0.0),
        "avg_liquidation_distance": totals.get("avg_liquidation_distance", 0.0)
    }
    
    # Insert the snapshot into the history table
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"""
        INSERT INTO {POSITIONS_TOTALS_HISTORY_TABLE} (
            id,
            snapshot_time,
            total_collateral,
            total_value,
            total_size,
            avg_leverage,
            avg_travel_percent,
            avg_heat_index,
            avg_liquidation_distance
        )
        VALUES (
            :id,
            :snapshot_time,
            :total_collateral,
            :total_value,
            :total_size,
            :avg_leverage,
            :avg_travel_percent,
            :avg_heat_index,
            :avg_liquidation_distance
        )
    """, snapshot)
    conn.commit()
    conn.close()
    logger.info("Inserted positions totals snapshot at %s", snapshot["snapshot_time"])
    return snapshot


if __name__ == "__main__":
    # For testing purposes, run the update and print the snapshot.
    snapshot = update_positions_totals()
    print("Positions Totals Snapshot:")
    print(json.dumps(snapshot, indent=2))
