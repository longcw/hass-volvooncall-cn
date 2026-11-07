"""Provincial retail fuel prices, read from autohome.com.cn's oil price pages."""

import json
import logging
import re
from datetime import date

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

# Province name -> administrative code, which is also the page id on autohome.
PROVINCES = {
    "北京": 110000, "天津": 120000, "河北": 130000, "山西": 140000,
    "内蒙古": 150000, "辽宁": 210000, "吉林": 220000, "黑龙江": 230000,
    "上海": 310000, "江苏": 320000, "浙江": 330000, "安徽": 340000,
    "福建": 350000, "江西": 360000, "山东": 370000, "河南": 410000,
    "湖北": 420000, "湖南": 430000, "广东": 440000, "广西": 450000,
    "海南": 460000, "重庆": 500000, "四川": 510000, "贵州": 520000,
    "云南": 530000, "西藏": 540000, "陕西": 610000, "甘肃": 620000,
    "青海": 630000, "宁夏": 640000, "新疆": 650000,
}
GRADES = ("92", "95", "98")
DEFAULT_GRADE = "95"

# config entry option keys
CONF_FUEL_PRICE_PROVINCE = "fuel_price_province"
CONF_FUEL_GRADE = "fuel_grade"

_URL = "https://www.autohome.com.cn/oil/{code}.html"
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def parse_price_history(html: str, province_code: int, grade: str) -> dict:
    """``{"YYYY-MM-DD": price}`` for every adjustment the page lists.

    Any object in the page's Next.js JSON with this province's id, a date and a price counts, so renamed keys don't break it."""
    match = _NEXT_DATA.search(html)
    if not match:
        return {}
    try:
        tree = json.loads(match.group(1))
    except ValueError:
        return {}

    history = {}
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            price = node.get(f"oilPrice{grade}")
            day = node.get("dateTime")
            if (
                node.get("provinceId") == province_code
                and isinstance(day, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
                and isinstance(price, (int, float))
                and price > 0
            ):
                history[day] = float(price)
            stack.extend(node.values())
    return history


def price_on(history: dict, day: date):
    """The price in force on ``day``: the latest adjustment on or before it."""
    key = day.isoformat()
    past = [d for d in history if d <= key]
    return history[max(past)] if past else None


async def async_fetch_price_history(
    session: ClientSession, province: str, grade: str
) -> dict:
    """Fetch and parse the province's price history; empty on any failure."""
    code = PROVINCES.get(province)
    if code is None or grade not in GRADES:
        return {}
    try:
        async with session.get(
            _URL.format(code=code),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            html = await response.text()
    except Exception as err:
        _LOGGER.warning("Fuel price lookup for %s failed: %s", province, err)
        return {}
    history = parse_price_history(html, code, grade)
    if not history:
        _LOGGER.warning("Fuel price page for %s had no %s# prices", province, grade)
    return history
