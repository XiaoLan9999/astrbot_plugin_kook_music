"""Lifecycle-safe interception of AstrBot's KOOK gateway callback."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from astrbot.api import logger


def event_field(value, name: str, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def enum_value(value):
    return getattr(value, "value", value)


@dataclass
class _Binding:
    client: Any
    original: Callable
    token: str
    callback: Callable | None = None
    active: bool = True


class KookEventBridge:
    def __init__(self, on_event):
        self.on_event = on_event
        self._bindings: dict[int, _Binding] = {}

    def sync(self, platforms):
        """Install after cold start and repair replaced clients/callbacks."""
        clients = {}
        for platform in platforms:
            if platform.meta().name != "kook":
                continue
            client = getattr(platform, "client", None)
            if client is None or not callable(getattr(client, "event_callback", None)):
                continue
            token = str(platform.config.get("kook_bot_token", "") or "").strip()
            clients[id(client)] = (client, token)

        for key, binding in list(self._bindings.items()):
            if key not in clients or binding.client.event_callback is not binding.callback:
                self._release(binding)
                self._bindings.pop(key)

        for key, (client, token) in clients.items():
            if key in self._bindings:
                self._bindings[key].token = token
                continue
            binding = _Binding(client, client.event_callback, token)

            async def callback(event, _binding=binding):
                if _binding.active:
                    try:
                        await self.on_event(_binding.client, event, _binding.token)
                    except Exception as exc:
                        logger.error(f"[KookMusic] KOOK system event handler failed: {type(exc).__name__}")
                return await _binding.original(event)

            binding.callback = callback
            client.event_callback = callback
            self._bindings[key] = binding
            logger.info("[KookMusic] 已安装 KOOK 按钮及语音退出事件监听")

    @staticmethod
    def _release(binding):
        # Another plugin may retain our wrapper: leave it as a pure passthrough.
        binding.active = False
        if binding.client.event_callback is binding.callback:
            binding.client.event_callback = binding.original

    def close(self):
        for binding in self._bindings.values():
            self._release(binding)
        self._bindings.clear()
