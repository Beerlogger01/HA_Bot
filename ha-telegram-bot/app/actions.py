"""One authorization and service boundary for inline UI, widgets and schedules."""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sessions import current_session

ENTITY_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9_\-]*$")
SERVICES = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle"},
    "cover": {"open_cover", "close_cover", "stop_cover"},
    "climate": {"set_temperature", "turn_on", "turn_off"},
    "humidifier": {"turn_on", "turn_off", "set_humidity", "set_mode"},
    "water_heater": {"set_temperature", "turn_on", "turn_off"},
    "vacuum": {"start", "stop", "pause", "return_to_base", "locate", "send_command", "clean_area"},
    "media_player": {"turn_on", "turn_off", "media_play", "media_pause", "media_stop",
                     "volume_set", "volume_up", "volume_down", "volume_mute", "select_source", "play_media"},
    "scene": {"turn_on"}, "script": {"turn_on", "turn_off"},
    "automation": {"turn_on", "turn_off", "trigger"},
    "lock": {"lock", "unlock"}, "button": {"press"},
    "select": {"select_option"}, "number": {"set_value"},
    "todo": {"get_items", "add_item", "update_item", "remove_item"},
}
ADMIN_DOMAINS = {"lock", "automation"}
# Data from imported favorite actions / schedules must not be able to select all
# devices, inject service paths, or expand targets through area/device selectors.
DATA_KEYS = {"entity_id", "brightness", "rgb_color", "color_temp", "color_temp_kelvin",
             "temperature", "volume_level", "is_volume_muted", "source", "option", "value",
             "media_content_id", "media_content_type", "command", "params", "variables",
             "item", "status", "skip_condition", "cleaning_area_id", "humidity", "mode"}


def validate_service(domain, service, data, *, role="user") -> str | None:
    if not isinstance(domain, str) or not isinstance(service, str):
        return "Недопустимый сервис"
    if domain not in SERVICES or service not in SERVICES[domain]:
        return "Недопустимый сервис"
    if not isinstance(data, dict) or set(data) - DATA_KEYS:
        return "Недопустимые параметры"
    eid = data.get("entity_id")
    if not isinstance(eid, str) or not ENTITY_ID.fullmatch(eid) or not eid.startswith(domain + "."):
        return "Требуется конкретная сущность соответствующего домена"
    if role not in {"admin", "user"} and (domain, service) != ("todo", "get_items"):
        return "Недостаточно прав"
    if domain in ADMIN_DOMAINS and role != "admin":
        return "Требуются права администратора"
    import math
    for key, low, high in (("brightness", 0, 255), ("volume_level", 0, 1), ("temperature", -100, 150), ("humidity", 0, 100)):
        if key in data:
            value = data[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                return "Значение вне допустимого диапазона"
    if service == "send_command" and data.get("command") != "app_segment_clean":
        return "Недопустимая команда пылесоса"
    if domain == "automation" and service == "trigger" and data.get("skip_condition") is not False:
        return "Разовый запуск должен проверять условия автоматизации"
    if service == "clean_area" and (not isinstance(data.get("cleaning_area_id"), list) or len(data["cleaning_area_id"]) != 1 or not isinstance(data["cleaning_area_id"][0], str)):
        return "Требуется конкретное помещение"
    if service == "play_media":
        url = data.get("media_content_id", "")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return "Недопустимый адрес медиа"
    return None


class ActionService:
    """Small HA facade; all writes are authorized, serialized and audited.

    Transaction covers read-modify-write handlers too. Absolute slider targets
    use last accepted writer wins. Never retry a non-idempotent service POST.
    """
    def __init__(self, ha, db, config, rate_limiter):
        self._ha, self._db, self._cfg, self._rl = ha, db, config, rate_limiter
        self._lock = asyncio.Lock()
        self._inside = ContextVar("action_transaction", default=False)

    def __getattr__(self, name):
        if name in {"get_state", "list_states", "list_services", "get_config", "ha_get"}:
            return getattr(self._ha, name)
        raise AttributeError(name)

    @asynccontextmanager
    async def transaction(self):
        if self._inside.get():
            yield
            return
        async with self._lock:
            token = self._inside.set(True)
            try:
                yield
            finally:
                self._inside.reset(token)

    async def call_service(self, domain, service, data):
        async with self.transaction():
            actor = current_session.get()
            if actor is None:
                return False, "Нет авторизованной сессии"
            role = await self._db.get_user_role(actor.user_id)
            error = validate_service(domain, service, data, role=role)
            if self._cfg.allowed_user_ids and actor.user_id not in self._cfg.allowed_user_ids:
                error = "Нет доступа"
            if self._cfg.allowed_chat_id and actor.chat_id != self._cfg.allowed_chat_id:
                error = "Нет доступа к чату"
            read_only = (domain, service) == ("todo", "get_items")
            if not error and not read_only and not self._rl.check():
                error = "Лимит действий. Подождите несколько секунд"
            ok, result = False, error or ""
            if not error and not read_only:
                try:
                    state = await self._ha.get_state(data["entity_id"])
                except Exception:
                    state = None
                if not state or state.get("state") == "unavailable" or (state.get("state") == "unknown" and domain not in {"scene", "button"}):
                    error = "Устройство недоступно. Обновите экран"
                    ok, result = False, error
            if not error:
                if not read_only:
                    self._rl.record()  # Reserve before awaiting HA, including failed attempts.
                try:
                    ok, result = await self._ha.call_service(domain, service, data)
                except Exception:
                    ok, result = False, "Home Assistant недоступен"
            if not read_only:
                await self._db.write_audit(
                    chat_id=actor.chat_id, user_id=actor.user_id, username="",
                    action=f"service:{domain}.{service}", entity_id=data.get("entity_id") if isinstance(data, dict) else None,
                    success=ok, error=str(result)[:200] if not ok else None,
                )
            if not read_only:
                actor.data["service_receipt"] = (
                    "Команда принята Home Assistant. Карточка показывает состояние HA; устройство может обновиться позже."
                    if ok else "Команда не подтверждена: " + str(result)[:180])
            return ok, result
