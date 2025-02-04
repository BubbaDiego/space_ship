import aiohttp
import logging
from typing import Dict, List

logger = logging.getLogger("BinanceFetcher")

BINANCE_BASE_URL = "https://api.binance.com"

async def fetch_current_binance(symbols: List[str]) -> Dict[str, float]:
    """
    Fetch the latest spot prices from Binance for each symbol in 'symbols'.
    Each symbol is typically in the form "BTCUSDT", "ETHUSDT", etc.
    
    Returns a dict: { "BTC": 12345.67, "ETH": 2345.67, ... } by stripping "USDT".
    """
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            for sym in symbols:
                url = f"{BINANCE_BASE_URL}/api/v3/ticker/price?symbol={sym}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error(f"Binance fetch failed for {sym}: status {resp.status}")
                        continue
                    data = await resp.json()
                    logger.debug(f"Binance response for {sym}: {data}")
                    
                    # data looks like: { "symbol": "BTCUSDT", "price": "12345.67" }
                    price_str = data.get("price")
                    if price_str is not None:
                        # We'll map "BTCUSDT" -> "BTC"
                        # If you use BUSD or something else, adapt the slicing
                        base_coin = sym.replace("USDT", "")  # naive approach
                        result[base_coin.upper()] = float(price_str)
    except Exception as e:
        logger.error(f"Error fetching from Binance: {e}", exc_info=True)

    return result
