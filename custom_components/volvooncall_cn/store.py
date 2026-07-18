
import logging
import math
from datetime import date, timedelta
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
