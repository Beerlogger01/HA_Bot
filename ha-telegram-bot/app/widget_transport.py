"""Bot API 10.4 methods over aiogram's existing authenticated HTTP session.

No second polling client or alternate endpoint. All four native methods are wrapped with local size validation and bounded timeouts.
"""
import asyncio
import time
import zlib
from aiogram.methods import SendWidget, AnswerWidgetQuery, EditMessageWidgetData, EditSharedWidgetData


def check_text(value: str, limit: int, *, empty: bool = True) -> str:
    size = len(value.encode("utf-8"))
    if "\0" in value or size > limit or (not empty and not size):
        raise ValueError(f"Invalid widget text (limit {limit} UTF-8 bytes)")
    return value


def validate_html(html: str) -> tuple[int, int]:
    raw = check_text(html, 32768, empty=False).encode("utf-8")
    packed = zlib.compress(raw, level=9)
    if len(packed) > 4096:
        raise ValueError("Widget compressed HTML exceeds 4096 bytes")
    return len(raw), len(packed)


class WidgetTransport:
    def __init__(self, bot):
        self.bot = bot
        self._shared_lock = asyncio.Lock()
        self._shared_at = {}

    async def send(self, **kwargs):
        validate_html(kwargs["html"])
        for key in ("chat_data", "public_data"):
            check_text(kwargs.get(key, ""), 1024)
        kwargs.setdefault("allow_queries", True)
        kwargs.setdefault("protect_content", True)
        result = await self.bot(SendWidget(**kwargs), request_timeout=5)
        return result.model_dump()

    async def answer(self, query_id, data=""):
        return await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=check_text(data, 1024)), request_timeout=3)

    async def edit(self, chat_id, message_id, *, chat_data=None, public_data=None):
        for value in (chat_data, public_data):
            if value is not None:
                check_text(value, 1024)
        return await self.bot(EditMessageWidgetData(chat_id=chat_id, message_id=message_id,
                                                    chat_data=chat_data, public_data=public_data), request_timeout=5)

    async def edit_shared(self, widget_id, public_data, cache_time=30):
        if not 5 <= cache_time <= 3600:
            raise ValueError("cache_time must be 5..3600")
        public_data = check_text(public_data, 1024)
        async with self._shared_lock:
            delay = 1 - (time.monotonic() - self._shared_at.get(widget_id, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._shared_at[widget_id] = time.monotonic()
            return await self.bot(EditSharedWidgetData(widget_id=widget_id,
                                public_data=public_data, cache_time=cache_time), request_timeout=5)
