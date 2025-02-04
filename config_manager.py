import json
import logging
from typing import Any, Dict

logger = logging.getLogger("ConfigLoader")

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
    Creates the 'config_overrides' table if it doesn't exist.
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
    Load config overrides from DB as a dict.
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
    Reads JSON from file, returns a dict.
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"JSON config file '{json_path}' not found. Returning empty dict.")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON from '{json_path}': {e}")
        return {}

def load_config(json_path: str, db_conn) -> Dict[str, Any]:
    """
    1) Load base config (dict) from JSON.
    2) Load overrides from DB (dict).
    3) Merge them (DB wins).
    4) Return a final dict—NO pydantic here.
    """
    base_data = load_json_config(json_path)
    db_overrides = load_overrides_from_db(db_conn)

    merged_data = deep_merge_dicts(base_data, db_overrides)
    return merged_data  # Just a dict—no Pydantic
