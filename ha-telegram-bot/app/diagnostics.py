"""Diagnostics module — health checks, debug info, error tracing.

Provides data for /health, /debug, /diag, /trace_last_error commands.
Includes a logging handler that captures errors to the DB ring buffer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from api import HAClient
from registry import HARegistry
from storage import Database

logger = logging.getLogger("ha_bot.diagnostics")


class ErrorCapture(logging.Handler):
    """Logging handler that writes ERROR+ records to the DB ring buffer."""

    def __init__(self, db: Database) -> None:
        super().__init__(level=logging.ERROR)
        self._db = db
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        tb = None
        if record.exc_info and record.exc_info[0] is not None:
            tb = "".join(traceback.format_exception(*record.exc_info))
        try:
            asyncio.run_coroutine_threadsafe(
                self._db.log_error(
                    level=record.levelname,
                    module=record.name,
                    message=record.getMessage(),
                    traceback_str=tb,
                ),
                self._loop,
            )
        except Exception:
            pass  # Never crash the logging chain


class Diagnostics:
    """Collects diagnostic info for bot health/debug commands."""

    def __init__(
        self, ha: HAClient, db: Database, registry: HARegistry,
        ha_version: str = "unknown",
    ) -> None:
        self._ha = ha
        self._db = db
        self._reg = registry
        self.ha_version = ha_version
        self._start_time = time.monotonic()
        self._start_ts = datetime.now(timezone.utc)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def uptime_str(self) -> str:
        secs = int(self.uptime_seconds)
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        return " ".join(parts)

    async def health_check(self) -> dict[str, Any]:
        """Quick health check — HA reachable, WS synced, DB ok."""
        ha_ok = False
        ha_cfg = await self._ha.get_config()
        if ha_cfg:
            ha_ok = True
            self.ha_version = ha_cfg.get("version", self.ha_version)

        return {
            "status": "ok" if ha_ok and self._reg.synced else "degraded",
            "ha_reachable": ha_ok,
            "ha_version": self.ha_version,
            "registry_synced": self._reg.synced,
            "uptime": self.uptime_str(),
            "floors": len(self._reg.floors),
            "areas": len(self._reg.areas),
            "devices": len(self._reg.devices),
            "entities": len(self._reg.entities),
        }

    async def debug_info(self) -> dict[str, Any]:
        """Extended debug info for /debug command."""
        health = await self.health_check()
        health.update({
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "started_at": self._start_ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "vacuum_routines": {
                vac: len(btns) for vac, btns in self._reg.vacuum_routines.items()
            },
            "vacuum_platforms": dict(self._reg.vacuum_platforms),
        })
        return health

    async def get_diagnostics_text(self) -> str:
        """Full diagnostics report as formatted text."""
        info = await self.debug_info()
        errors = await self._db.get_recent_errors(5)

        lines = [
            "\U0001f6e0 <b>Diagnostics</b>\n",
            f"Status: <code>{info['status']}</code>",
            f"HA: <code>{info['ha_version']}</code> ({'OK' if info['ha_reachable'] else 'FAIL'})",
            f"Registry: {'synced' if info['registry_synced'] else 'NOT synced'}",
            f"Uptime: {info['uptime']}",
            f"Python: {info.get('python', '?')}",
            f"PID: {info.get('pid', '?')}",
            f"Started: {info.get('started_at', '?')}",
            "",
            f"Floors: {info['floors']} | Areas: {info['areas']}",
            f"Devices: {info['devices']} | Entities: {info['entities']}",
        ]

        vac_routines = info.get("vacuum_routines", {})
        if vac_routines:
            lines.append("")
            for vac, count in vac_routines.items():
                plat = info.get("vacuum_platforms", {}).get(vac, "?")
                lines.append(f"Vacuum {vac}: {count} routines (platform={plat})")

        if errors:
            lines.append("\n<b>Recent errors:</b>")
            for err in errors[:5]:
                ts = err["timestamp"][:19]
                lines.append(f"\u2022 [{ts}] {_sanitize(err['message'][:100])}")

        return "\n".join(lines)

    async def trace_last_error(self) -> str:
        """Get the most recent error with full traceback."""
        errors = await self._db.get_recent_errors(1)
        if not errors:
            return "\u2705 No errors recorded."

        err = errors[0]
        lines = [
            "\U0001f534 <b>Last Error</b>\n",
            f"Time: <code>{err['timestamp']}</code>",
            f"Level: {err['level']}",
            f"Module: {err['module']}",
            f"Message: {_sanitize(err['message'][:500])}",
        ]
        if err.get("traceback"):
            tb = err["traceback"][:2000]
            lines.append(f"\n<pre>{_sanitize(tb)}</pre>")
        return "\n".join(lines)

    async def notify_test(self, bot: Any, chat_id: int) -> str:
        """Send a test notification to verify delivery."""
        from aiogram import Bot
        if not isinstance(bot, Bot):
            return "Invalid bot instance"
        try:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            await bot.send_message(
                chat_id=chat_id,
                text=f"\U0001f514 <b>Test notification</b>\n{ts}",
                parse_mode="HTML",
            )
            return "Test notification sent successfully"
        except Exception as exc:
            return f"Failed: {exc}"


def _sanitize(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
