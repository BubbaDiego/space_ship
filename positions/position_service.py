#!/usr/bin/env python
"""
Module: position_service.py
Description:
    Provides services for retrieving, enriching, and updating positions data.
    This includes methods to:
      - Get and enrich all positions.
      - Update Jupiter positions by fetching from the external API.
      - Delete existing Jupiter positions.
      - Record snapshots of aggregated positions data.
"""

import logging
from typing import List, Dict, Any
import requests
from datetime import datetime
from data.data_locker import DataLocker
from config.config_constants import DB_PATH
from utils.calc_services import CalcServices

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    import sys
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(levelname)s] %(asctime)s - %(name)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

class PositionService:
    # Mapping for market mints to asset types
    MINT_TO_ASSET = {
        "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",
        "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
        "So11111111111111111111111111111111111111112": "SOL"
    }

    @staticmethod
    def get_all_positions(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
        """
        Retrieve all positions from the database and enrich each one with calculated data.
        """
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
        """
        Enrich a single position dictionary with computed profit, leverage, travel percent,
        liquidation distance, and heat index.
        """
        try:
            calc = CalcServices()
            # Compute profit value
            position['profit'] = calc.calculate_value(position)

            # Compute leverage
            collateral = float(position.get('collateral', 0))
            size = float(position.get('size', 0))
            position['leverage'] = calc.calculate_leverage(size, collateral) if collateral > 0 else None

            # Compute travel percent if relevant data exists
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
                            logger.error(f"Error converting latest price for asset '{asset_type}': {conv_err}")
                            pos['current_price'] = pos.get('current_price')
                    else:
                        logger.warning(f"No latest price found for asset type: {asset_type}")
                else:
                    logger.warning("Position missing 'asset_type' field.")
            return positions
        except Exception as e:
            logger.error(f"Error in fill_positions_with_latest_price: {e}", exc_info=True)
            raise

    @staticmethod
    def update_jupiter_positions(db_path: str = DB_PATH) -> Dict[str, Any]:
        """
        Fetch Jupiter positions for all wallets in the database, update the positions table,
        and update balance variables. Returns a dictionary with a result message and counts.
        """
        try:
            dl = DataLocker.get_instance(db_path)
            wallets_list = dl.read_wallets()
            if not wallets_list:
                logger.info("No wallets found in DB.")
                return {"message": "No wallets found in DB", "imported": 0, "skipped": 0}

            new_positions = []
            for w in wallets_list:
                public_addr = w.get("public_address", "").strip()
                if not public_addr:
                    logger.info(f"Skipping wallet {w['name']} (no public_address).")
                    continue

                jupiter_url = f"https://perps-api.jup.ag/v1/positions?walletAddress={public_addr}&showTpslRequests=true"
                resp = requests.get(jupiter_url)
                resp.raise_for_status()
                data = resp.json()
                data_list = data.get("dataList", [])
                if not data_list:
                    logger.info(f"No positions for wallet {w['name']} ({public_addr}).")
                    continue

                for item in data_list:
                    try:
                        pos_pubkey = item.get("positionPubkey")
                        if not pos_pubkey:
                            logger.warning(f"Skipping item for wallet {w['name']} due to missing positionPubkey")
                            continue
                        epoch_time = float(item.get("updatedTime", 0))
                        updated_dt = datetime.fromtimestamp(epoch_time)
                        mint = item.get("marketMint", "")
                        asset_type = PositionService.MINT_TO_ASSET.get(mint, "BTC")
                        side = item.get("side", "short").capitalize()
                        travel_pct_value = item.get("pnlChangePctAfterFees")
                        travel_percent = float(travel_pct_value) if travel_pct_value is not None else 0.0
                        pos_dict = {
                            "id": pos_pubkey,
                            "asset_type": asset_type,
                            "position_type": side,
                            "entry_price": float(item.get("entryPrice", 0.0)),
                            "liquidation_price": float(item.get("liquidationPrice", 0.0)),
                            "collateral": float(item.get("collateral", 0.0)),
                            "size": float(item.get("size", 0.0)),
                            "leverage": float(item.get("leverage", 0.0)),
                            "value": float(item.get("value", 0.0)),
                            "last_updated": updated_dt.isoformat(),
                            "wallet_name": w["name"],
                            "pnl_after_fees_usd": float(item.get("pnlAfterFeesUsd", 0.0)),
                            "current_travel_percent": travel_percent
                        }
                        new_positions.append(pos_dict)
                    except Exception as map_err:
                        logger.warning(f"Skipping item for wallet {w['name']} due to mapping error: {map_err}")

            new_count = 0
            duplicate_count = 0
            for p in new_positions:
                cursor = dl.conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM positions WHERE id = ?", (p["id"],))
                dup_count = cursor.fetchone()
                cursor.close()
                if dup_count[0] == 0:
                    dl.create_position(p)
                    new_count += 1
                else:
                    duplicate_count += 1
                    logger.info(f"Skipping duplicate Jupiter position: {p['id']}")

            # Update balance variables
            all_positions = dl.get_positions()
            total_brokerage_value = sum(float(pos.get("value", 0)) for pos in all_positions)
            balance_vars = dl.get_balance_vars()
            old_wallet_balance = balance_vars.get("total_wallet_balance", 0.0)
            new_total_balance = old_wallet_balance + total_brokerage_value
            dl.set_balance_vars(
                brokerage_balance=total_brokerage_value,
                total_balance=new_total_balance
            )
            msg = (f"Imported {new_count} new Jupiter position(s); Skipped {duplicate_count} duplicate(s). "
                   f"BrokerageBalance={total_brokerage_value:.2f}, TotalBalance={new_total_balance:.2f}")
            logger.info(msg)
            return {"message": msg, "imported": new_count, "skipped": duplicate_count}
        except Exception as e:
            logger.error(f"Error in update_jupiter_positions: {e}", exc_info=True)
            return {"error": str(e)}

    @staticmethod
    def delete_all_jupiter_positions(db_path: str = DB_PATH):
        """
        Delete all Jupiter positions from the database.
        """
        try:
            dl = DataLocker.get_instance(db_path)
            dl.cursor.execute("DELETE FROM positions WHERE wallet_name IS NOT NULL")
            dl.conn.commit()
            logger.info("All Jupiter positions deleted.")
        except Exception as e:
            logger.error(f"Error deleting Jupiter positions: {e}", exc_info=True)
            raise

    @staticmethod
    def record_positions_snapshot(db_path: str = DB_PATH):
        """
        Retrieve all enriched positions, calculate aggregated totals, and store a snapshot
        in the positions_totals_history table.
        """
        try:
            positions = PositionService.get_all_positions(db_path)
            calc_services = CalcServices()
            totals = calc_services.calculate_totals(positions)
            dl = DataLocker.get_instance(db_path)
            dl.record_positions_totals_snapshot(totals)
            logger.info("Positions snapshot recorded.")
        except Exception as e:
            logger.error(f"Error recording positions snapshot: {e}", exc_info=True)
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
