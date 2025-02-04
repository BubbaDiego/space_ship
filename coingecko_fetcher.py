# price_sources/coingecko_fetcher.py
import aiohttp
import logging
from typing import Dict, List

logger = logging.getLogger("CoinGeckoFetcher")

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

async def fetch_current_coingecko(symbols: List[str], currency: str = "USD") -> Dict[str, float]:
    """
    Fetch current prices from CoinGecko for given symbols in `currency`.
    `symbols` is a list of coin "slugs" recognized by CoinGecko 
    (e.g. ["bitcoin", "ethereum"]).
    Return a dict: { "BTC": 12345.67, "ETH": 2345.67, ... } if possible.
    """
    # Convert slugs to a comma-separated string
    joined_slugs = ",".join(symbols)
    url = f"{COINGECKO_BASE_URL}/simple/price?ids={joined_slugs}&vs_currencies={currency}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"CoinGecko fetch failed: status {resp.status}")
                    return {}
                data = await resp.json()
                logger.debug(f"CoinGecko response: {data}")

                # data is a dict of slug -> { currency: price }
                # e.g. { "bitcoin": {"usd": 12345.67}, "ethereum": {"usd": 2345.67} }
                result = {}
                for slug in symbols:
                    slug_data = data.get(slug)
                    if not slug_data:
                        continue
                    price_val = slug_data.get(currency.lower())
                    if price_val is not None:
                        # For convenience, let's uppercase the slug
                        # or map it to a user-defined symbol. E.g. "bitcoin" -> "BTC"
                        # If you track that in a map, do so here. Otherwise, just store slug as key.
                        result[slug.upper()] = float(price_val)
                return result
    except Exception as e:
        logger.error(f"Error fetching from CoinGecko: {e}", exc_info=True)
        return {}
