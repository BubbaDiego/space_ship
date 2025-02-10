#!/usr/bin/env python
"""
Script: inject_sine_wave_data.py
Description:
  Injects 24 hours of sine wave snapshot data into the positions_totals_history table.
  Data is inserted at 5-minute intervals (288 snapshots). Each snapshot is calculated
  using a sine wave function so that the metrics vary continuously over the period.

  Metrics:
    - total_size: base 1000, amplitude 100
    - total_value: base 5000, amplitude 200
    - total_collateral: base 3000, amplitude 150
    - avg_leverage: base 2, amplitude 0.5
    - avg_travel_percent: base 50, amplitude 20
    - avg_heat_index: base 40, amplitude 10
"""

import sqlite3
import uuid
import math
from datetime import datetime, timedelta
from config.config_constants import DB_PATH  # Ensure DB_PATH is set correctly


def create_snapshot_table(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS positions_totals_history (
            id TEXT PRIMARY KEY,
            snapshot_time DATETIME,
            total_size REAL,
            total_value REAL,
            total_collateral REAL,
            avg_leverage REAL,
            avg_travel_percent REAL,
            avg_heat_index REAL
        )
    """)
    conn.commit()


def clear_snapshot_table(conn):
    cursor = conn.cursor()
    # Get count before deletion
    cursor.execute("SELECT COUNT(*) FROM positions_totals_history")
    before = cursor.fetchone()[0]
    print(f"Snapshots before deletion: {before}")

    cursor.execute("DELETE FROM positions_totals_history")
    conn.commit()

    # Verify deletion by checking the count again.
    cursor.execute("SELECT COUNT(*) FROM positions_totals_history")
    after = cursor.fetchone()[0]
    print(f"Snapshots after deletion: {after}")


def inject_sine_wave_data(db_path: str, start_time: datetime, interval_minutes: int, total_intervals: int):
    # Connect to the database and ensure the snapshot table exists.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    create_snapshot_table(conn)
    clear_snapshot_table(conn)
    cursor = conn.cursor()

    # Base values and amplitudes for the metrics.
    base_total_size = 1000.0
    amplitude_total_size = 100.0

    base_total_value = 5000.0
    amplitude_total_value = 200.0

    base_total_collateral = 3000.0
    amplitude_total_collateral = 150.0

    base_avg_leverage = 2.0
    amplitude_avg_leverage = 0.5

    base_avg_travel_percent = 50.0
    amplitude_avg_travel_percent = 20.0

    base_avg_heat_index = 40.0
    amplitude_avg_heat_index = 10.0

    # We'll produce one complete sine wave cycle over the total_intervals.
    for i in range(total_intervals):
        # Normalized time (t) goes from 0 to 1 over all intervals.
        t = i / total_intervals  # t in [0, 1)
        sine_val = math.sin(2 * math.pi * t)  # one full sine wave cycle

        snapshot_id = str(uuid.uuid4())
        snapshot_time = (start_time + timedelta(minutes=i * interval_minutes)).isoformat()

        # Calculate each metric as base + amplitude * sine_val.
        total_size = base_total_size + amplitude_total_size * sine_val
        total_value = base_total_value + amplitude_total_value * sine_val
        total_collateral = base_total_collateral + amplitude_total_collateral * sine_val
        avg_leverage = base_avg_leverage + amplitude_avg_leverage * sine_val
        avg_travel_percent = base_avg_travel_percent + amplitude_avg_travel_percent * sine_val
        avg_heat_index = base_avg_heat_index + amplitude_avg_heat_index * sine_val

        cursor.execute("""
            INSERT INTO positions_totals_history (
                id, snapshot_time, total_size, total_value, total_collateral,
                avg_leverage, avg_travel_percent, avg_heat_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_id,
            snapshot_time,
            total_size,
            total_value,
            total_collateral,
            avg_leverage,
            avg_travel_percent,
            avg_heat_index
        ))

        print(f"Inserted snapshot {i + 1}/{total_intervals} at {snapshot_time} with sine value {sine_val:.2f}")

    conn.commit()
    conn.close()
    print("Sine wave snapshot data injection complete.")


if __name__ == "__main__":
    # 24 hours of data at 5-minute intervals: 24 * 60 / 5 = 288 snapshots.
    total_hours = 24
    interval_minutes = 5
    total_intervals = (total_hours * 60) // interval_minutes

    # Set start_time so that the snapshots span the last 24 hours.
    start_time = datetime.now() - timedelta(hours=total_hours)

    inject_sine_wave_data(DB_PATH, start_time, interval_minutes, total_intervals)
