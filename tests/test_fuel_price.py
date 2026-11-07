"""Tests for the provincial fuel price lookup (fuel_price.py)."""

import json
from datetime import date

from custom_components.volvooncall_cn.fuel_price import parse_price_history, price_on


def _page(data) -> str:
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data, ensure_ascii=False)}</script></html>"
    )


def test_history_is_read_for_the_province_only():
    row = lambda pid, day, p95: {"provinceId": pid, "dateTime": day, "oilPrice95": p95}
    html = _page({"props": {"history": [
        row(430000, "2026-11-02", 9.38),
        row(430000, "2026-09-25", 9.09),
        row(440000, "2026-11-07", 9.65),  # another province's table
    ]}})

    history = parse_price_history(html, 430000, "95")
    assert history == {"2026-11-02": 9.38, "2026-09-25": 9.09}
    assert parse_price_history("<html></html>", 430000, "95") == {}

    assert price_on(history, date(2026, 11, 6)) == 9.38
    assert price_on(history, date(2026, 10, 1)) == 9.09
    assert price_on(history, date(2026, 9, 1)) is None
