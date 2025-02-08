#!/usr/bin/env python
import sqlite3
import os
from config_constants import DB_PATH

def create_wallets_table(db_path: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS wallets (
        name TEXT PRIMARY KEY,
        public_address TEXT,
        private_address TEXT,
        image_path TEXT,
        balance REAL DEFAULT 0.0
    );
    """
    cursor.execute(create_table_sql)
    conn.commit()
    conn.close()
    print("Table 'wallets' created successfully in DB:", db_path)

if __name__ == '__main__':
    create_wallets_table(DB_PATH)
