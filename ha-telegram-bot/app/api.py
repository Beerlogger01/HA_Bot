"""Home Assistant API client — retries, timeouts, reused session.

Communicates with HA Core API through the Supervisor proxy.
Never logs the Supervisor token.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger("ha_bot.api")

HA_BASE_URL = "http://supervisor/core/api"
HA_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20)
MAX_RETRIES = 3
RETRY_BACKOFF_BASE: float = 1.0


class HAClient:
    """Reuses a single aiohttp.ClientSession.
    Retries transient errors with exponential back-off.
    """

    def __init__(self, supervisor_token: str) -> None:
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(timeout=HA_TIMEOUT)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- internal request with retry --

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> tuple[bool, Any]:
        """HTTP request with retry.  Returns (success, data_or_error)."""
        assert self._session is not None, "HAClient.open() not called"
        url = f"{HA_BASE_URL}/{path}"
        last_error = ""

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._session.request(
                    method, url, json=json_data, headers=self._headers
                ) as resp:
                    if resp.status in (200, 201):
                        try:
                            data = await resp.json(content_type=None)
                        except (json.JSONDecodeError, aiohttp.ContentTypeError):
                            data = {}
                        return True, data

                    body = (await resp.text())[:300]
                    last_error = f"HTTP {resp.status}: {body}"

                    # Client errors except 429 — no retry
                    if 400 <= resp.status < 500 and resp.status != 429:
                        logger.error(
                            "HA API client error (no retry): %s %s -> %s",
                            method, path, last_error,
                        )
                        return False, last_error

                    logger.warning(
                        "HA API error (attempt %d/%d): %s %s -> %s",
                        attempt, MAX_RETRIES, method, path, last_error,
                    )
            except asyncio.TimeoutError:
                last_error = "Request timed out"
                logger.warning(
                    "HA API timeout (attempt %d/%d): %s %s",
                    attempt, MAX_RETRIES, method, path,
                )
            except aiohttp.ClientError as exc:
                last_error = f"Connection error: {exc}"
                logger.warning(
                    "HA API connection error (attempt %d/%d): %s %s -> %s",
                    attempt, MAX_RETRIES, method, path, exc,
                )

            if attempt < MAX_RETRIES:
                # Longer backoff for 502/503 (HA booting) vs other transient errors
                if "HTTP 502" in last_error or "HTTP 503" in last_error:
                    delay = RETRY_BACKOFF_BASE * 2 * (2 ** (attempt - 1))
                else:
                    delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        logger.error(
            "HA API failed after %d attempts: %s %s -> %s",
            MAX_RETRIES, method, path, last_error,
        )
        return False, last_error

    # -- public helpers --

    async def ha_get(self, path: str) -> tuple[bool, Any]:
        """GET request. Returns (success, data_or_error)."""
        return await self._request("GET", path)

    async def ha_post(self, path: str, payload: dict[str, Any] | None = None) -> tuple[bool, Any]:
        """POST request. Returns (success, data_or_error)."""
        return await self._request("POST", path, json_data=payload)

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> tuple[bool, str]:
        """Call an HA service.  Returns (success, error_message_or_empty)."""
        ok, result = await self._request(
            "POST", f"services/{domain}/{service}", json_data=data
        )
        if ok:
            logger.info(
                "Service called: %s.%s entity=%s",
                domain, service, data.get("entity_id", "?"),
            )
            return True, ""
        return False, str(result)

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Return entity state dict or None on failure."""
        ok, result = await self._request("GET", f"states/{entity_id}")
        return result if ok and isinstance(result, dict) else None

    async def list_states(self) -> list[dict[str, Any]]:
        """Return all entity states or empty list on failure."""
        ok, result = await self._request("GET", "states")
        if ok and isinstance(result, list):
            return result
        return []

    async def list_services(self) -> list[dict[str, Any]]:
        """Return all available services or empty list on failure."""
        ok, result = await self._request("GET", "services")
        if ok and isinstance(result, list):
            return result
        return []

    async def get_config(self) -> dict[str, Any] | None:
        """Fetch HA config (used for self-test at startup)."""
        ok, result = await self._request("GET", "config")
        return result if ok and isinstance(result, dict) else None
