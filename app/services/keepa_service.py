"""
Keepa API service — Amazon price history + title lookup.

Keepa timestamps are minutes since Jan 1 2011 00:00 UTC:
    keepa_minutes = unix_timestamp_seconds / 60  -  21_564_000

Price is in cents (integer); -1 means "not available".

We use csv[1] = NEW (3rd-party new marketplace price) exclusively.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

_API_URL = "https://api.keepa.com/product"
_KEEPA_EPOCH_OFFSET = 21_564_000


class KeepaError(Exception):
    pass


@dataclass
class KeepaResult:
    title: Optional[str]       # product title
    amazon_price: Optional[float]  # raw NEW price in USD at delivery date
    cost: Optional[int]        # amazon_price × 0.45, rounded to whole dollar


def _unix_to_keepa(dt: datetime) -> int:
    """
    Convert datetime to Keepa minutes.
    All datetimes stored in DB are naive UTC (converted at parse time in housecargo_service).
    """
    ts = dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
    return int(ts / 60) - _KEEPA_EPOCH_OFFSET


def _price_at(csv_pairs: list[int], keepa_time: int) -> Optional[float]:
    """
    Return last non-(-1) price (USD) at or before keepa_time.
    Skips OOS entries (price == -1), so the last known price is returned
    even if the item went out of stock before the delivery date.
    """
    best = None
    for i in range(0, len(csv_pairs) - 1, 2):
        t, p = csv_pairs[i], csv_pairs[i + 1]
        if t <= keepa_time:
            if p != -1:
                best = p / 100.0
        else:
            break
    return best


def _fetch_product(asin: str) -> dict:
    api_key = os.getenv("KEEPA_API_KEY", "")
    if not api_key:
        raise KeepaError("KEEPA_API_KEY is not set in .env")

    params = {
        "key": api_key,
        "domain": 1,
        "asin": asin,
        "history": 1,
        "days": 730,
    }
    try:
        r = requests.get(_API_URL, params=params, timeout=20)
    except requests.RequestException as exc:
        raise KeepaError(f"Keepa request failed: {exc}") from exc

    if r.status_code != 200:
        raise KeepaError(f"Keepa API HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    if data.get("error"):
        raise KeepaError(f"Keepa API error: {data['error'].get('message', data['error'])}")

    products = data.get("products") or []
    if not products:
        raise KeepaError(f"ASIN {asin} not found in Keepa")

    return products[0]


def get_product_info(asin: str, delivery_dt: datetime, multiplier: float = 0.45) -> KeepaResult:
    """
    Fetch product title and NEW price at delivery_dt.
    Returns KeepaResult with title, amazon_price, and cost (rounded to int).
    """
    product = _fetch_product(asin)

    # Title
    title = None
    title_raw = product.get("title")
    if title_raw:
        title = str(title_raw).strip()[:500]

    # Price
    csv = product.get("csv") or []
    new_csv = csv[1] if len(csv) > 1 else None
    amazon_price = None
    cost = None

    if new_csv:
        keepa_time = _unix_to_keepa(delivery_dt)
        amazon_price = _price_at(new_csv, keepa_time)
        if amazon_price is not None:
            cost = round(amazon_price * multiplier)  # whole dollar

    return KeepaResult(title=title, amazon_price=amazon_price, cost=cost)


def get_title_only(asin: str) -> Optional[str]:
    """Fetch only the product title (no price history needed)."""
    try:
        product = _fetch_product(asin)
        t = product.get("title")
        return str(t).strip()[:500] if t else None
    except KeepaError:
        return None


# Keep backward-compat alias used in parcels router
def calculate_cost(asin: str, delivery_dt: datetime, multiplier: float = 0.45) -> Optional[int]:
    """Returns cost as whole integer, or None."""
    result = get_product_info(asin, delivery_dt, multiplier)
    return result.cost
