#!/usr/bin/env python
"""
local_monitor.py
Description:
  This script runs an infinite loop that:
    1. Invokes update_jupiter_API.py to update the database.
    2. Queries the database for positions and computes position totals via CalcServices.
    3. Displays a colorful status table with the position totals, iteration count, and a PST timestamp.
    4. Shows a continuously updating spinner (with a countdown) during the waiting period.

  Extensive debug logs are output to both the console and a log file "local_mon_dbg.txt".

Configuration:
  - LOOP_DELAY: The delay between iterations (in seconds).

Usage:
  python local_monitor.py
"""

import time
import subprocess
import logging
import sys
from datetime import datetime, timedelta
import pytz

# Try to import update_jupiter_API's main function if available; otherwise, use subprocess.
try:
    from update_jupiter_API import main as update_jupiter_main
except ImportError:
    update_jupiter_main = None

# Import DataLocker and CalcServices for database queries.
from data.data_locker import DataLocker
from utils.calc_services import CalcServices
from config.config_constants import DB_PATH

# Rich for pretty console output.
try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.spinner import Spinner
except ImportError:
    print("Please install rich (pip install rich) to use this script.")
    sys.exit(1)

# ------------------------------------------------------------------------------
# Logging Configuration: Log to console and to 'local_mon_dbg.txt'
# ------------------------------------------------------------------------------
logger = logging.getLogger("LocalMonitor")
logger.setLevel(logging.DEBUG)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_formatter = logging.Formatter("[%(levelname)s] %(asctime)s - %(message)s", datefmt="%H:%M:%S")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# File handler: writes to local_mon_dbg.txt
file_handler = logging.FileHandler("local_mon_dbg.txt")
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter("[%(levelname)s] %(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# ------------------------------------------------------------------------------
# Configuration: Loop delay in seconds (e.g., 10 minutes)
# ------------------------------------------------------------------------------
LOOP_DELAY = 3 * 60  # 10 minutes


# ------------------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------------------

def convert_iso_to_pst(iso_str):
    """Convert an ISO timestamp to a PST formatted string."""
    if not iso_str:
        return "N/A"
    try:
        pst = pytz.timezone("US/Pacific")
        dt = datetime.fromisoformat(iso_str)
        dt_pst = dt.astimezone(pst)
        return dt_pst.strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception as e:
        logger.error(f"Error converting timestamp: {e}")
        return "N/A"


def run_update():
    """Run the update_jupiter_API script to update the database."""
    logger.info("Running update_jupiter_API...")
    if update_jupiter_main:
        try:
            update_jupiter_main()
            logger.info("update_jupiter_API executed via import successfully.")
        except Exception as e:
            logger.error(f"Error executing update_jupiter_API via import: {e}", exc_info=True)
    else:
        try:
            result = subprocess.run(
                ["python", "c:\\space_ship\\update_jupiter_API.py"],
                capture_output=True, text=True, check=True
            )
            logger.info("update_jupiter_API executed via subprocess successfully.")
            logger.debug(result.stdout)
        except subprocess.CalledProcessError as e:
            logger.error(f"Subprocess error: {e.stderr}", exc_info=True)


def query_position_totals():
    """Query the database for positions and calculate totals using CalcServices."""
    try:
        dl = DataLocker.get_instance(DB_PATH)
        positions = dl.get_positions()
        totals = CalcServices().calculate_totals(positions)
        logger.debug(f"Queried {len(positions)} positions. Totals: {totals}")
        return totals, len(positions)
    except Exception as e:
        logger.error(f"Error querying position totals: {e}", exc_info=True)
        return {}, 0


def print_status(iteration, totals, pos_count):
    """Print a colorful console table with the position totals using Rich."""
    console = Console()
    table = Table(title="Local Monitor Status", style="bold")
    table.add_column("Iteration", justify="center", style="cyan", no_wrap=True)
    table.add_column("PST Timestamp", justify="center", style="magenta")
    table.add_column("Total Size", justify="right", style="green")
    table.add_column("Total Value", justify="right", style="green")
    table.add_column("Total Collateral", justify="right", style="green")
    table.add_column("Avg Leverage", justify="right", style="green")
    table.add_column("Avg Travel %", justify="right", style="green")
    table.add_column("Avg Heat", justify="right", style="green")
    table.add_column("Positions Count", justify="center", style="yellow")

    current_iso = datetime.now().isoformat()
    pst_timestamp = convert_iso_to_pst(current_iso)

    table.add_row(
        str(iteration),
        pst_timestamp,
        f"{totals.get('total_size', 0):,.2f}",
        f"{totals.get('total_value', 0):,.2f}",
        f"{totals.get('total_collateral', 0):,.2f}",
        f"{totals.get('avg_leverage', 0):,.2f}",
        f"{totals.get('avg_travel_percent', 0):,.2f}%",
        f"{totals.get('avg_heat_index', 0):,.2f}",
        str(pos_count)
    )
    console.clear()
    console.print(table)


def wait_with_spinner(seconds):
    """Display a spinner with countdown during the waiting period."""
    console = Console()
    spinner = Spinner("dots", text="Waiting for next update...")
    with Live(spinner, refresh_per_second=10, console=console):
        for remaining in range(seconds, 0, -1):
            spinner.text = f"Waiting for next update... {remaining}s remaining"
            time.sleep(1)


# ------------------------------------------------------------------------------
# Main Loop
# ------------------------------------------------------------------------------

def main_loop():
    iteration = 1
    while True:
        logger.info(f"--- Iteration {iteration} started ---")
        try:
            run_update()
        except Exception as e:
            logger.error(f"Update failed: {e}", exc_info=True)
        try:
            totals, pos_count = query_position_totals()
        except Exception as e:
            logger.error(f"Querying totals failed: {e}", exc_info=True)
            totals, pos_count = {}, 0
        print_status(iteration, totals, pos_count)
        logger.info(f"--- Iteration {iteration} completed. Sleeping for {LOOP_DELAY} seconds ---")
        iteration += 1
        wait_with_spinner(LOOP_DELAY)


# ------------------------------------------------------------------------------
# Main Entry
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\nLocal monitor stopped by user.")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
