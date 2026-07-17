from __future__ import annotations

import array
import asyncio
import logging
import signal
from dataclasses import dataclass, field

import stoat
from livekit import rtc
from livekit_simple_audio_source_streaming import FFmpegAudio, VoiceClient

from .youtube import resolve_youtube

logger = logging.getLogger(__name__)

# livekit-simple-audio-source-streaming's VoiceClient calls participant.is_local()
# from its "track_published" handler, but livekit-python dropped that method
# (it's not present on Participant/RemoteParticipant/LocalParticipant in the
# installed SDK). Without this shim, any track publish in the room -- ours or
# another participant's -- crashes that event handler.
if not hasattr(rtc.Participant, "is_local"):
    rtc.Participant.is_local = lambda self: isinstance(self, rtc.LocalParticipant)


def parse_volume(text: str) -> int:
    """Parse a `!volume` argument into a 0-100 percentage.

    Accepts either the 0-10 scale (`10` == 100%) or an explicit
    percentage (`30%` == 30%). Out-of-range values are clamped.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("Provide a volume level, e.g. `!volume 7` or `!volume 70%`.")

    is_percent = raw.endswith("%")
    number = raw[:-1].strip() if is_percent else raw

    try:
        value = float(number)
    except ValueError:
        raise ValueError(f"`{text}` is not a valid volume.") from None

    percent = value if is_percent else value * 10
    return round(max(0.0, min(100.0, percent)))


def _scale_pcm(data: bytes, gain: float) -> bytes:
    samples = array.array("h")
    samples.frombytes(data)
    for i, sample in enumerate(samples):
        samples[i] = max(-32768, min(32767, int(sample * gain)))
    return samples.tobytes()


def _attach_volume(audio: FFmpegAudio, player: MusicPlayer) -> None:
    # FFmpegAudio/VoiceClient don't expose a volume knob, so we scale the
    # PCM samples ourselves between the ffmpeg read loop and LiveKit by
    # wrapping the underlying AudioSource.capture_frame.
    audio_source = audio._audio_source
    original_capture = audio_source.capture_frame

    async def capture_frame(frame: rtc.AudioFrame) -> None:
        gain = player.volume / 100
        if gain != 1.0:
            frame = rtc.AudioFrame(
                _scale_pcm(bytes(frame.data), gain),
                frame.sample_rate,
                frame.num_channels,
                frame.samples_per_channel,
            )
        await original_capture(frame)

    audio_source.capture_frame = capture_frame


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
    current_audio: object | None = None
    paused: bool = False
    volume: int = 100
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
            # If the track is paused, the ffmpeg process is stopped via
            # SIGSTOP and won't respond to voice_client.stop() cleanly, so
            # resume it first to let the normal stop/cancel path run.
            if self.paused and self.current_audio and self.current_audio._process:
                self.current_audio._process.send_signal(signal.SIGCONT)
                self.paused = False
            await self.voice_client.stop("music")
            return True

    async def stop(self) -> None:
        async with self.lock:
            self._clear_queue_unlocked()
            if self.paused and self.current_audio and self.current_audio._process:
                self.current_audio._process.send_signal(signal.SIGCONT)
                self.paused = False
            if self.voice_client:
                await self.voice_client.stop("music")

    async def pause(self) -> bool:
        async with self.lock:
            if self.paused or not self.current_audio or not self.current_audio._process:
                return False
            self.current_audio._process.send_signal(signal.SIGSTOP)
            self.paused = True
            return True

    async def resume(self) -> bool:
        async with self.lock:
            if not self.paused or not self.current_audio or not self.current_audio._process:
                return False
            self.current_audio._process.send_signal(signal.SIGCONT)
            self.paused = False
            return True

    async def set_volume(self, percent: int) -> None:
        async with self.lock:
            self.volume = percent

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
        _attach_volume(audio, self)
        self.current_audio = audio
        self.paused = False
        try:
            if audio._task:
                try:
                    await audio._task
                except asyncio.CancelledError:
                    # skip()/stop() cancel this specific track's read-loop task
                    # (via VoiceClient.stop -> FFmpegAudio.close -> self._task.cancel()).
                    # Since we're also awaiting that same task here, its
                    # cancellation would otherwise propagate straight out of
                    # _play_query and, uncaught by _worker's `except Exception`
                    # (CancelledError is a BaseException, not an Exception),
                    # kill the entire worker task -- silently stalling the
                    # queue forever, since enqueue() only spawns a new worker
                    # when the old one is .done(), and nothing else ever
                    # notices this specific track was only *skipped*, not that
                    # the whole player broke. Treat it as "this track ended
                    # early" and let the worker loop move on to the next song.
                    pass
        finally:
            self.current_audio = None
            self.paused = False

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
        self.current_audio = None
        self.paused = False

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
) -> tuple[stoat.TextChannel | None, str | None]:
    for channel_id, container in bot.voice_states.items():
        if user_id not in container.participants:
            continue

        channel = bot.get_channel(channel_id)
        if channel is None:
            continue

        # NOTE: fixed bug — voice-capable channels in this Stoat instance
        # are TextChannel objects carrying non-None .voice metadata, not a
        # distinct stoat.VoiceChannel type. The old isinstance() check
        # against stoat.VoiceChannel never matched, so this function always
        # returned (None, None) even when bot.voice_states had a real entry.
        if getattr(channel, "voice", None) is None:
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