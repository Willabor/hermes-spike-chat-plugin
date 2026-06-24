"""Spike-chat platform adapter for Hermes Agent.

Lets a Hermes agent (e.g. Queen) join the spike-chat box as a first-class
platform: the gateway's WARM persistent session receives spike-chat messages
and replies back natively — no cold one-shot per message, tools registered
once. Mirrors the structure of the shipped Home Assistant / Mattermost
platform adapters.

Config (per-profile config.yaml, under agent.platforms.spike_chat):
    enabled: true
    extra:
      url: http://100.67.169.29:4040      # spike-chat backend
      member: Queen                        # this agent's spike-chat identity
      project: online-operations           # default project for outbound posts
      channel: order-ops                   # default channel
      role: order-ops                      # role badge
      poll_seconds: 3                       # feed poll interval
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE


class SpikeChatAdapter(BasePlatformAdapter):
    """Polls spike-chat's member feed and forwards new messages to the agent;
    replies are posted back to spike-chat as this agent."""

    MAX_MESSAGE_LENGTH = 16000

    def __init__(self, config: PlatformConfig):
        # spike_chat is not in the built-in Platform enum; RELAY is the generic
        # custom-adapter slot. self.platform is informational only.
        super().__init__(config, Platform.RELAY)

        extra = config.extra or {}
        self._base = (extra.get("url") or "http://100.67.169.29:4040").rstrip("/")
        self._member = extra.get("member") or "Queen"
        self._project = extra.get("project") or "online-operations"
        self._channel = extra.get("channel") or "order-ops"
        self._role = extra.get("role") or ""
        self._poll_s = float(extra.get("poll_seconds", 3))

        self._session: Optional["aiohttp.ClientSession"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._last_id = 0

    @property
    def name(self) -> str:  # base may already provide this; keep a stable label
        return "spike_chat"

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[spike_chat] aiohttp not installed")
            return False
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        # Start from the newest message so we don't replay history on connect.
        try:
            feed = await self._fetch_feed()
            self._last_id = max((int(m.get("id", 0)) for m in feed), default=0)
        except Exception:
            self._last_id = 0
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        logger.info(
            "[spike_chat] connected as %s -> %s/%s (base=%s)",
            self._member, self._project, self._channel, self._base,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
        if self._session:
            await self._session.close()
            self._session = None

    async def _fetch_feed(self) -> list:
        url = f"{self._base}/api/feed?member={self._member}&limit=15"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return []
            return await resp.json()

    async def _listen_loop(self) -> None:
        while self._running:
            try:
                feed = await self._fetch_feed()
                fresh = sorted(
                    (m for m in feed if int(m.get("id", 0)) > self._last_id),
                    key=lambda m: int(m.get("id", 0)),
                )
                for m in fresh:
                    self._last_id = max(self._last_id, int(m.get("id", 0)))
                    if self._should_handle(m):
                        await self._dispatch(m)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[spike_chat] listen loop error: %s", e)
            await asyncio.sleep(self._poll_s)

    def _should_handle(self, m: dict) -> bool:
        if m.get("agent") == self._member:
            return False  # never react to self
        text = (m.get("text") or "").strip()
        if not text:
            return False
        if f"@{self._member}".lower() in text.lower():
            return True  # explicit mention
        if m.get("kind") != "agent":
            return True  # any human message in our channels
        return False  # ignore other agents' chatter unless mentioned

    async def _dispatch(self, m: dict) -> None:
        text = m.get("text") or ""
        # spike-chat DM vs channel: project=="dm" => reply as DM to the sender.
        is_dm = m.get("project") == "dm"
        chat_id = ("dm:" + str(m.get("agent"))) if is_dm else (m.get("channel") or self._channel)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type="dm" if is_dm else "channel",
            user_id=str(m.get("agent") or "user"),
            user_name=str(m.get("agent") or "user"),
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"spike_{m.get('id')}",
            timestamp=datetime.now(),
            raw_message=m,
        )
        await self.handle_message(event)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        body: Dict[str, Any] = {
            "agent": self._member,
            "role": self._role,
            "color": "",
            "text": content[: self.MAX_MESSAGE_LENGTH],
        }
        if isinstance(chat_id, str) and chat_id.startswith("dm:"):
            body["project"] = "dm"
            body["to"] = chat_id[3:]
        else:
            body["project"] = self._project
            body["channel"] = chat_id or self._channel
        try:
            async with self._session.post(
                f"{self._base}/api/messages",
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status < 300:
                    j = await resp.json()
                    return SendResult(success=True, message_id=str(j.get("id", uuid.uuid4().hex[:12])))
                txt = await resp.text()
                return SendResult(success=False, error=f"HTTP {resp.status}: {txt[:200]}")
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": f"{self._project}/{chat_id}", "type": "channel", "url": self._base}


def _build_adapter(config: PlatformConfig) -> SpikeChatAdapter:
    return SpikeChatAdapter(config)


def _is_connected(config) -> bool:
    extra = config.extra or {}
    return bool(extra.get("url") and extra.get("member"))


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars during gateway config load.

    Called BEFORE adapter construction so env-only setups surface in
    `gateway status` / get_connected_platforms() without instantiating.
    Returns None when not minimally configured (caller skips auto-enable).
    Keys go into PlatformConfig.extra; the special `home_channel` key
    becomes a HomeChannel on the PlatformConfig.
    """
    url = os.getenv("SPIKE_CHAT_URL", "").strip()
    member = os.getenv("SPIKE_CHAT_MEMBER", "").strip()
    if not (url and member):
        return None
    seed: dict = {
        "url": url,
        "member": member,
        "project": os.getenv("SPIKE_CHAT_PROJECT", "online-operations").strip(),
        "channel": os.getenv("SPIKE_CHAT_CHANNEL", "order-ops").strip(),
        "role": os.getenv("SPIKE_CHAT_ROLE", "").strip(),
    }
    poll = os.getenv("SPIKE_CHAT_POLL_SECONDS", "").strip()
    if poll:
        try:
            seed["poll_seconds"] = float(poll)
        except ValueError:
            pass
    home = seed["channel"] or "order-ops"
    seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="spike_chat",
        label="Spike Chat",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        is_connected=_is_connected,
        required_env=[],
        install_hint="pip install aiohttp",
        # Env-driven config: SPIKE_CHAT_URL + SPIKE_CHAT_MEMBER (+ PROJECT,
        # CHANNEL, ROLE, POLL_SECONDS) seed PlatformConfig.extra, so the
        # platform enables without a built-in Platform enum value.
        env_enablement_fn=_env_enablement,
        max_message_length=SpikeChatAdapter.MAX_MESSAGE_LENGTH,
        emoji="\U0001F4AC",
        allow_update_command=True,
    )
