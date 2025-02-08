import json
import logging
from typing import Any, Dict

logger = logging.getLogger("HybridConfigLoader")


class AppConfig:
    """
    A plain Python config class. Holds your top-level configuration dicts:
    price_config, system_config, api_config, alert_ranges,
    and notification_config. Any extra keys are stored in the 'extra' attribute.
    """
    def __init__(
        self,
        price_config: Dict[str, Any] = None,
        system_config: Dict[str, Any] = None,
        api_config: Dict[str, Any] = None,
        alert_ranges: Dict[str, Any] = None,
        notification_config: Dict[str, Any] = None,
        **kwargs
    ):
        self.price_config = price_config or {}
        self.system_config = system_config or {}
        self.api_config = api_config or {}
        self.alert_ranges = alert_ranges or {}
        self.notification_config = notification_config or {}
        self.extra = kwargs

    def __repr__(self):
        return (
            f"AppConfig("
            f"price_config={self.price_config}, "
            f"system_config={self.system_config}, "
            f"api_config={self.api_config}, "
            f"alert_ranges={self.alert_ranges}, "
            f"notification_config={self.notification_config}, "
            f"extra={self.extra}"
            f")"
        )

    def model_dump(self) -> Dict[str, Any]:
        """
        Returns the configuration as a dictionary.
        """
        data = {
            "price_config": self.price_config,
            "system_config": self.system_config,
            "api_config": self.api_config,
            "alert_ranges": self.alert_ranges,
            "notification_config": self.notification_config,
        }
        data.update(self.extra)
        return data


def deep_merge_dicts(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively merges 'overrides' into 'base'.
    If both base[key] and overrides[key] are dict, merge them.
    Otherwise, overrides[key] overwrites base[key].
    """
    merged = dict(base)
    for key, val in overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = deep_merge_dicts(merged[key], val)
        else:
            merged[key] = val
    return merged


def ensure_overrides_table(db_conn):
    """
    Creates the 'config_overrides' table if it doesn't exist,
    and ensures there's a row with id=1 so we can do an UPDATE easily.
    """
    try:
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS config_overrides (
                id INTEGER PRIMARY KEY,
                overrides TEXT
            )
        """)
        db_conn.execute("""
            INSERT OR IGNORE INTO config_overrides (id, overrides)
            VALUES (1, '{}')
        """)
        db_conn.commit()
    except Exception as e:
        logger.error(f"Error ensuring config_overrides table: {e}")


def load_overrides_from_db(db_conn) -> Dict[str, Any]:
    """
    Safely queries the DB for config overrides, returns them as a dict.
    """
    try:
        ensure_overrides_table(db_conn)
        row = db_conn.execute("SELECT overrides FROM config_overrides WHERE id=1").fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return {}
    except Exception as e:
        logger.error(f"Could not load overrides from DB: {e}")
        return {}


def load_json_config(json_path: str) -> Dict[str, Any]:
    """
    Loads a JSON file, returns a dict. If missing/bad, returns empty.
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Config file '{json_path}' not found. Returning empty dict.")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON in '{json_path}': {e}")
        return {}


def load_config_hybrid(json_path: str, db_conn) -> AppConfig:
    """
    1) Load base config from JSON.
    2) Load overrides from DB.
    3) Merge them (DB overrides win).
    4) Return an AppConfig object.
    """
    base_data = load_json_config(json_path)
    db_overrides = load_overrides_from_db(db_conn)
    merged_data = deep_merge_dicts(base_data, db_overrides)
    try:
        return AppConfig(
            price_config=merged_data.get("price_config", {}),
            system_config=merged_data.get("system_config", {}),
            api_config=merged_data.get("api_config", {}),
            alert_ranges=merged_data.get("alert_ranges", {}),
            notification_config=merged_data.get("notification_config", {}),
            **{k: v for k, v in merged_data.items() if k not in ["price_config", "system_config", "api_config", "alert_ranges", "notification_config"]}
        )
    except Exception as e:
        logger.error(f"Error building AppConfig: {e}")
        return AppConfig()
