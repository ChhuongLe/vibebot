from __future__ import annotations

import array
import asyncio
import logging
import random
import signal
import time
from dataclasses import dataclass, field

import stoat
from livekit import rtc
from .voice import FFmpegAudio, VoiceClient

from .youtube import resolve_youtube

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 600

IDLE_LEAVE_MESSAGES = [
    "No one's queued anything in a while, so I'm heading out. Catch you later!",
    "Ten minutes of silence is my cue to leave -- see you next time!",
    "Nothing queued, nobody around -- I'm gonna dip. `!play` to bring me back.",
    "Guess the party's over. Leaving the channel for now.",
    "I'll take the quiet as a sign to go. Ping me with `!play` whenever.",
    "Idle timeout reached -- stepping out until someone queues a song.",
    "No tunes, no reason to stick around. Peace out!",
]

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


def format_duration(seconds: float) -> str:
    """Format a number of seconds as `M:SS` (or `H:MM:SS` past an hour)."""
    total = int(max(0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


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
    volume: int = 10
    idle_task: asyncio.Task | None = None
    loop_mode: str = "off"  # "off", "song", or "queue"
    current_title: str | None = None
    current_duration: float | None = None
    current_started_at: float | None = None
    paused_accum: float = 0.0
    pause_started_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def join(self, channel: stoat.VoiceChannel, node_name: str) -> None:
        async with self.lock:
            if (
                self.voice_client
                and self.voice_channel
                and self.voice_channel.id == channel.id
            ):
                self._schedule_idle_timer()
                return

            await self._disconnect_unlocked()

            room = await channel.connect(node=node_name)
            self.voice_channel = channel
            self.node_name = node_name
            self.voice_client = VoiceClient(room)
            self._schedule_idle_timer()

    async def leave(self) -> None:
        async with self.lock:
            await self._disconnect_unlocked()
            self._clear_queue_unlocked()

    async def enqueue(self, query: str, text_channel_id: str) -> None:
        self.text_channel_id = text_channel_id
        self._cancel_idle_timer()
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
            self.loop_mode = "off"
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
            self.pause_started_at = time.monotonic()
            return True

    async def resume(self) -> bool:
        async with self.lock:
            if not self.paused or not self.current_audio or not self.current_audio._process:
                return False
            self.current_audio._process.send_signal(signal.SIGCONT)
            self.paused = False
            if self.pause_started_at is not None:
                self.paused_accum += time.monotonic() - self.pause_started_at
                self.pause_started_at = None
            return True

    async def set_volume(self, percent: int) -> None:
        async with self.lock:
            self.volume = percent

    async def set_loop_mode(self, mode: str) -> None:
        async with self.lock:
            self.loop_mode = mode

    async def remove(self, index: int) -> str | None:
        """Remove a 1-based position from the queue. Returns the removed query, or None if invalid."""
        async with self.lock:
            items = self.queue._queue
            if index < 1 or index > len(items):
                return None
            removed = items[index - 1]
            del items[index - 1]
            return removed

    async def clear_queue(self) -> int:
        """Empty the upcoming queue without touching the currently playing song. Returns count removed."""
        async with self.lock:
            count = self.queue.qsize()
            self._clear_queue_unlocked()
            return count

    def elapsed_seconds(self) -> float | None:
        if self.current_started_at is None:
            return None
        now = time.monotonic()
        still_paused = (now - self.pause_started_at) if self.pause_started_at is not None else 0.0
        return now - self.current_started_at - self.paused_accum - still_paused

    async def _worker(self) -> None:
        while True:
            query = await self.queue.get()
            succeeded = True
            try:
                while True:
                    ended_naturally = await self._play_query(query)
                    if ended_naturally and self.loop_mode == "song":
                        continue
                    break
            except Exception:
                logger.exception("Failed to play %r", query)
                await self._send(f"Could not play `{query}`.")
                succeeded = False
            finally:
                self.queue.task_done()

            # Only re-add on success -- looping a broken query forever would
            # just spam "Could not play" every lap.
            if succeeded and self.loop_mode == "queue":
                await self.queue.put(query)

            if self.queue.empty():
                self._schedule_idle_timer()
                break

    async def _play_query(self, query: str) -> bool:
        """Play one track. Returns True if it ended naturally (not skipped/stopped)."""
        if not self.voice_client:
            raise RuntimeError("Not connected to a voice channel.")

        title, stream_url, duration = await asyncio.to_thread(resolve_youtube, query)
        await self._send(f"Now playing: **{title}**")

        audio = await self.voice_client.play(stream_url, "music")
        _attach_volume(audio, self)
        self.current_audio = audio
        self.current_title = title
        self.current_duration = duration
        self.current_started_at = time.monotonic()
        self.paused_accum = 0.0
        self.pause_started_at = None
        self.paused = False
        ended_naturally = True
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
                    ended_naturally = False
        finally:
            self.current_audio = None
            self.current_title = None
            self.current_duration = None
            self.current_started_at = None
            self.pause_started_at = None
            self.paused = False
        return ended_naturally

    async def _send(self, content: str) -> None:
        if not self.text_channel_id:
            return

        channel = self.bot.get_channel(self.text_channel_id)
        if channel is None:
            return

        await channel.send(content)

    def _cancel_idle_timer(self) -> None:
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None

    def _schedule_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self.idle_task = asyncio.create_task(self._idle_timeout())

    async def _idle_timeout(self) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return

        self.idle_task = None
        async with self.lock:
            if not self.voice_client:
                return
            await self._disconnect_unlocked()
            self._clear_queue_unlocked()
        await self._send(random.choice(IDLE_LEAVE_MESSAGES))

    async def _disconnect_unlocked(self) -> None:
        self._cancel_idle_timer()
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
        self.loop_mode = "off"
        self.current_title = None
        self.current_duration = None
        self.current_started_at = None
        self.paused_accum = 0.0
        self.pause_started_at = None

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