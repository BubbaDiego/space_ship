#!/usr/bin/env python
"""
Module: position_service.py
Description:
    Provides services for retrieving and enriching positions data.
    Includes methods to get all positions, enrich a single position, and fill positions with the latest price.
"""

import logging
from typing import List, Dict, Any
from data.data_locker import DataLocker
from config.config_constants import DB_PATH
from utils.calc_services import CalcServices  # Updated import

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    # Add a basic console handler if none exist
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(levelname)s] %(asctime)s - %(name)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)


class PositionService:
    @staticmethod
    def get_all_positions(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
        try:
            dl = DataLocker.get_instance(db_path)
            raw_positions = dl.read_positions()
            positions = []
            for pos in raw_positions:
                enriched = PositionService.enrich_position(pos)
                positions.append(enriched)
            return positions
        except Exception as e:
            logger.error(f"Error retrieving positions: {e}", exc_info=True)
            raise

    @staticmethod
    def enrich_position(position: Dict[str, Any]) -> Dict[str, Any]:
        try:
            calc = CalcServices()
            # Compute profit value
            position['profit'] = calc.calculate_value(position)

            # Compute leverage
            collateral = float(position.get('collateral', 0))
            size = float(position.get('size', 0))
            if collateral > 0:
                position['leverage'] = calc.calculate_leverage(size, collateral)
            else:
                position['leverage'] = None

            # Compute travel percent
            if all(k in position for k in ['entry_price', 'current_price', 'liquidation_price']):
                position['travel_percent'] = calc.calculate_travel_percent(
                    position.get('position_type', ''),
                    float(position['entry_price']),
                    float(position['current_price']),
                    float(position['liquidation_price'])
                )
            else:
                position['travel_percent'] = None

            # Compute liquidation distance (absolute difference)
            if 'current_price' in position and 'liquidation_price' in position:
                position['liquidation_distance'] = calc.calculate_liquid_distance(
                    float(position['current_price']),
                    float(position['liquidation_price'])
                )
            else:
                position['liquidation_distance'] = None

            # Compute heat index
            position['heat_index'] = calc.calculate_heat_index(position)

            return position
        except Exception as e:
            logger.error(f"Error enriching position data: {e}", exc_info=True)
            raise

    @staticmethod
    def fill_positions_with_latest_price(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        For each position in the provided list, retrieve the latest price for its asset type
        using DataLocker and update the position's 'current_price' field accordingly.

        Parameters:
            positions (List[Dict[str, Any]]): List of position dictionaries.

        Returns:
            List[Dict[str, Any]]: Updated list of positions with refreshed current prices.
        """
        try:
            dl = DataLocker.get_instance()
            for pos in positions:
                asset_type = pos.get('asset_type')
                if asset_type:
                    latest_price_data = dl.get_latest_price(asset_type)
                    if latest_price_data and 'current_price' in latest_price_data:
                        try:
                            pos['current_price'] = float(latest_price_data['current_price'])
                        except (ValueError, TypeError) as conv_err:
                            logger.error(f"Error converting latest price to float for asset '{asset_type}': {conv_err}")
                            pos['current_price'] = pos.get('current_price')
                    else:
                        logger.warning(f"No latest price found for asset type: {asset_type}")
                else:
                    logger.warning("Position does not have an 'asset_type' field.")
            return positions
        except Exception as e:
            logger.error(f"Error in fill_positions_with_latest_price: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    try:
        positions = PositionService.get_all_positions()
        updated_positions = PositionService.fill_positions_with_latest_price(positions)
        for pos in updated_positions:
            asset = pos.get('asset_type', 'Unknown')
            current_price = pos.get('current_price', 'N/A')
            print(f"Position for asset {asset} updated with current_price: {current_price}")
    except Exception as e:
        logger.error(f"Error during testing: {e}", exc_info=True)
