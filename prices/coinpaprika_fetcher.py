import aiohttp
import logging
from typing import Dict, List

logger = logging.getLogger("CoinPaprikaFetcher")

COINPAPRIKA_BASE_URL = "https://api.coinpaprika.com/v1"

async def fetch_current_coinpaprika(ids: List[str]) -> Dict[str, float]:
    """
    Fetch the latest price in USD for each coin ID from CoinPaprika.
    'ids' should be a list like ["btc-bitcoin", "eth-ethereum"].
    
    Returns a dict: { "BTC": 12345.67, "ETH": 2345.67, ... }
    
    Steps:
      1) For each coin ID, call GET /v1/tickers/<coin_id>.
      2) Parse 'quotes.USD.price' from the response.
      3) Use 'symbol' from the response or your own mapping as dict key.
    """
    result = {}

    # We'll do a straightforward sequential approach. For large usage,
    # you might parallelize each request with asyncio.gather.
    try:
        async with aiohttp.ClientSession() as session:
            for coin_id in ids:
                url = f"{COINPAPRIKA_BASE_URL}/tickers/{coin_id}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error(f"CoinPaprika fetch failed for {coin_id}: status {resp.status}")
                        continue
                    data = await resp.json()
                    logger.debug(f"CoinPaprika response for {coin_id}: {data}")

                    # Example data:
                    # {
                    #   "id": "btc-bitcoin",
                    #   "symbol": "BTC",
                    #   "name": "Bitcoin",
                    #   "rank": 1,
                    #   "circulating_supply": 19000000,
                    #   "total_supply": 21000000,
                    #   "max_supply": 21000000,
                    #   "beta_value": 1.03,
                    #   "first_data_at": "...",
                    #   "last_updated": "...",
                    #   "quotes": {
                    #       "USD": {
                    #           "price": 12345.67,
                    #           ...
                    #       }
                    #   }
                    # }
                    symbol = data.get("symbol", coin_id.upper())
                    quotes = data.get("quotes", {})
                    usd_data = quotes.get("USD", {})
                    price_val = usd_data.get("price")

                    if price_val is not None:
                        result[symbol.upper()] = float(price_val)

    except Exception as e:
        logger.error(f"Error fetching from CoinPaprika: {e}", exc_info=True)

    return result
