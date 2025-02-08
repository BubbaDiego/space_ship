import os

# Go one level up from the current file (assuming this file is in the 'config' folder)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(BASE_DIR, "data", "mother_brain.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config", "sonic_config.json")
