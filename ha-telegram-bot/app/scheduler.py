"""Scheduler — periodic task execution with simple cron expressions.

Supports a subset of cron: minute hour day_of_month month day_of_week
Uses HA service calls as the execution engine.
Falls back gracefully if the schedule expression is invalid.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone
from typing import Any

from api import HAClient
from storage import Database

logger = logging.getLogger("ha_bot.scheduler")

CHECK_INTERVAL = 30  # seconds between schedule checks


def parse_cron_field(field: str, min_val: int, max_val: int) -> list[int] | None:
    """Parse a single cron field. Returns list of matching values or None on error."""
    if field == "*":
        return list(range(min_val, max_val + 1))

    values: list[int] = []
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                return None
            if base == "*":
                values.extend(range(min_val, max_val + 1, step))
            else:
                try:
                    start = int(base)
                except ValueError:
                    return None
                values.extend(range(start, max_val + 1, step))
        elif "-" in part:
            lo_str, hi_str = part.split("-", 1)
            try:
                lo, hi = int(lo_str), int(hi_str)
            except ValueError:
                return None
            values.extend(range(lo, hi + 1))
        else:
            try:
                values.append(int(part))
            except ValueError:
                return None

    return [v for v in values if min_val <= v <= max_val] or None


def next_cron_time(cron_expr: str, after: float | None = None) -> float:
    """Calculate the next run time for a cron expression.

    cron_expr: "minute hour day month weekday" (5 fields)
    Returns epoch timestamp of next matching time, or 0.0 on parse error.
    """
    if after is None:
        after = time.time()

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return 0.0

    minutes = parse_cron_field(parts[0], 0, 59)
    hours = parse_cron_field(parts[1], 0, 23)
    days = parse_cron_field(parts[2], 1, 31)
    months = parse_cron_field(parts[3], 1, 12)
    weekdays = parse_cron_field(parts[4], 0, 6)

    if not all([minutes, hours, days, months, weekdays]):
        return 0.0

    dt = datetime.fromtimestamp(after, tz=timezone.utc).replace(second=0, microsecond=0)

    # Search up to 366 days ahead
    for _ in range(527040):  # 366 * 24 * 60
        dt = dt.replace(second=0, microsecond=0)
        ts = dt.timestamp()
        if ts <= after:
            dt = datetime.fromtimestamp(ts + 60, tz=timezone.utc)
            continue

        if (dt.month in months  # type: ignore[operator]
                and dt.day in days  # type: ignore[operator]
                and dt.weekday() in [w % 7 for w in weekdays]  # type: ignore[union-attr]
                and dt.hour in hours  # type: ignore[operator]
                and dt.minute in minutes):  # type: ignore[operator]
            return ts

        dt = datetime.fromtimestamp(ts + 60, tz=timezone.utc)

    return 0.0


def validate_cron(cron_expr: str) -> str | None:
    """Validate cron expression. Returns error message or None if valid."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return "Expected 5 fields: minute hour day month weekday"
    labels = ["minute(0-59)", "hour(0-23)", "day(1-31)", "month(1-12)", "weekday(0-6)"]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for i, (part, label, (lo, hi)) in enumerate(zip(parts, labels, ranges)):
        result = parse_cron_field(part, lo, hi)
        if result is None:
            return f"Invalid {label}: '{part}'"
    return None


class Scheduler:
    """Background task that checks and executes due schedules."""

    def __init__(self, ha: HAClient, db: Database) -> None:
        self._ha = ha
        self._db = db
        self._task: asyncio.Task[None] | None = None
        self._stop = False

    async def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run_loop(), name="scheduler")
        logger.info("Scheduler started (check every %ds)", CHECK_INTERVAL)

    async def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _run_loop(self) -> None:
        while not self._stop:
            try:
                await self._check_due()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Scheduler loop error")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check_due(self) -> None:
        now = time.time()
        due = await self._db.get_due_schedules(now)
        for sched in due:
            result = await self._execute(sched)
            # Calculate next run
            nr = next_cron_time(sched["cron_expr"], after=now)
            if nr == 0.0:
                # Invalid cron — disable
                result = f"DISABLED (invalid cron: {sched['cron_expr']})"
                await self._db.toggle_schedule(sched["id"], sched["user_id"])
                nr = 0.0
            await self._db.update_schedule_run(sched["id"], nr, result)

    async def _execute(self, sched: dict[str, Any]) -> str:
        """Execute a scheduled action. Returns result string."""
        action_type = sched["action_type"]
        payload = sched["payload"]
        name = sched["name"]

        logger.info("Executing schedule #%d '%s' (%s)", sched["id"], name, action_type)

        if action_type == "service_call":
            domain = payload.get("domain", "")
            service = payload.get("service", "")
            data = payload.get("data", {})
            if not domain or not service:
                return "ERROR: missing domain/service"
            ok, err = await self._ha.call_service(domain, service, data)
            if ok:
                return "OK"
            return f"ERROR: {err[:200]}"

        elif action_type == "ha_automation":
            eid = payload.get("entity_id", "")
            if not eid:
                return "ERROR: missing entity_id"
            ok, err = await self._ha.call_service(
                "automation", "trigger", {"entity_id": eid},
            )
            return "OK" if ok else f"ERROR: {err[:200]}"

        return f"ERROR: unknown action_type '{action_type}'"
