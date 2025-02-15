#!/usr/bin/env python
import sqlite3
from config.config_constants import DB_PATH

def insert_test_wallet():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Replace these values as needed.
    wallet_data = (
        "TestWalletd",             # name
        "test_public_addressd",      # public_address
        "test_private_addressd",     # private_address
        "images/wallpaper.png",       # image_path
        100.0                       # balance
    )
    cursor.execute("""
        INSERT OR REPLACE INTO wallets (name, public_address, private_address, image_path, balance)
        VALUES (?, ?, ?, ?, ?)
    """, wallet_data)
    conn.commit()
    cursor.close()
    conn.close()
    print("Test wallet inserted.")

if __name__ == "__main__":
    insert_test_wallet()
