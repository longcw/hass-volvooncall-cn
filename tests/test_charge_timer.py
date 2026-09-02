"""Tests for the daily timed home charge (_apply_charge_timer in __init__.py)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.volvooncall_cn import CHARGE_TIMER_GRACE, VolvoCoordinator
from custom_components.volvooncall_cn.store import VolvoStore

TIMER_START = "23:00"


def _at(hour, minute=0):
    """Aware local datetime at today's given clock time."""
    return dt_util.start_of_local_day() + timedelta(hours=hour, minutes=minute)


def _vehicle(level=60, plugged=True, car_plugged=True, charging=False):
    return SimpleNamespace(
        vin="YV1TEST0000000000",
        has_home_pile=True,
        home_pile_plugged=plugged,
        charger_connected=car_plugged,
        home_pile_charging=charging,
        battery_charge_level=level,
        home_pile_charge_start=AsyncMock(),
        home_pile_charge_stop=AsyncMock(),
    )


async def _store(hass, name, **fields):
    store = VolvoStore(hass, name)
    await store.load_create_data()
    await store.update(
        **{
            "charge_timer_enabled": True,
            "charge_timer_start": TIMER_START,
            "charge_limit": 90,
            **fields,
        }
    )
    return store


@pytest.fixture
def coordinator(hass):
    return VolvoCoordinator(hass, MagicMock(), 30)


@pytest.mark.asyncio
async def test_starts_a_charge_below_the_limit(hass, coordinator):
    vehicle = _vehicle(level=60)
    store = await _store(hass, "TIMER_START")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))

    vehicle.home_pile_charge_start.assert_awaited_once()
    vehicle.home_pile_charge_stop.assert_not_awaited()
    assert store.get("charge_timer_last_run") == _at(23).date().isoformat()


@pytest.mark.asyncio
async def test_deadband_skips_a_pointless_top_up(hass, coordinator):
    """86% under a 90% limit is not worth a session; 85% is."""
    vehicle = _vehicle(level=86)
    store = await _store(hass, "TIMER_DEADBAND")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))

    vehicle.home_pile_charge_start.assert_not_awaited()
    # The day is spent: the timer decided, it did not fail to decide.
    assert store.get("charge_timer_last_run") == _at(23).date().isoformat()

    edge = _vehicle(level=85)
    await coordinator._apply_charge_timer(
        edge, await _store(hass, "TIMER_DEADBAND_EDGE"), _at(23)
    )
    edge.home_pile_charge_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_at_the_limit_leaves_a_plugged_in_car_alone(hass, coordinator):
    """The 90%-of-90% case: nothing happens, and the timer re-arms for tomorrow."""
    vehicle = _vehicle(level=90)
    store = await _store(hass, "TIMER_AT_LIMIT")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))
    vehicle.home_pile_charge_start.assert_not_awaited()

    # Same car, next day, driven down to 60%.
    vehicle.battery_charge_level = 60
    await coordinator._apply_charge_timer(vehicle, store, _at(23) + timedelta(days=1))
    vehicle.home_pile_charge_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_does_nothing_before_the_time_or_after_the_grace(hass, coordinator):
    vehicle = _vehicle()
    store = await _store(hass, "TIMER_WINDOW")

    await coordinator._apply_charge_timer(vehicle, store, _at(22, 59))
    await coordinator._apply_charge_timer(
        vehicle, store, _at(23) + CHARGE_TIMER_GRACE + timedelta(minutes=1)
    )

    vehicle.home_pile_charge_start.assert_not_awaited()
    assert store.get("charge_timer_last_run") is None


@pytest.mark.asyncio
async def test_fires_once_a_day(hass, coordinator):
    vehicle = _vehicle()
    store = await _store(hass, "TIMER_ONCE")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))
    await coordinator._apply_charge_timer(vehicle, store, _at(23, 5))

    vehicle.home_pile_charge_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_timer_does_nothing(hass, coordinator):
    vehicle = _vehicle()
    store = await _store(hass, "TIMER_OFF", charge_timer_enabled=False)

    await coordinator._apply_charge_timer(vehicle, store, _at(23))

    vehicle.home_pile_charge_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_car_itself_has_to_report_the_cable(hass, coordinator):
    """The wallbox belongs to the account, so it can be plugged into another car."""
    vehicle = _vehicle(plugged=True, car_plugged=False)
    store = await _store(hass, "TIMER_OTHER_CAR")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))
    vehicle.home_pile_charge_start.assert_not_awaited()
    assert store.get("charge_timer_last_run") is None

    await store.update(charge_timer_any_car=True)
    await coordinator._apply_charge_timer(vehicle, store, _at(23, 10))
    vehicle.home_pile_charge_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unplugged_car_keeps_its_slot_inside_the_grace(hass, coordinator):
    vehicle = _vehicle(plugged=False, car_plugged=False)
    store = await _store(hass, "TIMER_LATE_PLUG")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))
    assert store.get("charge_timer_last_run") is None

    vehicle.home_pile_plugged = True
    vehicle.charger_connected = True
    await coordinator._apply_charge_timer(vehicle, store, _at(23, 20))

    vehicle.home_pile_charge_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_charge_in_progress_is_left_alone(hass, coordinator):
    """Manual (or plug-and-charge) sessions are never touched, stop included."""
    vehicle = _vehicle(level=60, charging=True)
    store = await _store(hass, "TIMER_MANUAL")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))
    vehicle.home_pile_charge_start.assert_not_awaited()
    vehicle.home_pile_charge_stop.assert_not_awaited()

    # Stopped by hand a minute later: the timer must not start it again.
    vehicle.home_pile_charging = False
    await coordinator._apply_charge_timer(vehicle, store, _at(23, 1))
    vehicle.home_pile_charge_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_battery_level_decides_nothing(hass, coordinator):
    vehicle = _vehicle(level=None)
    store = await _store(hass, "TIMER_NO_LEVEL")

    await coordinator._apply_charge_timer(vehicle, store, _at(23))

    vehicle.home_pile_charge_start.assert_not_awaited()
    assert store.get("charge_timer_last_run") is None
