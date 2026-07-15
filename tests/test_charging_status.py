"""Tests for the derived charging-status state machine.

The battery gRPC status cannot tell "plugged but idle" apart from "charging"
(chargingStatus #17 reads 2 in both cases). The authoritative signal is the
home wallbox connectorStatus (2 = 已插枪/plugged-idle, 3 = 充电中/charging),
which the Volvo app itself uses for display. `_derive_charging_status`
reconciles both sources.
"""

from custom_components.volvooncall_cn.volvooncall_cn import _derive_charging_status


def test_home_pile_plugged_but_idle_is_not_charging():
    # The reported bug: XC90 plugged into home pile at 35%, not charging,
    # connectorStatus == 2 (已插枪). Must NOT report "charging".
    assert (
        _derive_charging_status(
            has_battery=True,
            charge=35,
            charger_connected=True,
            home_pile_connector_status=2,
        )
        == "connected"
    )


def test_home_pile_actively_charging():
    assert (
        _derive_charging_status(
            has_battery=True,
            charge=35,
            charger_connected=True,
            home_pile_connector_status=3,
        )
        == "charging"
    )


def test_home_pile_full_is_done():
    assert (
        _derive_charging_status(
            has_battery=True,
            charge=100,
            charger_connected=True,
            home_pile_connector_status=2,
        )
        == "done"
    )


def test_no_home_pile_falls_back_to_battery_connection():
    # Public / non-home-pile charging: no wallbox info, battery says plugged.
    assert (
        _derive_charging_status(
            has_battery=True,
            charge=50,
            charger_connected=True,
            home_pile_connector_status=None,
        )
        == "charging"
    )


def test_disconnected_when_nothing_plugged():
    assert (
        _derive_charging_status(
            has_battery=True,
            charge=50,
            charger_connected=False,
            home_pile_connector_status=None,
        )
        == "disconnected"
    )


def test_home_pile_status_overrides_stale_battery_flag():
    # Home pile says idle (2) even though the battery connection flag is set.
    assert (
        _derive_charging_status(
            has_battery=True,
            charge=80,
            charger_connected=True,
            home_pile_connector_status=2,
        )
        == "connected"
    )


def test_non_phev_has_no_charging_status():
    assert (
        _derive_charging_status(
            has_battery=False,
            charge=None,
            charger_connected=False,
            home_pile_connector_status=None,
        )
        is None
    )
