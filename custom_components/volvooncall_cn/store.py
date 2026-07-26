
import logging
import math
from datetime import date, datetime, timedelta
from typing import TypedDict, Unpack
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1

# 100 disables the limit so charging behaves exactly as before (charge to full).
CHARGE_LIMIT_DISABLED = 100
CHARGE_LIMIT_MIN = 50

# How much odometer history to keep for the trailing-30-day + monthly charts.
_ODO_DAILY_KEEP = 45          # days
_ODO_MONTHS_KEEP = 14         # calendar months
_ODO_30D_TOLERANCE_DAYS = 12  # nearest snapshot to "30 days ago" must be this close

# --- Refuel log (tank-to-tank fuel consumption) -----------------------------
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
    full_charge_electric_range: int | float
    full_charge_sampled_at: str
    full_charge_sample_count: int
    full_charge_session_active: bool
    full_charge_data_source: str
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

    async def async_capture_full_charge_range(
        self,
        battery_level: int | float | None,
        electric_range: int | float | None,
        sampled_at: str,
        data_source: str | None = None,
    ) -> bool:
        """Capture one range sample when a new 100% charge session starts.

        Samples once per 100% session: the first time battery_level reaches
        100% the current electric_range is recorded; no further samples are
        taken until battery_level drops below 100% and rises to 100% again.
        """
        self.data = self.data or await self.load_create_data()
        if battery_level is None:
            return False

        try:
            battery_level_value = float(battery_level)
        except (TypeError, ValueError):
            return False

        if not math.isfinite(battery_level_value):
            return False

        session_active = bool(
            self.data.get("full_charge_session_active", False)
        )
        if battery_level_value < 100:
            if session_active:
                await self.update(full_charge_session_active=False)
            return False

        if session_active or electric_range is None:
            return False

        try:
            electric_range_value = float(electric_range)
        except (TypeError, ValueError):
            return False

        if (
            not math.isfinite(electric_range_value)
            or electric_range_value <= 0
        ):
            return False

        captured_range: int | float
        if isinstance(electric_range, int):
            captured_range = electric_range
        else:
            captured_range = electric_range_value

        sample = StoreData(
            full_charge_electric_range=captured_range,
            full_charge_sampled_at=sampled_at,
            full_charge_sample_count=(
                self.data.get("full_charge_sample_count", 0) + 1
            ),
            full_charge_session_active=True,
        )
        if data_source is not None:
            sample["full_charge_data_source"] = data_source
        await self.update(**sample)
        return True

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
            })

        await self._save_refuel_log(log, fuel_amount_last=fuel)
        return True

    async def add_refuel(self, liters, odometer, now) -> dict | None:
        """Log a fill the detector missed. Returns the new record."""
        liters_value = _as_float(liters)
        if liters_value is None or liters_value <= 0:
            return None
        self.data = self.data or await self.load_create_data()
        log = self._refuel_log()
        timestamp = now.isoformat()
        record = {
            "id": self._unique_refuel_id(log, timestamp),
            "at": timestamp,
            "odometer": _as_float(odometer),
            "liters": round(liters_value, 2),
            "liters_sensor": None,
            "edited": True,
            "fuel_before": None,
            "fuel_after": None,
            "source": "manual",
        }
        log.append(record)
        await self._save_refuel_log(log)
        return record

    async def update_refuel(self, record_id, liters=None, odometer=None) -> bool:
        """Correct a record — typically the litres, to what the pump charged for."""
        self.data = self.data or await self.load_create_data()
        log = self._refuel_log()
        liters_value = _as_float(liters)
        odometer_value = _as_float(odometer)
        if liters_value is not None and liters_value <= 0:
            return False
        if liters_value is None and odometer_value is None:
            return False

        for index, record in enumerate(log):
            if record.get("id") != record_id:
                continue
            updated = dict(record)
            if liters_value is not None:
                updated["liters"] = round(liters_value, 2)
            if odometer_value is not None:
                updated["odometer"] = odometer_value
            updated["edited"] = True
            log[index] = updated
            await self._save_refuel_log(log)
            return True
        return False

    async def delete_refuel(self, record_id) -> bool:
        self.data = self.data or await self.load_create_data()
        log = self._refuel_log()
        remaining = [r for r in log if r.get("id") != record_id]
        if len(remaining) == len(log):
            return False
        await self._save_refuel_log(remaining)
        return True

    def get_refuel_stats(self) -> dict:
        """Tank-to-tank consumption derived from the log.

        The litres put in at fill *i* are exactly what was burned since fill
        *i-1*, so ``liters[i] / (odometer[i] - odometer[i-1]) * 100`` is the
        consumption for that tank. The first record has no predecessor, and any
        segment that is too short or lands outside sane bounds is left blank
        instead of publishing a nonsense figure.

        Returns ``{"last", "average", "count", "records"}`` with records newest
        first, each carrying its computed ``distance`` and ``consumption``."""
        records = []
        previous_odometer = None
        total_liters = 0.0
        total_km = 0.0

        for record in self._refuel_log():
            odometer = _as_float(record.get("odometer"))
            liters = _as_float(record.get("liters"))
            distance = None
            consumption = None
            if previous_odometer is not None and odometer is not None:
                km = round(odometer - previous_odometer, 1)
                if km > 0:
                    distance = km
                    if liters and km >= _REFUEL_MIN_SEGMENT_KM:
                        value = round(liters / km * 100, 1)
                        if _REFUEL_CONSUMPTION_MIN <= value <= _REFUEL_CONSUMPTION_MAX:
                            consumption = value
                            total_liters += liters
                            total_km += km
            records.append({
                **record,
                "distance": distance,
                "consumption": consumption,
            })
            if odometer is not None:
                previous_odometer = odometer

        last = next(
            (r["consumption"] for r in reversed(records) if r["consumption"] is not None),
            None,
        )
        average = round(total_liters / total_km * 100, 1) if total_km > 0 else None
        return {
            "last": last,
            "average": average,
            "count": len(records),
            "records": list(reversed(records)),
        }
