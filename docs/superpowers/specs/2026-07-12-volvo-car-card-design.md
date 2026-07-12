# Volvo Car Card + charging sensors — design

Port the custom Lovelace card (`volvo-car-card.js`) from the
[Annincikee fork](https://github.com/Annincikee/hass-volvooncall-cn) into this
fork, plus the three charging data points its energy section needs but this fork
does not yet produce. Deliver a test build on the user's live Home Assistant.

## Scope

- **In:** the self-contained frontend card (remapped to this fork's entity IDs),
  integration glue to serve + auto-register it, and three new sensors:
  `charging_power`, `estimated_charging_time`, `full_charge_electric_range`.
- **Out:** the fork's powertrain-type filter, parking-AC switch, and its
  conflicting `battery.proto` reinterpretation. This fork already has PHEV
  battery, A/C preconditioning, wallbox, and trip-computer support.

## Principle

Additive only. Existing entities, their IDs, and their parsing are left
untouched so the user's current dashboard and automations keep working. No
proto regeneration (see below).

## The card

Copied to `custom_components/volvooncall_cn/frontend/volvo-car-card.js`. Its
`ENTITY_DEFINITIONS` resolves `{platform}.{vin}_{suffix}`. Most suffixes already
match this fork. Remaps applied:

| Card key | This fork's entity |
|---|---|
| `charger_connection` | `binary_sensor.{vin}_charger_connected` |
| `tm_distance` | `sensor.{vin}_trip_meter_tm` |
| `tm_average_speed` | `sensor.{vin}_avg_speed_tm` |
| `ta_distance` | `sensor.{vin}_trip_meter_at` |
| `ta_average_speed` | `sensor.{vin}_avg_speed_at` |
| `ta_fuel_consumption` | `sensor.{vin}_fuel_consumption_at` |

`tm_fuel_consumption` and `tm_energy_consumption` have no entity here; the card
dims those two tiles (graceful). The car body is pure CSS — no image asset.

## The three sensors

Object-id suffixes match the card exactly (`charging_power`,
`estimated_charging_time`, `full_charge_electric_range`).

1. **`estimated_charging_time`** — `BatteryStatus.field5` (already parsed into
   `battery_raw`; promoted to a real sensor). Minutes.
2. **`charging_power`** — battery wire **field 10** read via protobuf
   `UnknownFieldSet` (watts → kW). **No proto change.**
3. **`full_charge_electric_range`** — derived. Port the fork's `store.py`
   capture method; snapshot `electric_range` when the battery first reaches
   100%, persisted via HA `Store`, exposed with `state_class=measurement` for
   long-term statistics.

**Caveat:** `charging_power` / `estimated_charging_time` are unvalidated until
an active charge session; `full_charge_electric_range` stays empty until the
next 100% event. Labelled, not hidden.

## Why no proto regeneration

The committed `*_pb2.py` target protobuf runtime **5.27.2** (works on the user's
HA). The dev `.venv` has protobuf **7.35.1**; regenerating would emit a 7.x
runtime check that fails to import on HA. So field 5 is read from the existing
generated message and field 10 via `UnknownFieldSet`.

## Coordinator fix

`_async_update_data` appends a fresh `VolvoStore` to `store_datas` every poll
(unbounded growth; fragile for multi-vehicle). Reworked to reuse a per-VIN store
and rebuild `store_datas` aligned to the vehicle order each poll. This is
required for reliable full-charge capture and fixes the leak. `number.py`
(the other `store_datas[idx]` consumer) is unaffected.

## Card serving

Add a top-level `async_setup(hass, config)` that serves the card via
`async_register_static_paths` at `/volvooncall_cn/frontend/…` and auto-registers
it as a storage-mode Lovelace resource. Lovelace-internal imports are lazy +
guarded so a version mismatch degrades to "add the resource manually" instead of
breaking the integration.

## Test on HA

Branch `feature/volvo-car-card`. Deploy `custom_components/volvooncall_cn/` to
the HA host via scp, restart HA (gated on user confirmation), verify the three
new entities exist and the card resource registered, then add a
`custom:volvo-car-card` (VIN `yv1lfh5f3s1388274`, model `xc90_t8`) to a **new
test view** beside the existing 座驾 view.
