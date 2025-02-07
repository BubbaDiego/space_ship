#!/usr/bin/env python3
import sqlite3
import os
import sys

# Define the path to your database.
DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "mother_brain.db")

def create_update_times_table(db_path):
    print("========================================")
    print("Starting update_times table fixer script")
    print("========================================")
    print(f"Connecting to database at: {db_path}")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        print("Successfully connected to the database.")
    except Exception as e:
        print(f"Failed to connect to database: {e}")
        sys.exit(1)
    
    try:
        # Check if the table "update_times" exists.
        print("Checking for the 'update_times' table...")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='update_times'")
        result = cursor.fetchone()
        
        if result:
            print("Table 'update_times' already exists.")
        else:
            print("Table 'update_times' not found. Creating table now...")
            create_query = """
                CREATE TABLE update_times (
                    id INTEGER PRIMARY KEY,
                    last_update_time_positions DATETIME,
                    last_update_positions_source TEXT,
                    last_update_time_prices DATETIME,
                    last_update_prices_source TEXT,
                    last_update_time_jupiter DATETIME,
                    last_update_jupiter_source TEXT
                );
            """
            cursor.execute(create_query)
            print("Table 'update_times' created successfully.")
            
            # Insert a default row with id=1.
            insert_query = """
                INSERT INTO update_times (
                    id, last_update_time_positions, last_update_positions_source,
                    last_update_time_prices, last_update_prices_source,
                    last_update_time_jupiter, last_update_jupiter_source
                ) VALUES (
                    1, NULL, NULL, NULL, NULL, NULL, NULL
                );
            """
            cursor.execute(insert_query)
            conn.commit()
            print("Default row inserted into 'update_times'.")
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        conn.close()
        print("Database connection closed.")
        print("========================================")
        print("Script completed.")
        print("========================================")

if __name__ == "__main__":
    create_update_times_table(DB_PATH)
