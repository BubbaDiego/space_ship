import asyncio
import logging
from typing import Dict, Optional, List

from config_manager import load_config
from data_locker import DataLocker
from coingecko_fetcher import fetch_current_coingecko
from coinmarketcap_fetcher import fetch_current_cmc, fetch_historical_cmc
from coinpaprika_fetcher import fetch_current_coinpaprika
from binance_fetcher import fetch_current_binance

logger = logging.getLogger("PriceMonitorLogger")

class PriceMonitor:
    def __init__(
        self,
        db_path="data/mother_brain.db",
        config_path="sonic_config.json",
    ):
        self.db_path = db_path
        self.config_path = config_path

        # 1) Setup data locker & DB
        self.data_locker = DataLocker(self.db_path)
        self.db_conn = self.data_locker.get_db_connection()

        # 2) Load final config as a pure dict
        self.config = load_config(self.config_path, self.db_conn)

        # read config for coinpaprika/binance
        api_cfg = self.config.get("api_config", {})
        self.coinpaprika_enabled = (api_cfg.get("coinpaprika_api_enabled") == "ENABLE")
        self.binance_enabled = (api_cfg.get("binance_api_enabled") == "ENABLE")

        # 4) Parse relevant fields from config
        price_cfg = self.config.get("price_config", {})
        self.assets = price_cfg.get("assets", ["BTC", "ETH"])
        self.currency = price_cfg.get("currency", "USD")
        self.cmc_api_key = price_cfg.get("cmc_api_key")

        self.coingecko_enabled = (api_cfg.get("coingecko_api_enabled") == "ENABLE")
        self.cmc_enabled = (api_cfg.get("coinmarketcap_api_enabled") == "ENABLE")


    async def initialize_monitor(self):
        logger.info("PriceMonitor initialized with dictionary config.")


    async def update_prices(self):
        """
        Fetches prices from enabled APIs in parallel,
        then averages them for each symbol, storing only one row per symbol.
        """
        logger.info("Starting update_prices...")

        tasks = []
        if self.coingecko_enabled:
            tasks.append(self._fetch_coingecko_prices())
        if self.cmc_enabled:
            tasks.append(self._fetch_cmc_prices())
        if self.coinpaprika_enabled:
            tasks.append(self._fetch_coinpaprika_prices())
        if self.binance_enabled:
            tasks.append(self._fetch_binance_prices())

        if not tasks:
            logger.warning("No API sources enabled for update_prices.")
            return

        # results_list: each item is a dict like {"BTC": 29000, "ETH": 1900}
        results_list = await asyncio.gather(*tasks)

        # Combine them into { "BTC": [p1, p2, ...], "ETH": [p3, p4, ...] }
        aggregated = {}
        for result_dict in results_list:
            for sym, price_val in result_dict.items():
                aggregated.setdefault(sym, []).append(price_val)

        # Now compute the average per symbol & insert
        for sym, price_list in aggregated.items():
            if not price_list:
                continue
            avg_price = sum(price_list) / len(price_list)
            # We'll label it "Averaged" as the source
            self.data_locker.insert_or_update_price(sym, avg_price, "Averaged")

        logger.info("All price updates completed.")


    async def _fetch_coingecko_prices(self) -> Dict[str, float]:
        """
        Grab prices from Coingecko for the assets in self.assets, return as dict (no DB insert).
        """
        slug_map = {
            "BTC": "bitcoin",
            "ETH": "ethereum",
        }
        slugs = []
        for sym in self.assets:
            if sym.upper() in slug_map:
                slugs.append(slug_map[sym.upper()])
            else:
                logger.warning(f"No slug found for {sym}, skipping.")
        if not slugs:
            return {}

        logger.info("Fetching Coingecko for assets: %s", slugs)
        cg_data = await fetch_current_coingecko(slugs, self.currency)
        # cg_data e.g. {"bitcoin": 29100, "ethereum": 1900}

        # Convert slug -> symbol
        results = {}
        for slug, price in cg_data.items():
            found_sym = None
            for s, slugval in slug_map.items():
                if slugval.lower() == slug.lower():
                    found_sym = s
                    break
            sym = found_sym if found_sym else slug  # fallback
            results[sym] = price

        self.data_locker.increment_api_report_counter("CoinGecko")
        return results


    async def _fetch_coinpaprika_prices(self) -> Dict[str, float]:
        """
        Grab prices from CoinPaprika, return as dict.
        """
        logger.info("Fetching CoinPaprika for assets: ...")
        paprika_map = {
            "BTC": "btc-bitcoin",
            "ETH": "eth-ethereum",
            "SOL": "sol-solana",
        }
        ids = []
        for sym in self.assets:
            if sym.upper() in paprika_map:
                ids.append(paprika_map[sym.upper()])
            else:
                logger.warning(f"No paprika ID found for {sym}, skipping.")
        if not ids:
            return {}

        data = await fetch_current_coinpaprika(ids)  # e.g. {"BTC": 29050, "ETH": 1905}
        self.data_locker.increment_api_report_counter("CoinPaprika")
        return data


    async def _fetch_binance_prices(self) -> Dict[str, float]:
        """
        Grab prices from Binance, return as dict.
        """
        logger.info("Fetching Binance for assets: ...")
        binance_symbols = [sym.upper() + "USDT" for sym in self.assets]
        bn_data = await fetch_current_binance(binance_symbols)  # e.g. {"BTC": 29040, "ETH": 1898}
        self.data_locker.increment_api_report_counter("Binance")
        return bn_data


    async def _fetch_cmc_prices(self) -> Dict[str, float]:
        """
        Grab prices from CoinMarketCap, return as dict.
        """
        logger.info("Fetching CMC for assets: %s", self.assets)
        cmc_data = await fetch_current_cmc(self.assets, self.currency, self.cmc_api_key)
        # e.g. {"BTC": 29045, "ETH": 1899}
        self.data_locker.increment_api_report_counter("CoinMarketCap")
        return cmc_data


    async def update_historical_cmc(self, symbol: str, start_date: str, end_date: str):
        if not self.cmc_enabled:
            logger.warning("CoinMarketCap is not enabled, skipping historical fetch.")
            return
        logger.info(
            f"Fetching historical CMC for {symbol} from {start_date} to {end_date}..."
        )

        records = await fetch_historical_cmc(
            symbol, start_date, end_date, self.currency, self.cmc_api_key
        )
        logger.debug(f"Fetched {len(records)} daily records for {symbol} from CMC.")

        for r in records:
            self.data_locker.insert_historical_ohlc(
                symbol,
                r["time_open"],
                r["open"],
                r["high"],
                r["low"],
                r["close"],
                r["volume"],
            )


# Standalone usage
if __name__ == "__main__":
    async def main():
        pm = PriceMonitor(
            db_path="mother_brain.db",
            config_path="sonic_config.json",
        )
        await pm.initialize_monitor()
        await pm.update_prices()

        # Example of historical usage:
        start_date = "2024-12-01"
        end_date = "2025-01-19"
        # await pm.update_historical_cmc("BTC", start_date, end_date)

    asyncio.run(main())
