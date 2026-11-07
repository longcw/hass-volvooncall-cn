
import logging
import math
from datetime import date, datetime, time, timedelta
from typing import TypedDict, Unpack
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from .fuel_price import price_on
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1

# 100 disables the limit so charging behaves exactly as before (charge to full).
CHARGE_LIMIT_DISABLED = 100
CHARGE_LIMIT_MIN = 50

# Daily charge timer: when the coordinator checks whether to start a charge.
DEFAULT_CHARGE_TIMER_START = time(23, 0)

# How much odometer history to keep for the trailing-30-day + monthly charts.
_ODO_DAILY_KEEP = 45          # days
_ODO_MONTHS_KEEP = 14         # calendar months
_ODO_30D_TOLERANCE_DAYS = 12  # nearest snapshot to "30 days ago" must be this close

# --- Refuel log (per-tank fuel consumption and cost) ------------------------
# A refill shows up as a jump in the reported tank level; anything smaller is
# gauge noise (slosh on a slope, temperature drift, sensor rounding).
REFUEL_MIN_DELTA_L = 5.0
# A fill straddling two polls must extend the open record instead of creating a
# second one: same station, car barely moved.
_REFUEL_MERGE_MINUTES = 60
_REFUEL_MERGE_MAX_KM = 3.0
_REFUEL_KEEP = 50             # records
# Sanity bounds on a computed tank-to-tank figure; outside these the pair of
# records is unusable (partial fill, odometer glitch, hand-typed mistake).
_REFUEL_MIN_SEGMENT_KM = 20.0
_REFUEL_CONSUMPTION_MIN = 1.0
_REFUEL_CONSUMPTION_MAX = 40.0


def _as_float(value):
    """Finite float, or None for anything unusable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_dt(value):
    """Parse a stored ISO timestamp, or None when it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sort_ts(record) -> float:
    """Sortable epoch for a record; mixing naive/aware datetimes would raise."""
    parsed = _parse_dt(record.get("at"))
    if parsed is None:
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


class StoreData(TypedDict, total=False):
    """Volvo Store Data"""
    engine_duration_number: int
    charge_limit: int
    # Daily timed home charge (see _apply_charge_timer in __init__.py).
    charge_timer_enabled: bool
    charge_timer_start: str      # "HH:MM", local time
    charge_timer_any_car: bool   # fire on the pile's plugged state alone
    charge_timer_last_run: str   # "YYYY-MM-DD", the day the timer last decided
    # Odometer snapshots for forward-only distance stats.
    odometer_max: float
    odometer_daily: dict     # {"YYYY-MM-DD": km}
    odometer_monthly: dict   # {"YYYY-MM": {"first": km, "last": km}}
    odometer_seeded: bool    # one-time backfill from recorder statistics done
    # Refuel log: one record per detected/logged fill-up, oldest first.
    refuel_log: list
    fuel_amount_last: float  # previous reading, the jump-detection baseline


class VolvoStore(Store[StoreData]):
    def __init__(self, hass: HomeAssistant, vin: str):
        super().__init__(hass=hass, key=f"{DOMAIN}.{vin}", version=STORE_VERSION)
        self.data: StoreData | None = None
        self.default_data = StoreData(engine_duration_number=5)

    def get(self, key):
        assert self.data is not None
        return self.data.get(key)

    async def load_create_data(self) -> StoreData:
        self.data = await self.async_load() or self.default_data
        return self.data

    async def update(self, **kwargs: Unpack[StoreData]):
        self.data = self.data or await self.load_create_data()
        for key, value in kwargs.items():
            if value is not None and key in StoreData.__annotations__:
                self.data[key] = value
        await self.async_save(self.data)

    def get_engine_duration_number(self):
        self.data = self.data or self.default_data
        return self.data.get("engine_duration_number")

    async def set_engine_duration_number(self, value):
        await self.update(engine_duration_number=int(value))

    def get_charge_limit(self) -> int:
        """Persisted SoC ceiling (%). Defaults to 100 (disabled) when unset."""
        self.data = self.data or self.default_data
        value = self.data.get("charge_limit")
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return CHARGE_LIMIT_DISABLED
        return min(max(limit, CHARGE_LIMIT_MIN), CHARGE_LIMIT_DISABLED)

    async def set_charge_limit(self, value):
        limit = min(max(int(value), CHARGE_LIMIT_MIN), CHARGE_LIMIT_DISABLED)
        await self.update(charge_limit=limit)

    def get_charge_timer_start(self) -> time:
        """Daily start time of the timed charge; the default when unset or corrupt."""
        self.data = self.data or self.default_data
        raw = self.data.get("charge_timer_start")
        if isinstance(raw, str):
            try:
                return time.fromisoformat(raw)
            except ValueError:
                pass
        return DEFAULT_CHARGE_TIMER_START

    async def set_charge_timer_start(self, value):
        """``value`` is a ``datetime.time`` or an ISO ``HH:MM[:SS]`` string."""
        parsed = value if isinstance(value, time) else time.fromisoformat(str(value))
        await self.update(charge_timer_start=parsed.strftime("%H:%M"))

    async def record_odometer(self, odometer, now):
        """Record a validated odometer reading into the daily + monthly snapshots.

        ``now`` is a (local) datetime. Backward / zero / non-numeric readings are
        ignored so a glitchy poll can't pollute the distance stats.
        """
        try:
            odo = float(odometer)
        except (TypeError, ValueError):
            return
        if odo <= 0:
            return
        self.data = self.data or await self.load_create_data()

        # Monotonic guard: the odometer only increases, so ignore any reading
        # below the max we've seen (a stale/zero cache read).
        prev_max = self.data.get("odometer_max")
        try:
            prev_max = float(prev_max) if prev_max is not None else None
        except (TypeError, ValueError):
            prev_max = None
        if prev_max is not None and odo < prev_max:
            return

        daily = dict(self.data.get("odometer_daily") or {})
        monthly = dict(self.data.get("odometer_monthly") or {})

        today = now.date().isoformat()
        daily[today] = odo
        # Keep only recent days.
        keep_after = (now.date() - timedelta(days=_ODO_DAILY_KEEP)).isoformat()
        daily = {d: v for d, v in daily.items() if d >= keep_after}

        ym = now.strftime("%Y-%m")
        bucket = dict(monthly.get(ym) or {})
        if "first" not in bucket:
            bucket["first"] = odo
        bucket["last"] = odo
        monthly[ym] = bucket
        # Keep only recent months.
        for old in sorted(monthly)[:-_ODO_MONTHS_KEEP]:
            monthly.pop(old, None)

        await self.update(
            odometer_max=max(odo, prev_max or 0),
            odometer_daily=daily,
            odometer_monthly=monthly,
        )

    async def seed_odometer(self, daily_points):
        """One-time backfill of odometer snapshots from recorder statistics.

        ``daily_points`` is an ascending list of ``(date, km)``. Merges any days
        we don't already have, recomputes the monthly buckets, and marks the
        store seeded so the (DB-backed) query only runs once.
        """
        self.data = self.data or await self.load_create_data()
        daily = dict(self.data.get("odometer_daily") or {})
        added = False
        for day, km in daily_points:
            try:
                value = float(km)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            key = day.isoformat()
            if key not in daily:
                daily[key] = value
                added = True

        if not added:
            await self.update(odometer_seeded=True)
            return

        monthly = {}
        for key in sorted(daily):
            value = daily[key]
            ym = key[:7]
            bucket = monthly.get(ym) or {}
            if "first" not in bucket:
                bucket["first"] = value
            bucket["last"] = value
            monthly[ym] = bucket
        for old in sorted(monthly)[:-_ODO_MONTHS_KEEP]:
            monthly.pop(old, None)

        await self.update(
            odometer_seeded=True,
            odometer_daily=daily,
            odometer_monthly=monthly,
            odometer_max=max(daily.values()),
        )

    def get_distance_stats(self, current_odometer, now):
        """Return ``{"last_30d": float|None, "monthly": [{"month", "km"}, ...]}``
        from the stored odometer snapshots. Forward-only: values fill in over
        time as snapshots accumulate."""
        data = self.data or {}
        monthly_raw = data.get("odometer_monthly") or {}
        daily = data.get("odometer_daily") or {}

        monthly = []
        for ym in sorted(monthly_raw)[-12:]:
            bucket = monthly_raw.get(ym) or {}
            try:
                km = float(bucket.get("last")) - float(bucket.get("first"))
            except (TypeError, ValueError):
                continue
            if 0 <= km <= 50000:
                monthly.append({"month": ym, "km": round(km, 1)})

        last_30d = None
        try:
            current = float(current_odometer)
        except (TypeError, ValueError):
            current = None
        if daily and current is not None:
            cutoff = now.date() - timedelta(days=30)
            best = None    # (abs day delta, km) — closest snapshot to 30 days ago
            oldest = None  # (date, km) — earliest snapshot we have
            for day_iso, value in daily.items():
                try:
                    day = date.fromisoformat(day_iso)
                    val = float(value)
                except (TypeError, ValueError):
                    continue
                delta = abs((day - cutoff).days)
                if best is None or delta < best[0]:
                    best = (delta, val)
                if oldest is None or day < oldest[0]:
                    oldest = (day, val)
            chosen = None
            if best is not None and best[0] <= _ODO_30D_TOLERANCE_DAYS:
                chosen = best[1]
            elif oldest is not None:
                # Not yet 30 days of history — use the earliest snapshot we have
                # so the figure reflects available data instead of showing blank.
                chosen = oldest[1]
            if chosen is not None:
                diff = current - chosen
                if 0 <= diff <= 50000:
                    last_30d = round(diff, 1)

        return {"last_30d": last_30d, "monthly": monthly}

    # --- Refuel log ---------------------------------------------------------

    def _refuel_log(self) -> list:
        """The stored records, oldest first, junk entries dropped."""
        data = self.data or {}
        log = [r for r in (data.get("refuel_log") or []) if isinstance(r, dict)]
        log.sort(key=_sort_ts)
        return log

    async def _save_refuel_log(self, log, **extra: Unpack[StoreData]):
        log.sort(key=_sort_ts)
        await self.update(refuel_log=log[-_REFUEL_KEEP:], **extra)

    @staticmethod
    def _unique_refuel_id(log, base: str) -> str:
        """``base`` (the timestamp), suffixed if two records share a second."""
        taken = {r.get("id") for r in log}
        if base not in taken:
            return base
        for suffix in range(1, 100):
            candidate = f"{base}#{suffix}"
            if candidate not in taken:
                return candidate
        return f"{base}#{len(log)}"

    @staticmethod
    def _can_merge_refuel(record, odometer, now) -> bool:
        """True when a new jump belongs to the record still being filled.

        Fuelling can straddle two polls (30 L seen, then the rest); that is one
        fill, not two. A hand-corrected record is never touched again."""
        if record.get("edited") or record.get("source") == "manual":
            return False
        at = _parse_dt(record.get("at"))
        if at is None:
            return False
        try:
            if now - at > timedelta(minutes=_REFUEL_MERGE_MINUTES):
                return False
        except TypeError:  # naive/aware mismatch
            return False
        previous_odometer = _as_float(record.get("odometer"))
        odometer = _as_float(odometer)
        if previous_odometer is not None and odometer is not None:
            if odometer - previous_odometer > _REFUEL_MERGE_MAX_KM:
                return False
        return True

    async def record_fuel_amount(self, fuel_amount, odometer, now) -> bool:
        """Track the tank level and log a refuel when it jumps up.

        Returns True when a record was created or extended. ``now`` is an aware
        datetime; the odometer is stored with the record so the next fill can be
        divided by the distance actually driven on that tank."""
        fuel = _as_float(fuel_amount)
        if fuel is None or fuel <= 0:
            # A failed poll reports 0/None — keep the previous baseline so the
            # recovery reading isn't mistaken for a fill-up.
            return False
        self.data = self.data or await self.load_create_data()
        previous = _as_float(self.data.get("fuel_amount_last"))

        if previous is None or fuel - previous < REFUEL_MIN_DELTA_L:
            if previous != fuel:
                await self.update(fuel_amount_last=fuel)
            return False

        delta = round(fuel - previous, 2)
        odometer_value = _as_float(odometer)
        log = self._refuel_log()

        if log and self._can_merge_refuel(log[-1], odometer_value, now):
            record = dict(log[-1])
            record["liters"] = round((_as_float(record.get("liters")) or 0) + delta, 2)
            sensor_liters = _as_float(record.get("liters_sensor"))
            if sensor_liters is not None:
                record["liters_sensor"] = round(sensor_liters + delta, 2)
            record["fuel_after"] = fuel
            log[-1] = record
        else:
            timestamp = now.isoformat()
            log.append({
                "id": self._unique_refuel_id(log, timestamp),
                "at": timestamp,
                "odometer": odometer_value,
                "liters": delta,
                "liters_sensor": delta,
                "edited": False,
                "fuel_before": round(previous, 2),
                "fuel_after": fuel,
                "source": "auto",
                "price": None,
                "price_source": None,
            })

        await self._save_refuel_log(log, fuel_amount_last=fuel)
        return True

    async def add_refuel(
        self, liters, odometer, when, fuel_before=None, price=None
    ) -> dict | None:
        """Log a fill the detector missed — including a past one, hence ``when``.

        Returns the new record."""
        liters_value = _as_float(liters)
        if liters_value is None or liters_value <= 0:
            return None
        price_value = _as_float(price)
        self.data = self.data or await self.load_create_data()
        log = self._refuel_log()
        timestamp = when.isoformat()
        record = {
            "id": self._unique_refuel_id(log, timestamp),
            "at": timestamp,
            "odometer": _as_float(odometer),
            "liters": round(liters_value, 2),
            "liters_sensor": None,
            "edited": True,
            "fuel_before": _as_float(fuel_before),
            "fuel_after": None,
            "source": "manual",
            "price": price_value,
            "price_source": "manual" if price_value is not None else None,
        }
        log.append(record)
        await self._save_refuel_log(log)
        return record

    async def update_refuel(
        self, record_id, liters=None, odometer=None, when=None,
        fuel_before=None, price=None,
    ) -> bool:
        """Correct a record from the pump receipt; the id survives a date change.

        Records are ordered by ``at``, so a new date also changes which tank the next fill measures."""
        self.data = self.data or await self.load_create_data()
        log = self._refuel_log()
        changes = {}
        # litres and price must be positive; a reading of zero is valid
        for key, value, allow_zero in (
            ("liters", liters, False), ("odometer", odometer, True),
            ("fuel_before", fuel_before, True), ("price", price, False),
        ):
            if value is None:
                continue
            number = _as_float(value)
            if number is None or number < 0 or (number == 0 and not allow_zero):
                return False
            changes[key] = round(number, 2) if key in ("liters", "price") else number
        if when is not None:
            changes["at"] = when.isoformat()
        if not changes:
            return False
        if "price" in changes:
            changes["price_source"] = "manual"

        for index, record in enumerate(log):
            if record.get("id") != record_id:
                continue
            log[index] = {**record, **changes, "edited": True}
            await self._save_refuel_log(log)
            return True
        return False

    def needs_refuel_prices(self) -> bool:
        """True while some record still waits for a looked-up price."""
        return any(
            r.get("price") is None and r.get("price_source") != "manual"
            for r in self._refuel_log()
        )

    async def apply_refuel_prices(self, history: dict) -> bool:
        """Price unpriced records by the adjustment in force on their date; True if any changed."""
        log = self._refuel_log()
        changed = False
        for index, record in enumerate(log):
            if record.get("price") is not None or record.get("price_source") == "manual":
                continue
            at = _parse_dt(record.get("at"))
            price = price_on(history, at.date()) if at else None
            if price is not None:
                log[index] = {**record, "price": price, "price_source": "auto"}
                changed = True
        if changed:
            await self._save_refuel_log(log)
        return changed

    async def delete_refuel(self, record_id) -> bool:
        self.data = self.data or await self.load_create_data()
        log = self._refuel_log()
        remaining = [r for r in log if r.get("id") != record_id]
        if len(remaining) == len(log):
            return False
        await self._save_refuel_log(remaining)
        return True

    def get_refuel_stats(self) -> dict:
        """Per-tank consumption and cost from the log, records newest first.

        Fuel burned is ``before[i-1] + liters[i-1] - before[i]``, which holds for
        partial fills and trusts the pump over the gauge's reading after a fill,
        which sits at full for the top few litres."""
        records = []
        previous = None
        total_liters = total_km = 0.0
        priced_cost = priced_km = 0.0

        for record in self._refuel_log():
            odometer = _as_float(record.get("odometer"))
            liters = _as_float(record.get("liters"))
            before = _as_float(record.get("fuel_before"))
            price = _as_float(record.get("price"))
            # without a level reading, assume every fill tops the tank up
            after = (
                round(before + liters, 2)
                if before is not None and liters is not None
                else _as_float(record.get("fuel_after"))
            )
            distance = fuel_used = consumption = cost_per_100km = None
            if previous is not None and odometer is not None:
                km = round(odometer - previous["odometer"], 1)
                if km > 0:
                    distance = km
                    if previous["after"] is not None and before is not None:
                        used = previous["after"] - before
                    else:
                        used = liters
                    if used and used > 0 and km >= _REFUEL_MIN_SEGMENT_KM:
                        value = round(used / km * 100, 1)
                        if _REFUEL_CONSUMPTION_MIN <= value <= _REFUEL_CONSUMPTION_MAX:
                            fuel_used = round(used, 2)
                            consumption = value
                            total_liters += used
                            total_km += km
                            # the burned fuel was bought at the previous fill
                            tank_price = previous["price"] or price
                            if tank_price:
                                cost_per_100km = round(used * tank_price / km * 100, 1)
                                priced_cost += used * tank_price
                                priced_km += km
            records.append({
                **record,
                "level_after": after,
                "cost": round(liters * price, 2) if liters and price else None,
                "distance": distance,
                "fuel_used": fuel_used,
                "consumption": consumption,
                "cost_per_100km": cost_per_100km,
            })
            if odometer is not None:
                previous = {"odometer": odometer, "after": after, "price": price}

        def _last(key):
            return next((r[key] for r in reversed(records) if r[key] is not None), None)

        return {
            "last": _last("consumption"),
            "average": round(total_liters / total_km * 100, 1) if total_km > 0 else None,
            "last_cost_per_100km": _last("cost_per_100km"),
            "average_cost_per_100km": (
                round(priced_cost / priced_km * 100, 1) if priced_km > 0 else None
            ),
            "count": len(records),
            "records": list(reversed(records)),
        }
