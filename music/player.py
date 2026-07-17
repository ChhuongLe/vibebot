from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import stoat
from livekit_simple_audio_source_streaming import VoiceClient

from .youtube import resolve_youtube

logger = logging.getLogger(__name__)


@dataclass
class MusicPlayer:
    bot: stoat.Client
    server_id: str
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    voice_client: VoiceClient | None = None
    voice_channel: stoat.VoiceChannel | None = None
    node_name: str | None = None
    text_channel_id: str | None = None
    worker: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def join(self, channel: stoat.VoiceChannel, node_name: str) -> None:
        async with self.lock:
            if (
                self.voice_client
                and self.voice_channel
                and self.voice_channel.id == channel.id
            ):
                return

            await self._disconnect_unlocked()

            room = await channel.connect(node=node_name)
            self.voice_channel = channel
            self.node_name = node_name
            self.voice_client = VoiceClient(room)

    async def leave(self) -> None:
        async with self.lock:
            await self._disconnect_unlocked()
            self._clear_queue_unlocked()

    async def enqueue(self, query: str, text_channel_id: str) -> None:
        self.text_channel_id = text_channel_id
        await self.queue.put(query)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._worker())

    async def skip(self) -> bool:
        async with self.lock:
            if not self.voice_client:
                return False
            await self.voice_client.stop("music")
            return True

    async def stop(self) -> None:
        async with self.lock:
            self._clear_queue_unlocked()
            if self.voice_client:
                await self.voice_client.stop("music")

    async def _worker(self) -> None:
        while True:
            query = await self.queue.get()
            try:
                await self._play_query(query)
            except Exception:
                logger.exception("Failed to play %r", query)
                await self._send(f"Could not play `{query}`.")
            finally:
                self.queue.task_done()

            if self.queue.empty():
                break

    async def _play_query(self, query: str) -> None:
        if not self.voice_client:
            raise RuntimeError("Not connected to a voice channel.")

        title, stream_url = await asyncio.to_thread(resolve_youtube, query)
        await self._send(f"Now playing: **{title}**")

        audio = await self.voice_client.play(stream_url, "music")
        if audio._task:
            await audio._task

    async def _send(self, content: str) -> None:
        if not self.text_channel_id:
            return

        channel = self.bot.get_channel(self.text_channel_id)
        if channel is None:
            return

        await channel.send(content)

    async def _disconnect_unlocked(self) -> None:
        if self.worker and not self.worker.done():
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        self.worker = None

        if self.voice_client:
            await self.voice_client.disconnect()

        self.voice_client = None
        self.voice_channel = None
        self.node_name = None

    def _clear_queue_unlocked(self) -> None:
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break


def find_member_voice_channel(
    bot: stoat.Client,
    server_id: str,
    user_id: str,
) -> tuple[stoat.VoiceChannel | None, str | None]:
    for channel_id, container in bot.voice_states.items():
        if user_id not in container.participants:
            continue

        channel = bot.get_channel(channel_id)
        if not isinstance(channel, stoat.VoiceChannel):
            continue
        if channel.server_id != server_id:
            continue

        return channel, container.node

    return None, None


async def get_voice_node(bot: stoat.Client) -> str:
    instance = await bot.http.query_node()
    if not instance.features.voice.is_livekit():
        raise RuntimeError("This Stoat instance does not use LiveKit voice.")

    nodes = instance.features.voice.nodes
    if not nodes:
        raise RuntimeError("No voice nodes are available.")

    return nodes[0].name
