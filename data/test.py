#!/usr/bin/env python
import random
from datetime import datetime, timedelta
from data.data_locker import DataLocker

def inject_sample_data(num_entries=10):
    """
    Injects sample portfolio entries into the database.
    Each entry will have a snapshot_time and a random total_value.
    """
    dl = DataLocker.get_instance()
    now = datetime.now()
    for i in range(num_entries):
        # For each entry, set the snapshot time to a different day in the past.
        snapshot_time = (now - timedelta(days=i)).isoformat()
        # Generate a random total_value between 1000 and 5000.
        total_value = round(random.uniform(1000, 5000), 2)
        entry = {
            "snapshot_time": snapshot_time,
            "total_value": total_value
        }
        try:
            dl.add_portfolio_entry(entry)
            print(f"Inserted portfolio entry with snapshot_time={snapshot_time} and total_value={total_value}")
        except Exception as e:
            print(f"Error inserting entry: {e}")

if __name__ == '__main__':
    inject_sample_data(10)
