"""Tests for the refuel log and tank-to-tank consumption (store.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volvooncall_cn.store import VolvoStore

BASE = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)


async def _store(hass, name):
    store = VolvoStore(hass, name)
    await store.load_create_data()
    return store


@pytest.mark.asyncio
async def test_first_reading_only_sets_the_baseline(hass):
    """Nothing to compare against yet, so the first poll can't be a fill-up."""
    store = await _store(hass, "REFUEL_BASELINE")

    assert await store.record_fuel_amount(30.0, 1000.0, BASE) is False
    assert store.get("fuel_amount_last") == 30.0
    assert store.get_refuel_stats()["count"] == 0


@pytest.mark.asyncio
async def test_jump_creates_a_record_and_drift_does_not(hass):
    store = await _store(hass, "REFUEL_JUMP")
    await store.record_fuel_amount(20.0, 1000.0, BASE)

    # Gauge noise (slosh on a slope) must not look like a fill-up.
    assert await store.record_fuel_amount(23.5, 1010.0, BASE + timedelta(hours=2)) is False
    assert store.get_refuel_stats()["count"] == 0
    assert store.get("fuel_amount_last") == 23.5

    # Burning fuel keeps the baseline moving down.
    assert await store.record_fuel_amount(12.0, 1200.0, BASE + timedelta(days=1)) is False
    assert store.get("fuel_amount_last") == 12.0

    # +59 L is a fill-up.
    assert await store.record_fuel_amount(71.0, 1300.0, BASE + timedelta(days=2)) is True
    stats = store.get_refuel_stats()
    assert stats["count"] == 1
    record = stats["records"][0]
    assert record["liters"] == 59.0
    assert record["odometer"] == 1300.0
    assert record["fuel_before"] == 12.0
    assert record["fuel_after"] == 71.0
    assert record["source"] == "auto"
    assert record["edited"] is False
    # No predecessor yet, so no consumption figure.
    assert record["consumption"] is None
    assert stats["last"] is None


@pytest.mark.asyncio
async def test_fill_across_two_polls_merges_into_one_record(hass):
    """Fuelling can straddle a poll; that is one fill, not two."""
    store = await _store(hass, "REFUEL_MERGE")
    await store.record_fuel_amount(10.0, 5000.0, BASE)

    assert await store.record_fuel_amount(40.0, 5000.0, BASE + timedelta(minutes=5)) is True
    assert await store.record_fuel_amount(71.0, 5000.4, BASE + timedelta(minutes=9)) is True

    stats = store.get_refuel_stats()
    assert stats["count"] == 1
    record = stats["records"][0]
    assert record["liters"] == 61.0
    assert record["fuel_after"] == 71.0

    # An hour later and 40 km down the road it is a separate fill.
    await store.record_fuel_amount(45.0, 5040.0, BASE + timedelta(minutes=90))
    assert await store.record_fuel_amount(70.0, 5040.0, BASE + timedelta(minutes=95)) is True
    assert store.get_refuel_stats()["count"] == 2


@pytest.mark.asyncio
async def test_consumption_is_litres_of_this_fill_over_distance_of_last_tank(hass):
    store = await _store(hass, "REFUEL_CONSUMPTION")
    await store.record_fuel_amount(15.0, 9000.0, BASE)
    await store.record_fuel_amount(71.0, 9000.0, BASE + timedelta(minutes=1))
    # 500 km later, 40 L back in: 8.0 L/100km.
    await store.record_fuel_amount(31.0, 9500.0, BASE + timedelta(days=7))
    await store.record_fuel_amount(71.0, 9500.0, BASE + timedelta(days=7, minutes=1))

    stats = store.get_refuel_stats()
    assert stats["count"] == 2
    newest, oldest = stats["records"]
    assert newest["distance"] == 500.0
    assert newest["consumption"] == 8.0
    assert oldest["consumption"] is None
    assert stats["last"] == 8.0
    assert stats["average"] == 8.0


@pytest.mark.asyncio
async def test_correcting_litres_reprices_the_tank_and_survives_polling(hass):
    """The pump charged for 40.82 L; the tank sensor guessed 40.0."""
    store = await _store(hass, "REFUEL_EDIT")
    await store.record_fuel_amount(15.0, 9000.0, BASE)
    await store.record_fuel_amount(71.0, 9000.0, BASE + timedelta(minutes=1))
    await store.record_fuel_amount(31.0, 9500.0, BASE + timedelta(days=7))
    await store.record_fuel_amount(71.0, 9500.0, BASE + timedelta(days=7, minutes=1))

    newest = store.get_refuel_stats()["records"][0]
    assert newest["liters"] == 40.0

    assert await store.update_refuel(newest["id"], liters=40.82) is True
    corrected = store.get_refuel_stats()["records"][0]
    assert corrected["liters"] == 40.82
    assert corrected["liters_sensor"] == 40.0
    assert corrected["edited"] is True
    assert corrected["consumption"] == 8.2  # 40.82 / 500 km

    # A later poll during the same window must not re-merge into (and undo)
    # a hand-corrected record.
    await store.record_fuel_amount(71.0, 9500.0, BASE + timedelta(days=7, minutes=2))
    assert store.get_refuel_stats()["records"][0]["liters"] == 40.82

    reloaded = await _store(hass, "REFUEL_EDIT")
    assert reloaded.get_refuel_stats()["records"][0]["liters"] == 40.82

    assert await store.update_refuel("no-such-record", liters=10) is False
    assert await store.update_refuel(newest["id"], liters=0) is False


@pytest.mark.asyncio
async def test_correcting_the_odometer_reprices_the_tank(hass):
    """The fill was logged on the next poll, 30 km down the road."""
    store = await _store(hass, "REFUEL_EDIT_ODO")
    await store.add_refuel(40.0, 9000.0, BASE)
    await store.add_refuel(40.0, 9530.0, BASE + timedelta(days=7))

    newest = store.get_refuel_stats()["records"][0]
    assert newest["consumption"] == 7.5  # 40 L / 530 km

    assert await store.update_refuel(newest["id"], odometer=9500.0) is True
    corrected = store.get_refuel_stats()["records"][0]
    assert corrected["odometer"] == 9500.0
    assert corrected["distance"] == 500.0
    assert corrected["consumption"] == 8.0
    assert await store.update_refuel(newest["id"], odometer=-5) is False


@pytest.mark.asyncio
async def test_correcting_the_date_reorders_the_log(hass):
    """Records are ordered by date, so moving one moves what it measures."""
    store = await _store(hass, "REFUEL_EDIT_DATE")
    await store.add_refuel(40.0, 9000.0, BASE)
    middle = await store.add_refuel(30.0, 9300.0, BASE + timedelta(days=4))
    await store.add_refuel(40.0, 9500.0, BASE + timedelta(days=7))

    assert [r["odometer"] for r in store.get_refuel_stats()["records"]] == [
        9500.0, 9300.0, 9000.0,
    ]

    # It actually happened before the first one, at 8900 km.
    assert (
        await store.update_refuel(
            middle["id"], odometer=8900.0, when=BASE - timedelta(days=3)
        )
        is True
    )
    stats = store.get_refuel_stats()
    assert [r["odometer"] for r in stats["records"]] == [9500.0, 9000.0, 8900.0]
    # The id survives the move, so the card's edit button still resolves.
    assert any(r["id"] == middle["id"] for r in stats["records"])
    # 40 L over the 100 km from 8900 to 9000.
    assert stats["records"][1]["consumption"] == 40.0
    assert stats["records"][0]["consumption"] == 8.0

    # Nothing to change is not an update.
    assert await store.update_refuel(middle["id"]) is False


@pytest.mark.asyncio
async def test_manual_records_and_deletion(hass):
    store = await _store(hass, "REFUEL_MANUAL")
    first = await store.add_refuel(50.0, 20000.0, BASE)
    assert first is not None
    second = await store.add_refuel(45.5, 20500.0, BASE + timedelta(days=5))

    stats = store.get_refuel_stats()
    assert stats["count"] == 2
    assert stats["records"][0]["source"] == "manual"
    assert stats["last"] == 9.1  # 45.5 L / 500 km

    assert await store.add_refuel(0, 20000.0, BASE) is None
    assert await store.delete_refuel(second["id"]) is True
    assert store.get_refuel_stats()["count"] == 1
    assert await store.delete_refuel(second["id"]) is False


@pytest.mark.asyncio
async def test_implausible_segments_are_left_blank(hass):
    """A short hop or a nonsense figure publishes nothing rather than garbage."""
    store = await _store(hass, "REFUEL_BOUNDS")
    await store.add_refuel(40.0, 1000.0, BASE)
    # 5 km later — too short to divide by.
    await store.add_refuel(40.0, 1005.0, BASE + timedelta(hours=1))
    stats = store.get_refuel_stats()
    assert stats["records"][0]["distance"] == 5.0
    assert stats["records"][0]["consumption"] is None
    assert stats["last"] is None

    # 60 L over 100 km is 60 L/100km — outside any plausible range.
    await store.add_refuel(60.0, 1105.0, BASE + timedelta(hours=4))
    assert store.get_refuel_stats()["records"][0]["consumption"] is None
    assert store.get_refuel_stats()["average"] is None


@pytest.mark.asyncio
async def test_failed_poll_does_not_look_like_a_fill_up(hass):
    """A 0 L reading from a failed fetch must not reset the baseline."""
    store = await _store(hass, "REFUEL_FAILED_POLL")
    await store.record_fuel_amount(45.0, 3000.0, BASE)

    assert await store.record_fuel_amount(0, 3000.0, BASE + timedelta(hours=1)) is False
    assert await store.record_fuel_amount(None, 3000.0, BASE + timedelta(hours=2)) is False
    assert store.get("fuel_amount_last") == 45.0

    assert await store.record_fuel_amount(44.5, 3050.0, BASE + timedelta(hours=3)) is False
    assert store.get_refuel_stats()["count"] == 0


@pytest.mark.asyncio
async def test_log_is_capped(hass):
    store = await _store(hass, "REFUEL_CAP")
    for index in range(60):
        await store.add_refuel(40.0, 1000.0 + index * 500, BASE + timedelta(days=index))

    stats = store.get_refuel_stats()
    assert stats["count"] == 50
    # The oldest were dropped, newest kept.
    assert stats["records"][0]["odometer"] == 1000.0 + 59 * 500
