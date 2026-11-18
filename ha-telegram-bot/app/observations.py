"""Present HA observations without inventing freshness, cause or group membership."""
from datetime import datetime, timezone


def observation_age(state, *, now=None, max_age=None):
    now = now or datetime.now(timezone.utc)
    for key, label in (("last_reported", "Сообщено HA"), ("last_updated", "Данные изменены")):
        try:
            stamp = datetime.fromisoformat(str(state.get(key, "")).replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > now:
                continue
            seconds = int((now - stamp).total_seconds())
        except (ValueError, TypeError):
            continue
        age = "только что" if seconds < 60 else (f"{seconds // 60} мин назад" if seconds < 3600
               else f"{seconds // 3600} ч назад" if seconds < 86400 else f"{seconds // 86400} дн назад")
        # last_updated is an attribute/state change, NOT a measurement heartbeat.
        stale = key == "last_reported" and isinstance(max_age, (int, float)) and max_age > 0 and seconds > max_age
        return f"{'⚠ ' if stale else ''}{label}: {age}" + (" · давно нет данных" if stale else "")
    return "Давность данных неизвестна"


def independent_lights(states):
    """Avoid commanding a light group and its explicit members twice.

    Prefer leaf targets when all group members are present. For incomplete graphs
    use the group and suppress known descendants; no name-based inference.
    """
    lights = {s['entity_id']: s for s in states if s.get('entity_id', '').startswith('light.')}
    selected = set(lights)
    for eid, state in lights.items():
        members = state.get('attributes', {}).get('entity_id', [])
        if not isinstance(members, list):
            continue
        members = {x for x in members if isinstance(x, str) and x != eid}
        if not members:
            continue
        if members <= lights.keys():
            selected.discard(eid)
        elif eid in selected:
            selected.difference_update(members)
    return [s for eid, s in lights.items() if eid in selected]
