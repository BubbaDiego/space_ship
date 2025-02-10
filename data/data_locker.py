#!/usr/bin/env python
import os
import sqlite3
import logging
from typing import List, Dict, Optional
from datetime import datetime
from uuid import uuid4
from config.config_constants import DB_PATH

class DataLocker:
    """
    A synchronous DataLocker that manages database interactions using sqlite3.
    Stores:
      - Prices in the 'prices' table.
      - Positions in the 'positions' table.
      - Alerts in the 'alerts' table.
      - System variables (timestamps and balance vars) in the 'system_vars' table.
      - Brokers in the 'brokers' table.
      - Wallets in the 'wallets' table.
      - Aggregated positions snapshots in the 'positions_totals_history' table.
    """

    _instance: Optional['DataLocker'] = None

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = DB_PATH
        self.db_path = db_path
        self.logger = logging.getLogger("DataLockerLogger")
        self.conn = None
        self.cursor = None
        self._initialize_database()

    def _initialize_database(self):
        try:
            self._init_sqlite_if_needed()

            # Create system_vars table (if needed)
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_vars (
                    id INTEGER PRIMARY KEY,
                    last_update_time_positions DATETIME,
                    last_update_positions_source TEXT,
                    last_update_time_prices DATETIME,
                    last_update_prices_source TEXT,
                    last_update_time_jupiter DATETIME
                )
            """)
            self.cursor.execute("""
                INSERT OR IGNORE INTO system_vars (
                    id,
                    last_update_time_positions,
                    last_update_positions_source,
                    last_update_time_prices,
                    last_update_prices_source,
                    last_update_time_jupiter
                )
                VALUES (1, NULL, NULL, NULL, NULL, NULL)
            """)

            # Add new columns for Jupiter updates if missing
            self.cursor.execute("PRAGMA table_info(system_vars)")
            existing_cols = [row["name"] for row in self.cursor.fetchall()]
            if "last_update_time_jupiter" not in existing_cols:
                self.cursor.execute("""
                    ALTER TABLE system_vars
                    ADD COLUMN last_update_time_jupiter DATETIME
                """)
                self.logger.info("Added 'last_update_time_jupiter' column to 'system_vars' table.")
            if "last_update_jupiter_source" not in existing_cols:
                self.cursor.execute("""
                    ALTER TABLE system_vars
                    ADD COLUMN last_update_jupiter_source TEXT
                """)
                self.logger.info("Added 'last_update_jupiter_source' column to 'system_vars' table.")

            # Add additional balance columns if missing
            self.cursor.execute("PRAGMA table_info(system_vars)")
            existing_cols = [row["name"] for row in self.cursor.fetchall()]
            for col, sql in [
                ("total_brokerage_balance", "ALTER TABLE system_vars ADD COLUMN total_brokerage_balance REAL DEFAULT 0.0"),
                ("total_wallet_balance", "ALTER TABLE system_vars ADD COLUMN total_wallet_balance REAL DEFAULT 0.0"),
                ("total_balance", "ALTER TABLE system_vars ADD COLUMN total_balance REAL DEFAULT 0.0")
            ]:
                if col not in existing_cols:
                    self.cursor.execute(sql)
                    self.logger.info(f"Added '{col}' column to 'system_vars' table.")

            # Create prices table if it doesn't exist
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS prices (
                    id TEXT PRIMARY KEY,
                    asset_type TEXT,
                    current_price REAL,
                    previous_price REAL,
                    last_update_time DATETIME,
                    previous_update_time DATETIME,
                    source TEXT
                )
            """)

            # Create positions table if it doesn't exist
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    asset_type TEXT,
                    position_type TEXT,
                    entry_price REAL,
                    liquidation_price REAL,
                    current_travel_percent REAL,
                    value REAL,
                    collateral REAL,
                    size REAL,
                    leverage REAL,
                    wallet_name TEXT,
                    last_updated DATETIME,
                    alert_reference_id TEXT,
                    hedge_buddy_id TEXT,
                    current_price REAL,
                    liquidation_distance REAL,
                    heat_index REAL,
                    current_heat_index REAL,
                    pnl_after_fees_usd REAL
                )
            """)

            # Create alerts table if it doesn't exist
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    alert_type TEXT,
                    asset_type TEXT,
                    trigger_value REAL,
                    condition TEXT,
                    notification_type TEXT,
                    last_triggered DATETIME,
                    status TEXT,
                    frequency INTEGER,
                    counter INTEGER,
                    liquidation_distance REAL,
                    target_travel_percent REAL,
                    liquidation_price REAL,
                    notes TEXT,
                    position_reference_id TEXT
                )
            """)

            # Create brokers table if it doesn't exist
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS brokers (
                    name TEXT PRIMARY KEY,
                    image_path TEXT,
                    web_address TEXT,
                    total_holding REAL DEFAULT 0.0
                )
            """)

            # Create wallets table if it doesn't exist
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    name TEXT PRIMARY KEY,
                    public_address TEXT,
                    private_address TEXT,
                    image_path TEXT,
                    balance REAL DEFAULT 0.0
                )
            """)

            # Create positions_totals_history table if it doesn't exist
            self.cursor.execute("""
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

            self.conn.commit()
            self.logger.debug("Database initialization complete.")

        except sqlite3.Error as e:
            self.logger.error(f"Error initializing database: {e}", exc_info=True)
            raise

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> 'DataLocker':
        if cls._instance is None:
            cls._instance = cls(db_path)
        return cls._instance

    def _init_sqlite_if_needed(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            self.logger.debug(f"Created directory for DB: {db_dir}")
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        if self.cursor is None:
            self.cursor = self.conn.cursor()

    def get_db_connection(self) -> sqlite3.Connection:
        self._init_sqlite_if_needed()
        return self.conn

    # ----------------------------------------------------------------
    # PRICES
    # ----------------------------------------------------------------

    def read_api_counters(self) -> List[dict]:
        self._init_sqlite_if_needed()
        self.cursor.execute("""
            SELECT api_name, total_reports, last_updated
              FROM api_status_counters
             ORDER BY api_name
        """)
        rows = self.cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "api_name": r["api_name"],
                "total_reports": r["total_reports"],
                "last_updated": r["last_updated"]
            })
        return results

    def reset_api_counters(self):
        self._init_sqlite_if_needed()
        self.cursor.execute("UPDATE api_status_counters SET total_reports = 0")
        self.conn.commit()

    def increment_api_report_counter(self, api_name: str) -> None:
        self._init_sqlite_if_needed()
        self.cursor.execute(
            "SELECT total_reports FROM api_status_counters WHERE api_name = ?",
            (api_name,)
        )
        row = self.cursor.fetchone()
        now_str = datetime.now().isoformat()
        old_count = row["total_reports"] if row else 0
        self.logger.debug(f"Previous total_reports for {api_name}={old_count}")
        if row is None:
            self.cursor.execute("""
                INSERT INTO api_status_counters (api_name, total_reports, last_updated)
                VALUES (?, 1, ?)
            """, (api_name, now_str))
        else:
            self.cursor.execute("""
                UPDATE api_status_counters
                   SET total_reports = total_reports + 1,
                       last_updated = ?
                 WHERE api_name = ?
            """, (now_str, api_name))
        self.conn.commit()
        self.logger.debug(f"Incremented API report counter for {api_name}, last_updated={now_str}.")

    def get_balance_vars(self) -> dict:
        self._init_sqlite_if_needed()
        row = self.cursor.execute("""
            SELECT
              total_brokerage_balance,
              total_wallet_balance,
              total_balance
            FROM system_vars
            WHERE id=1
        """).fetchone()
        if not row:
            return {
                "total_brokerage_balance": 0.0,
                "total_wallet_balance": 0.0,
                "total_balance": 0.0
            }
        return {
            "total_brokerage_balance": row["total_brokerage_balance"] or 0.0,
            "total_wallet_balance": row["total_wallet_balance"] or 0.0,
            "total_balance": row["total_balance"] or 0.0
        }

    def set_balance_vars(self, brokerage_balance: float = None, wallet_balance: float = None, total_balance: float = None):
        self._init_sqlite_if_needed()
        current = self.get_balance_vars()
        new_brokerage = brokerage_balance if brokerage_balance is not None else current["total_brokerage_balance"]
        new_wallet = wallet_balance if wallet_balance is not None else current["total_wallet_balance"]
        new_total = total_balance if total_balance is not None else current["total_balance"]
        self.cursor.execute("""
            UPDATE system_vars
               SET total_brokerage_balance=?,
                   total_wallet_balance=?,
                   total_balance=?
             WHERE id=1
        """, (new_brokerage, new_wallet, new_total))
        self.conn.commit()
        self.logger.debug(f"Updated system_vars => total_brokerage_balance={new_brokerage}, total_wallet_balance={new_wallet}, total_balance={new_total}")

    def insert_price(self, price_dict: dict):
        try:
            self._init_sqlite_if_needed()
            if "id" not in price_dict:
                price_dict["id"] = str(uuid4())
            if "asset_type" not in price_dict:
                price_dict["asset_type"] = "BTC"
            if "current_price" not in price_dict:
                price_dict["current_price"] = 1.0
            if "previous_price" not in price_dict:
                price_dict["previous_price"] = 0.0
            if "last_update_time" not in price_dict:
                price_dict["last_update_time"] = datetime.now().isoformat()
            if "previous_update_time" not in price_dict:
                price_dict["previous_update_time"] = None
            if "source" not in price_dict:
                price_dict["source"] = "Manual"
            self.cursor.execute("""
                INSERT INTO prices (
                    id,
                    asset_type,
                    current_price,
                    previous_price,
                    last_update_time,
                    previous_update_time,
                    source
                )
                VALUES (
                    :id, :asset_type, :current_price, :previous_price,
                    :last_update_time, :previous_update_time, :source
                )
            """, price_dict)
            self.conn.commit()
            self.logger.debug(f"Inserted price row with ID={price_dict['id']}")
        except Exception as e:
            self.logger.exception(f"Unexpected error in insert_price: {e}")
            raise

    def get_prices(self, asset_type: Optional[str] = None) -> List[dict]:
        try:
            self._init_sqlite_if_needed()
            if asset_type:
                self.cursor.execute("""
                    SELECT *
                      FROM prices
                     WHERE asset_type=?
                     ORDER BY last_update_time DESC
                """, (asset_type,))
            else:
                self.cursor.execute("""
                    SELECT *
                      FROM prices
                     ORDER BY last_update_time DESC
                """)
            rows = self.cursor.fetchall()
            price_list = [dict(row) for row in rows]
            self.logger.debug(f"Retrieved {len(price_list)} price rows.")
            return price_list
        except sqlite3.Error as e:
            self.logger.error(f"Database error in get_prices: {e}", exc_info=True)
            return []
        except Exception as e:
            self.logger.exception(f"Unexpected error in get_prices: {e}")
            return []

    def read_positions(self) -> List[dict]:
        self._init_sqlite_if_needed()
        self.cursor.execute("SELECT * FROM positions")
        rows = self.cursor.fetchall()
        return [dict(r) for r in rows]

    def read_prices(self) -> List[dict]:
        self._init_sqlite_if_needed()
        self.cursor.execute("SELECT * FROM prices ORDER BY last_update_time DESC")
        rows = self.cursor.fetchall()
        return [dict(r) for r in rows]

    def get_latest_price(self, asset_type: str) -> Optional[dict]:
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("""
                SELECT *
                  FROM prices
                 WHERE asset_type=?
                 ORDER BY last_update_time DESC
                 LIMIT 1
            """, (asset_type,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            self.logger.error(f"Database error in get_latest_price: {e}", exc_info=True)
            return None
        except Exception as ex:
            self.logger.exception(f"Unexpected error in get_latest_price: {ex}")
            return None

    def delete_price(self, price_id: str):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("DELETE FROM prices WHERE id=?", (price_id,))
            self.conn.commit()
            self.logger.debug(f"Deleted price row ID={price_id}")
        except sqlite3.Error as e:
            self.logger.error(f"Database error in delete_price: {e}", exc_info=True)
            raise
        except Exception as ex:
            self.logger.exception(f"Unexpected error in delete_price: {ex}")
            raise

    # ----------------------------------------------------------------
    # ALERTS
    # ----------------------------------------------------------------

    def create_alert(self, alert_dict: dict):
        try:
            if not alert_dict.get("id"):
                alert_dict["id"] = str(uuid4())
            self._init_sqlite_if_needed()
            self.cursor.execute("""
                INSERT INTO alerts (
                    id,
                    alert_type,
                    asset_type,
                    trigger_value,
                    condition,
                    notification_type,
                    last_triggered,
                    status,
                    frequency,
                    counter,
                    liquidation_distance,
                    target_travel_percent,
                    liquidation_price,
                    notes,
                    position_reference_id
                ) VALUES (
                    :id, :alert_type, :asset_type,
                    :trigger_value, :condition, :notification_type,
                    :last_triggered, :status, :frequency, :counter,
                    :liquidation_distance, :target_travel_percent,
                    :liquidation_price, :notes, :position_reference_id
                )
            """, alert_dict)
            self.conn.commit()
            self.logger.debug(f"Created alert ID={alert_dict['id']}")
        except sqlite3.IntegrityError as ie:
            self.logger.error(f"IntegrityError creating alert: {ie}", exc_info=True)
        except sqlite3.Error as e:
            self.logger.error(f"Database error in create_alert: {e}", exc_info=True)
            raise
        except Exception as ex:
            self.logger.exception(f"Unexpected error in create_alert: {ex}")
            raise

    def get_alerts(self) -> List[dict]:
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("SELECT * FROM alerts")
            rows = self.cursor.fetchall()
            alert_list = [dict(r) for r in rows]
            self.logger.debug(f"Fetched {len(alert_list)} alerts.")
            return alert_list
        except sqlite3.Error as e:
            self.logger.error(f"Database error in get_alerts: {e}", exc_info=True)
            return []
        except Exception as ex:
            self.logger.exception(f"Unexpected error in get_alerts: {ex}")
            return []

    def update_alert_status(self, alert_id: str, new_status: str):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("""
                UPDATE alerts
                   SET status=?
                 WHERE id=?
            """, (new_status, alert_id))
            self.conn.commit()
            self.logger.debug(f"Alert {alert_id} => status={new_status}")
        except sqlite3.Error as e:
            self.logger.error(f"DB error update_alert_status: {e}", exc_info=True)
            raise
        except Exception as ex:
            self.logger.exception(f"Error updating alert status: {ex}")
            raise

    def delete_alert(self, alert_id: str):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            self.conn.commit()
            self.logger.debug(f"Deleted alert ID={alert_id}")
        except sqlite3.Error as e:
            self.logger.error(f"DB error in delete_alert: {e}", exc_info=True)
            raise
        except Exception as ex:
            self.logger.exception(f"Error deleting alert: {ex}")
            raise

    # ----------------------------------------------------------------
    # Insert/Update Price
    # ----------------------------------------------------------------

    def insert_or_update_price(self, asset_type: str, current_price: float, source: str, timestamp: Optional[datetime] = None):
        self._init_sqlite_if_needed()
        if timestamp is None:
            timestamp = datetime.now()
        price_dict = {
            "id": str(uuid4()),
            "asset_type": asset_type,
            "current_price": current_price,
            "previous_price": 0.0,
            "last_update_time": timestamp.isoformat(),
            "previous_update_time": None,
            "source": source
        }
        self.insert_price(price_dict)

    # ----------------------------------------------------------------
    # POSITIONS
    # ----------------------------------------------------------------

    def create_position(self, pos_dict: dict):
        if "id" not in pos_dict:
            pos_dict["id"] = str(uuid4())
        pos_dict.setdefault("asset_type", "BTC")
        pos_dict.setdefault("position_type", "LONG")
        pos_dict.setdefault("entry_price", 0.0)
        pos_dict.setdefault("liquidation_price", 0.0)
        pos_dict.setdefault("current_travel_percent", 0.0)
        pos_dict.setdefault("value", 0.0)
        pos_dict.setdefault("collateral", 0.0)
        pos_dict.setdefault("size", 0.0)
        pos_dict.setdefault("leverage", 0.0)
        pos_dict.setdefault("wallet_name", "Default")
        pos_dict.setdefault("last_updated", datetime.now().isoformat())
        pos_dict.setdefault("alert_reference_id", None)
        pos_dict.setdefault("hedge_buddy_id", None)
        pos_dict.setdefault("current_price", 0.0)
        pos_dict.setdefault("liquidation_distance", None)
        pos_dict.setdefault("heat_index", 0.0)
        pos_dict.setdefault("current_heat_index", 0.0)
        pos_dict.setdefault("pnl_after_fees_usd", 0.0)
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("""
                INSERT INTO positions (
                    id, asset_type, position_type,
                    entry_price, liquidation_price, current_travel_percent,
                    value, collateral, size, wallet_name, leverage, last_updated,
                    alert_reference_id, hedge_buddy_id, current_price,
                    liquidation_distance, heat_index, current_heat_index,
                    pnl_after_fees_usd
                ) VALUES (
                    :id, :asset_type, :position_type,
                    :entry_price, :liquidation_price, :current_travel_percent,
                    :value, :collateral, :size, :wallet_name, :leverage, :last_updated,
                    :alert_reference_id, :hedge_buddy_id, :current_price,
                    :liquidation_distance, :heat_index, :current_heat_index,
                    :pnl_after_fees_usd
                )
            """, pos_dict)
            self.conn.commit()
            self.logger.debug(f"Created position ID={pos_dict['id']}")
        except Exception as ex:
            self.logger.exception(f"Error creating position: {ex}")
            raise

    def get_positions(self) -> List[dict]:
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("SELECT * FROM positions")
            rows = self.cursor.fetchall()
            results = [dict(r) for r in rows]
            self.logger.debug(f"Fetched {len(results)} positions.")
            return results
        except sqlite3.Error as e:
            self.logger.error(f"DB error get_positions: {e}", exc_info=True)
            return []
        except Exception as ex:
            self.logger.exception(f"Error get_positions: {ex}")
            return []

    def delete_position(self, position_id: str):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("DELETE FROM positions WHERE id=?", (position_id,))
            self.conn.commit()
            self.logger.debug(f"Deleted position ID={position_id}")
        except sqlite3.Error as e:
            self.logger.error(f"DB error delete_position: {e}", exc_info=True)
            raise
        except Exception as ex:
            self.logger.exception(f"Error delete_position: {ex}")
            raise

    def delete_all_positions(self):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("DELETE FROM positions")
            self.conn.commit()
            self.logger.debug("Deleted all positions.")
        except Exception as ex:
            self.logger.exception(f"Error in delete_all_positions: {ex}")
            raise

    # ----------------------------------------------------------------
    # GET / SET last update times (system_vars table)
    # ----------------------------------------------------------------

    def set_last_update_times(self, positions_dt=None, positions_source=None, prices_dt=None, prices_source=None, jupiter_dt=None):
        current = self.get_last_update_times() or {}
        new_positions_dt = positions_dt.isoformat() if positions_dt else current.get("last_update_time_positions", None)
        new_prices_dt = prices_dt.isoformat() if prices_dt else current.get("last_update_time_prices", None)
        new_jupiter_dt = jupiter_dt.isoformat() if jupiter_dt else current.get("last_update_time_jupiter", None)

        cursor = self.conn.cursor()
        if current:
            cursor.execute("""
                UPDATE system_vars
                   SET last_update_time_positions = ?,
                       last_update_positions_source = ?,
                       last_update_time_prices = ?,
                       last_update_prices_source = ?,
                       last_update_time_jupiter = ?
                 WHERE id = 1
            """, (new_positions_dt, positions_source, new_prices_dt, prices_source, new_jupiter_dt))
        else:
            cursor.execute("""
                INSERT INTO system_vars 
                    (id, last_update_time_positions, last_update_positions_source,
                     last_update_time_prices, last_update_prices_source, last_update_time_jupiter)
                VALUES (1, ?, ?, ?, ?, ?)
            """, (new_positions_dt, positions_source, new_prices_dt, prices_source, new_jupiter_dt))
        self.conn.commit()
        cursor.close()

    def get_last_update_times(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT last_update_time_positions, last_update_positions_source,
                   last_update_time_prices, last_update_prices_source,
                   last_update_time_jupiter
              FROM system_vars
             WHERE id = 1
             LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        if row is not None:
            return dict(row)
        else:
            return {}

    # ----------------------------------------------------------------
    # WALLET & BROKER
    # ----------------------------------------------------------------

    def read_wallets(self) -> List[dict]:
        self._init_sqlite_if_needed()
        self.cursor.execute("SELECT * FROM wallets")
        rows = self.cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "name": r["name"],
                "public_address": r["public_address"],
                "private_address": r["private_address"],
                "image_path": r["image_path"],
                "balance": float(r["balance"])
            })
        return results

    def update_wallet(self, wallet_name, wallet_dict):
        self._init_sqlite_if_needed()
        query = """
            UPDATE wallets 
               SET name = ?,
                   public_address = ?,
                   private_address = ?,
                   image_path = ?,
                   balance = ?
             WHERE name = ?
        """
        self.cursor.execute(query, (
            wallet_dict.get("name"),
            wallet_dict.get("public_address"),
            wallet_dict.get("private_address"),
            wallet_dict.get("image_path"),
            wallet_dict.get("balance"),
            wallet_name
        ))
        self.conn.commit()

    def delete_positions_for_wallet(self, wallet_name: str):
        self._init_sqlite_if_needed()
        self.logger.info(f"Deleting positions for wallet: {wallet_name}")
        self.cursor.execute("DELETE FROM positions WHERE wallet_name=?", (wallet_name,))
        self.conn.commit()

    def update_position(self, position_id: str, size: float, collateral: float):
        try:
            self._init_sqlite_if_needed()
            query = """
            UPDATE positions
               SET size=?,
                   collateral=?
             WHERE id=?
            """
            self.cursor.execute(query, (size, collateral, position_id))
            self.conn.commit()
        except Exception as ex:
            self.logger.exception(f"Error updating position {position_id}: {ex}")
            raise

    def create_wallet(self, wallet_dict: dict):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("""
                INSERT INTO wallets (name, public_address, private_address, image_path, balance)
                VALUES (?,?,?,?,?)
            """, (
                wallet_dict.get("name"),
                wallet_dict.get("public_address"),
                wallet_dict.get("private_address"),
                wallet_dict.get("image_path"),
                wallet_dict.get("balance", 0.0)
            ))
            self.conn.commit()
        except Exception as ex:
            self.logger.exception(f"Error creating wallet: {ex}")
            raise

    def create_broker(self, broker_dict: dict):
        self._init_sqlite_if_needed()
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO brokers (name, image_path, web_address, total_holding)
                VALUES (?,?,?,?)
            """, (
                broker_dict.get("name"),
                broker_dict.get("image_path"),
                broker_dict.get("web_address"),
                broker_dict.get("total_holding", 0.0)
            ))
            self.conn.commit()
        except sqlite3.Error as ex:
            self.logger.error(f"DB error create_broker: {ex}", exc_info=True)
            raise

    def read_brokers(self) -> List[dict]:
        self._init_sqlite_if_needed()
        self.cursor.execute("SELECT * FROM brokers")
        rows = self.cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "name": r["name"],
                "image_path": r["image_path"],
                "web_address": r["web_address"],
                "total_holding": float(r["total_holding"])
            })
        return results

    def read_positions_raw(self) -> List[Dict]:
        self._init_sqlite_if_needed()
        results: List[Dict] = []
        try:
            self.logger.debug("Reading positions raw...")
            self.cursor.execute("SELECT * FROM positions")
            rows = self.cursor.fetchall()
            for row in rows:
                results.append(dict(row))
            self.logger.debug(f"Fetched {len(results)} raw positions.")
            return results
        except Exception as ex:
            self.logger.error(f"Error reading raw positions: {ex}", exc_info=True)
            return []

    def record_positions_totals_snapshot(self, totals: dict):
        """
        Inserts a snapshot of aggregated positions totals into the positions_totals_history table.
        """
        try:
            self._init_sqlite_if_needed()
            snapshot_id = str(uuid4())
            snapshot_time = datetime.now().isoformat()
            self.cursor.execute("""
                INSERT INTO positions_totals_history (
                    id, snapshot_time, total_size, total_value, total_collateral,
                    avg_leverage, avg_travel_percent, avg_heat_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_id,
                snapshot_time,
                totals.get("total_size", 0.0),
                totals.get("total_value", 0.0),
                totals.get("total_collateral", 0.0),
                totals.get("avg_leverage", 0.0),
                totals.get("avg_travel_percent", 0.0),
                totals.get("avg_heat_index", 0.0)
            ))
            self.conn.commit()
            self.logger.debug(f"Recorded positions totals snapshot with ID={snapshot_id}.")
        except Exception as e:
            self.logger.exception(f"Error recording positions totals snapshot: {e}")
            raise

    def update_position_size(self, position_id: str, new_size: float):
        try:
            self._init_sqlite_if_needed()
            self.cursor.execute("""
                UPDATE positions
                   SET size=?
                 WHERE id=?
            """, (new_size, position_id))
            self.conn.commit()
            self.logger.debug(f"Updated position {position_id} => size={new_size}")
        except sqlite3.Error as ex:
            self.logger.error(f"DB error in update_position_size: {ex}", exc_info=True)
            raise
        except Exception as ex:
            self.logger.exception(f"Error update_position_size: {ex}")
            raise

    def get_wallet_by_name(self, wallet_name: str) -> Optional[dict]:
        self._init_sqlite_if_needed()
        self.cursor.execute("""
            SELECT name,
                   public_address,
                   private_address,
                   image_path,
                   balance
              FROM wallets
             WHERE name=?
             LIMIT 1
        """, (wallet_name,))
        row = self.cursor.fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "public_address": row["public_address"],
            "private_address": row["private_address"],
            "image_path": row["image_path"],
            "balance": row["balance"]
        }

    def close(self):
        if self.conn:
            self.conn.close()
            self.logger.debug("Database connection closed.")
