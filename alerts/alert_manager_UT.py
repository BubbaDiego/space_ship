#!/usr/bin/env python
import time
import unittest
from unittest.mock import patch
from alert_manager import get_alert_class, AlertManager, METRIC_DIRECTIONS

# Dummy configuration for testing alerts.
DUMMY_CONFIG = {
    "alert_cooldown_seconds": 0,
    "call_refractory_period": 0,
    "system_config": {"alert_monitor_enabled": True},
    "alert_ranges": {
        "profit_ranges": {
            "enabled": True,
            "low": "25",       # Profit below 25: no alert.
            "medium": "50",    # Profit between 25 and 50: alert-low.
            "high": "75"       # Profit between 50 and 75: alert-medium; 75+ => alert-high.
        },
        "travel_percent_liquid_ranges": {
            "enabled": True,
            "low": "-25",      # Travel percent <= -25: low alert.
            "medium": "-50",   # <= -50: medium alert.
            "high": "-74"      # <= -74: high alert.
        }
    },
    "twilio_config": {}
}

# Dummy positions for testing.
DUMMY_POSITIONS = [
    # Profit tests:
    {
        "id": "1",
        "asset_type": "BTC",
        "position_type": "LONG",
        "entry_price": "100",
        "current_price": "105",  # pnl = (105-100)*(200/100)=10; collateral=50, so value = 50+10=60, profit from fallback = 10
        "collateral": "50",
        "size": "200",
        "current_travel_percent": "-15",
        "wallet_name": "WalletA",
        "profit": "30"  # Explicit profit set to 30 -> should trigger alert-low.
    },
    {
        "id": "2",
        "asset_type": "BTC",
        "position_type": "SHORT",
        "entry_price": "100",
        "current_price": "90",   # pnl = (100-90)*(150/100)=15; collateral=40, value = 40+15=55, profit from fallback = 15
        "collateral": "40",
        "size": "150",
        "current_travel_percent": "-25",
        "wallet_name": "WalletB",
        "profit": "55"  # Profit of 55 -> should trigger alert-medium.
    },
    {
        "id": "3",
        "asset_type": "ETH",
        "position_type": "LONG",
        "entry_price": "200",
        "current_price": "250",  # pnl = (250-200)*(100/200)=25; collateral=100, value=125, profit=25
        "collateral": "100",
        "size": "100",
        "current_travel_percent": "-80",  # Should trigger HIGH travel alert.
        "wallet_name": "WalletC",
        "profit": "80"  # Profit of 80 -> should trigger alert-high.
    },
    # Travel tests:
    {
        "id": "4",
        "asset_type": "ETH",
        "position_type": "SHORT",
        "entry_price": "200",
        "current_price": "210",
        "collateral": "80",
        "size": "100",
        "current_travel_percent": "-40",  # -40 falls between -25 and -50, so should be alert-low.
        "wallet_name": "WalletD"
    }
]

# Dummy DataLocker that returns our dummy positions.
class DummyDataLocker:
    def __init__(self, positions):
        self._positions = positions

    def read_positions(self):
        return [dict(pos) for pos in self._positions]

    def get_alerts(self):
        return []

    def get_latest_price(self, asset_type):
        return {"current_price": "100"}

    def get_db_connection(self):
        return None

# Dummy AlertManager subclass to override DataLocker dependency.
class DummyAlertManager(AlertManager):
    def __init__(self, positions, config):
        self.db_path = "dummy.db"
        self.poll_interval = 0
        self.config_path = "dummy_config.json"
        self.last_profit = {}
        self.last_triggered = {}
        self.last_call_triggered = {}
        self.data_locker = DummyDataLocker(positions)
        from utils.calc_services import CalcServices
        self.calc_services = CalcServices()
        self.config = config
        self.twilio_config = config.get("twilio_config", {})
        self.cooldown = float(config.get("alert_cooldown_seconds", 0))
        self.call_refractory_period = float(config.get("call_refractory_period", 0))
        self.monitor_enabled = config.get("system_config", {}).get("alert_monitor_enabled", True)

    def send_call(self, body: str, key: str):
        self.last_call_triggered[key] = time.time()

class TestAlertStates(unittest.TestCase):
    def setUp(self):
        self.alert_manager = DummyAlertManager(DUMMY_POSITIONS, DUMMY_CONFIG)

    def test_profit_alert_no_alert_for_low_profit(self):
        # For position 1, profit = 30, thresholds: low=25, med=50, high=75, should be alert-low.
        alert_msg = self.alert_manager.check_profit(DUMMY_POSITIONS[0])
        self.assertIn("ALERT", alert_msg.upper())
        self.assertIn("LOW", alert_msg.upper())

    def test_profit_alert_medium(self):
        # For position 2, profit = 55, should trigger alert-medium.
        alert_msg = self.alert_manager.check_profit(DUMMY_POSITIONS[1])
        self.assertIn("ALERT", alert_msg.upper())
        self.assertIn("MEDIUM", alert_msg.upper())

    def test_profit_alert_high(self):
        # For position 3, profit = 80, should trigger alert-high.
        alert_msg = self.alert_manager.check_profit(DUMMY_POSITIONS[2])
        self.assertIn("ALERT", alert_msg.upper())
        self.assertIn("HIGH", alert_msg.upper())

    def test_profit_alert_no_alert_for_negative(self):
        # For a negative profit, no alert should be triggered.
        negative_profit_position = dict(DUMMY_POSITIONS[0])
        negative_profit_position["profit"] = "-5"
        alert_msg = self.alert_manager.check_profit(negative_profit_position)
        self.assertEqual(alert_msg, "")

    def test_travel_alert_high(self):
        # For position 3, travel_percent = -80, with thresholds low=-25, medium=-50, high=-74 => should trigger HIGH.
        alert_msg = self.alert_manager.check_travel_percent_liquid(DUMMY_POSITIONS[2])
        self.assertIn("HIGH", alert_msg.upper())

    def test_travel_alert_low(self):
        # For position 4, travel_percent = -40, should trigger LOW alert.
        alert_msg = self.alert_manager.check_travel_percent_liquid(DUMMY_POSITIONS[3])
        self.assertIn("LOW", alert_msg.upper())

if __name__ == "__main__":
    import unittest
    unittest.main()
