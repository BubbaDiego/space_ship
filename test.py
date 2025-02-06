#!/usr/bin/env python
import os
import sqlite3

# Determine the base directory and database path.
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "mother_brain.db")

def create_tables():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create the update_times table to store last update times.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS update_times (
        id INTEGER PRIMARY KEY,
        last_update_time_positions TEXT,
        last_update_time_positions_source TEXT,
        last_update_time_prices TEXT,
        last_update_time_prices_source TEXT,
        last_update_time_jupiter TEXT
    );
    """)

    # Create the positions table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_name TEXT,
        asset_type TEXT,
        position_type TEXT,
        entry_price REAL,
        liquidation_price REAL,
        collateral REAL,
        size REAL,
        leverage REAL,
        value REAL,
        last_updated TEXT,
        pnl_after_fees_usd REAL,
        current_travel_percent REAL,
        profit REAL DEFAULT 0.0
    );
    """)

    # Create the prices table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_type TEXT,
        current_price REAL,
        last_update_time TEXT
    );
    """)

    # Create the alerts table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        alert_type TEXT,
        asset_type TEXT,
        trigger_value REAL,
        condition TEXT,
        status TEXT,
        position_type TEXT,
        wallet_name TEXT,
        frequency INTEGER,
        counter INTEGER,
        liquidation_distance REAL,
        target_travel_percent REAL,
        liquidation_price REAL,
        notes TEXT
    );
    """)

    # Create the brokers table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS brokers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        image_path TEXT,
        web_address TEXT,
        total_holding REAL
    );
    """)

    # Create the wallets table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wallets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        public_address TEXT,
        private_address TEXT,
        image_path TEXT,
        balance REAL
    );
    """)

    # Create the api_counters table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS api_counters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        counter_name TEXT,
        counter_value INTEGER
    );
    """)

    # Create the balance_vars table.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS balance_vars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        total_wallet_balance REAL,
        total_brokerage_balance REAL,
        total_balance REAL
    );
    """)

    conn.commit()
    conn.close()
    print("Tables created successfully in", DB_PATH)

if __name__ == "__main__":
    create_tables()
