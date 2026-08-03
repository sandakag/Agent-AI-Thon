"""Extract stage — pull live trades from the Coinbase public API.

Coinbase Exchange exposes recent trades per product with **no API key**::

    GET {base}/products/{product}/trades?limit=N

Each trade is a real transaction: ``{trade_id, side, size, price, time}``.
We tag every record with its ``product`` (the market / region dimension) and
hand the raw batch downstream to the ETL. A source outage is returned as a
sentinel record (``_source_error``) instead of raising — an unreachable
upstream is itself a health signal the agent should see, not a crash.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import config


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_trades(product: str, limit: int | None = None) -> list[dict]:
    """Return recent trades for one product as normalised raw records."""
    limit = limit or config.TRADES_PER_PRODUCT
    url = f"{config.COINBASE_BASE}/products/{product}/trades?limit={limit}"
    try:
        rows = _get(url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        return [{"product": product, "_source_error": str(exc)}]

    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "product": product,
                "trade_id": r.get("trade_id"),
                "price": r.get("price"),
                "size": r.get("size"),
                "side": r.get("side"),
                "time": r.get("time"),
            }
        )
    return out


def fetch_batch(products: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """Fetch trades across all configured products into one raw batch."""
    products = products or config.PRODUCTS
    batch: list[dict] = []
    for p in products:
        batch.extend(fetch_trades(p.strip(), limit))
    return batch
