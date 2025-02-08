# calc_services.py

from typing import Optional, List, Dict
import sqlite3


class CalcServices:
    """
    This class provides all aggregator/analytics logic for positions:
     - Calculating value (long/short),
     - Leverage,
     - Travel %,
     - Heat index,
     - Summaries/Totals,
     - Optional color coding for display.
    """

    def __init__(self):
        # Ranges for color coding (used by get_color) for some metrics.
        self.color_ranges = {
            "travel_percent": [
                (0, 25, "green"),
                (25, 50, "yellow"),
                (50, 75, "orange"),
                (75, 100, "red")
            ],
            "heat_index": [
                (0, 20, "blue"),
                (20, 40, "green"),
                (40, 60, "yellow"),
                (60, 80, "orange"),
                (80, 100, "red")
            ],
            "collateral": [
                (0, 500, "lightgreen"),
                (500, 1000, "yellow"),
                (1000, 2000, "orange"),
                (2000, 10000, "red")
            ]
        }

    def calculate_value(self, position):
        # Since size is already in USD, just return it.
        size = float(position.get("size", 0.0))
        return round(size, 2)

    def calculate_leverage(self, size: float, collateral: float) -> float:
        if size <= 0 or collateral <= 0:
            return 0.0
        return round(size / collateral, 2)

    def calculate_travel_percent(self,
                                 position_type: str,
                                 entry_price: float,
                                 current_price: float,
                                 liquidation_price: float) -> float:
        """
        Example function that calculates travel_percent for both LONG and SHORT.
        Adjust as needed to fit your exact logic.
        """
        ptype = position_type.upper()

        # Basic checks
        if entry_price <= 0 or liquidation_price <= 0:
            return 0.0

        def pct_of_range(numer, denom):
            return (numer / denom) * 100 if denom else 0.0

        travel_percent = 0.0
        profit_price = entry_price * 2

        if ptype == "LONG":
            if current_price < entry_price:
                denom = (entry_price - liquidation_price)
                numer = (current_price - entry_price)
                travel_percent = pct_of_range(numer, -abs(denom))
            else:
                denom = (profit_price - entry_price)
                numer = (current_price - entry_price)
                travel_percent = pct_of_range(numer, denom)
        else:  # SHORT
            if current_price > entry_price:
                denom = (liquidation_price - entry_price)
                numer = (entry_price - current_price)
                travel_percent = pct_of_range(numer, -abs(denom))
            else:
                denom = abs(entry_price - profit_price)
                numer = (entry_price - current_price)
                travel_percent = pct_of_range(numer, denom)

        return travel_percent

    def aggregator_positions(self, positions: List[dict], db_path: str) -> List[dict]:
        """
        For each position in `positions`, compute travel percent,
        liquidation distance, value, leverage, and heat index.
        Also updates the DB with the new travel_percent and liquidation_distance.
        """
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        for pos in positions:
            position_type = (pos.get("position_type") or "LONG").upper()
            entry_price = float(pos.get("entry_price", 0.0))
            current_price = float(pos.get("current_price", 0.0))
            liquidation_price = float(pos.get("liquidation_price", 0.0))
            collateral = float(pos.get("collateral", 0.0))
            size = float(pos.get("size", 0.0))

            # Calculate travel percent (using a version with no profit anchor)
            travel_percent = self.calculate_travel_percent_no_profit(
                position_type,
                entry_price,
                current_price,
                liquidation_price
            )
            pos["current_travel_percent"] = travel_percent

            # Calculate liquidation distance
            pos["liquidation_distance"] = self.calculate_liquid_distance(
                current_price=current_price,
                liquidation_price=liquidation_price
            )

            # Update the DB for current_travel_percent and liquidation_distance
            try:
                cursor.execute("""
                    UPDATE positions
                       SET current_travel_percent = ?
                     WHERE id = ?
                """, (travel_percent, pos["id"]))
            except Exception as e:
                print(f"Error updating travel_percent for position {pos['id']}: {e}")

            try:
                cursor.execute("""
                    UPDATE positions
                       SET liquidation_distance = ?
                     WHERE id = ?
                """, (pos["liquidation_distance"], pos["id"]))
            except Exception as e:
                print(f"Error updating liquidation_distance for position {pos['id']}: {e}")

            # Basic PnL calculation => Value
            if entry_price > 0:
                token_count = size / entry_price
                if position_type == "LONG":
                    pnl = (current_price - entry_price) * token_count
                else:
                    pnl = (entry_price - current_price) * token_count
            else:
                pnl = 0.0
            pos["value"] = round(collateral + pnl, 2)

            # Leverage = size / collateral
            if collateral > 0:
                pos["leverage"] = round(size / collateral, 2)
            else:
                pos["leverage"] = 0.0

            # Heat Index calculation
            pos["heat_index"] = self.calculate_heat_index(pos) or 0.0

        conn.commit()
        conn.close()
        return positions

    def calculate_liquid_distance(self, current_price: float, liquidation_price: float) -> float:
        """
        Returns the absolute difference between current_price and liquidation_price.
        """
        if current_price is None:
            current_price = 0.0
        if liquidation_price is None:
            liquidation_price = 0.0
        return round(abs(liquidation_price - current_price), 2)

    def calculate_heat_index(self, position: dict) -> Optional[float]:
        """
        Example "heat index" = (size * leverage) / collateral.
        Returns None if collateral <= 0.
        """
        size = float(position.get("size", 0.0) or 0.0)
        leverage = float(position.get("leverage", 0.0) or 0.0)
        collateral = float(position.get("collateral", 0.0) or 0.0)
        if collateral <= 0:
            return None
        hi = (size * leverage) / collateral
        return round(hi, 2)

    def calculate_travel_percent_no_profit(self,
                                           position_type: str,
                                           entry_price: float,
                                           current_price: float,
                                           liquidation_price: float) -> float:
        """
        Calculates Travel Percent with NO profit anchor.
        - At entry_price => 0%.
        - Approaching liquidation_price => goes down to -100%.
        """
        if entry_price <= 0 or liquidation_price <= 0 or entry_price == liquidation_price:
            return 0.0

        ptype = position_type.upper()

        def safe_ratio(numer, denom):
            if denom == 0:
                return 0.0
            return (numer / denom) * 100

        if ptype == "LONG":
            denom = abs(entry_price - liquidation_price)
            numer = current_price - entry_price
            travel_percent = safe_ratio(numer, denom)
        else:  # SHORT
            denom = abs(entry_price - liquidation_price)
            numer = entry_price - current_price
            travel_percent = safe_ratio(numer, denom)

        return travel_percent

    def prepare_positions_for_display(self, positions: List[dict]) -> List[dict]:
        processed_positions = []

        for idx, pos in enumerate(positions, start=1):
            print(f"\n[DEBUG] Position #{idx} BEFORE aggregator => {pos}")

            raw_ptype = pos.get("position_type", "LONG")
            ptype_lower = raw_ptype.strip().lower()
            if "short" in ptype_lower:
                position_type = "SHORT"
            else:
                position_type = "LONG"

            entry_price = float(pos.get("entry_price", 0.0))
            current_price = float(pos.get("current_price", 0.0))
            collateral = float(pos.get("collateral", 0.0))
            size = float(pos.get("size", 0.0))
            liquidation_price = float(pos.get("liquidation_price", 0.0))

            pos["current_travel_percent"] = self.calculate_travel_percent(
                position_type,
                entry_price,
                current_price,
                liquidation_price
            )

            print(
                f"[DEBUG] Normalized => type={position_type}, entry={entry_price}, current={current_price}, collat={collateral}, size={size}, travel_percent={pos['current_travel_percent']}")

            if entry_price <= 0:
                pnl = 0.0
            else:
                token_count = size / entry_price
                if position_type == "LONG":
                    pnl = (current_price - entry_price) * token_count
                else:
                    pnl = (entry_price - current_price) * token_count

            pos["value"] = round(collateral + pnl, 2)
            if collateral > 0:
                pos["leverage"] = round(size / collateral, 2)
            else:
                pos["leverage"] = 0.0

            pos["heat_index"] = self.calculate_heat_index(pos) or 0.0

            print(f"[DEBUG] Position #{idx} AFTER aggregator => {pos}")

            processed_positions.append(pos)

        return processed_positions

    def calculate_totals(self, positions: List[dict]) -> dict:
        total_size = 0.0
        total_value = 0.0
        total_collateral = 0.0
        total_heat_index = 0.0
        heat_index_count = 0
        weighted_leverage_sum = 0.0
        weighted_travel_percent_sum = 0.0

        for pos in positions:
            size = float(pos.get("size") or 0.0)
            value = float(pos.get("value") or 0.0)
            collateral = float(pos.get("collateral") or 0.0)
            leverage = float(pos.get("leverage") or 0.0)
            travel_percent = float(pos.get("current_travel_percent") or 0.0)
            heat_index = float(pos.get("heat_index") or 0.0)

            total_size += size
            total_value += value
            total_collateral += collateral
            weighted_leverage_sum += (leverage * size)
            weighted_travel_percent_sum += (travel_percent * size)

            if heat_index != 0.0:
                total_heat_index += heat_index
                heat_index_count += 1

        if total_size > 0:
            avg_leverage = weighted_leverage_sum / total_size
            avg_travel_percent = weighted_travel_percent_sum / total_size
        else:
            avg_leverage = 0.0
            avg_travel_percent = 0.0

        avg_heat_index = total_heat_index / heat_index_count if heat_index_count > 0 else 0.0

        return {
            "total_size": total_size,
            "total_value": total_value,
            "total_collateral": total_collateral,
            "avg_leverage": avg_leverage,
            "avg_travel_percent": avg_travel_percent,
            "avg_heat_index": avg_heat_index
        }

    def get_color(self, value: float, metric: str) -> str:
        if metric not in self.color_ranges:
            return "white"
        for (lower, upper, color) in self.color_ranges[metric]:
            if lower <= value < upper:
                return color
        return "red"

    def get_alert_class(self, value: float, low_thresh: Optional[float], med_thresh: Optional[float],
                        high_thresh: Optional[float], direction: str = "increasing_bad") -> str:
        """
        Returns a CSS class string based on thresholds and metric direction.

        For metrics with direction "increasing_bad" (e.g. size, where higher is worse):
          - If value < low_thresh: returns "alert-low" (green, OK)
          - If low_thresh <= value < med_thresh: returns "alert-medium" (yellow, caution)
          - If value >= med_thresh: returns "alert-high" (red, alert)

        For metrics with direction "decreasing_bad", the logic is reversed.
        """
        if low_thresh is None:
            low_thresh = 0.0
        if med_thresh is None:
            med_thresh = 0.0
        if high_thresh is None:
            high_thresh = float('inf')

        if direction == "increasing_bad":
            if value < low_thresh:
                return "alert-low"
            elif value < med_thresh:
                return "alert-medium"
            else:
                return "alert-high"
        elif direction == "decreasing_bad":
            if value > low_thresh:
                return "alert-low"
            elif value > med_thresh:
                return "alert-medium"
            else:
                return "alert-high"
        else:
            return ""

# End of calc_services.py
