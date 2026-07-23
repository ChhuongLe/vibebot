"""LiveKit Audio Source Streaming (vendored, quality-tuned fork).

Vendored from livekit_simple_audio_source_streaming==0.1.2 instead of
importing the pip package, because that package hardcodes mono audio and
leaves the Opus publish bitrate unset (falling back to LiveKit's voice-chat
default, not a music-appropriate one). Pinned pip packages get overwritten
on every rebuild, so the fix lives here instead of patched in-place in
site-packages.

Main Classes:
    VoiceClient: Main client for streaming audio to a LiveKit room.
    FFmpegAudio: Audio source that streams audio via FFmpeg.
    AudioSourceBase: Abstract base class for audio sources.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import typing
from abc import ABC, abstractmethod

import livekit.rtc as rtc
from livekit.rtc._proto import room_pb2 as _room_pb2

if typing.TYPE_CHECKING:
    from livekit.rtc import Room

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_NUM_CHANNELS = 2
MUSIC_AUDIO_BITRATE = 128_000


class AudioSourceBase(ABC):
    """Abstract base class for audio sources.

    This class defines the interface that all audio source implementations
    must follow. Audio sources are used to create LiveKit audio tracks
    that can be published to a room.

    Subclasses must implement the create_track() and close() methods.
    """

    @abstractmethod
    async def create_track(self) -> rtc.LocalAudioTrack:
        """Create a LiveKit audio track from this audio source.

        This method should initialize any necessary resources (such as
        FFmpeg processes or audio decoders) and return a LocalAudioTrack
        that can be published to a LiveKit room.

        Returns:
            rtc.LocalAudioTrack: A LiveKit local audio track ready for publishing.

        Raises:
            RuntimeError: If the audio source has already been closed.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close and clean up the audio source.

        This method should release all resources associated with the
        audio source, such as closing FFmpeg processes, stopping
        background tasks, and freeing audio buffers.

        After calling this method, the audio source should no longer
        be used.
        """
        pass


AudioSource = AudioSourceBase
"""Type alias for AudioSourceBase, kept for backwards compatibility."""


class FFmpegAudio(AudioSourceBase):
    """Audio source that streams audio using FFmpeg.

    This class wraps an to decode FFmpeg process and stream audio from
    various sources (local files, URLs, streams) into a LiveKit room.

    The audio is read from FFmpeg's stdout in raw PCM format (s16le)
    and pushed to a LiveKit AudioSource as AudioFrame objects.

    Attributes:
        source: The audio source URL or file path.
        sample_rate: The sample rate for the audio (default: 48000).
        num_channels: The number of audio channels (default: 2 for stereo).

    Args:
        source: URL or file path to the audio source. Can be a local file,
            HTTP URL, or any format supported by FFmpeg.
        sample_rate: Output sample rate in Hz (default: 48000).
        num_channels: Number of output audio channels (default: 2 for stereo).
    """

    def __init__(
        self,
        source: str,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        num_channels: int = DEFAULT_NUM_CHANNELS,
    ) -> None:
        self._source = source
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._audio_source: typing.Optional[rtc.AudioSource] = None
        self._track: typing.Optional[rtc.LocalAudioTrack] = None
        self._process: typing.Optional[subprocess.Popen] = None
        self._task: typing.Optional[asyncio.Task] = None
        self._closed = False
        self._started = False

    async def create_track(self) -> rtc.LocalAudioTrack:
        """Create a LiveKit audio track from this FFmpeg audio source.

        Initializes the LiveKit AudioSource with the configured sample rate
        and channel count, then creates a LocalAudioTrack that can be
        published to a room.

        This method must be called before start(). The track is created
        in a paused state and begins streaming audio only after start()
        is called.

        Returns:
            rtc.LocalAudioTrack: A LiveKit local audio track ready for publishing.

        Raises:
            RuntimeError: If the audio source has already been closed.
        """
        self._audio_source = rtc.AudioSource(self._sample_rate, self._num_channels)
        self._track = rtc.LocalAudioTrack.create_audio_track(
            f"audio-{id(self)}", self._audio_source
        )
        return self._track

    async def start(self) -> None:
        """Start streaming audio from FFmpeg to the LiveKit room.

        Launches an FFmpeg process to decode the audio source and begins
        reading audio frames in a background task. The audio is pushed
        to the LiveKit AudioSource as AudioFrame objects at the configured
        sample rate.

        This method must be called after create_track() and after the
        track has been published to a room.

        Raises:
            RuntimeError: If the audio source has already been closed,
                or if create_track() has not been called yet.

        Note:
            The FFmpeg process runs in the background and continuously
            reads audio data. Use close() to properly terminate it.
        """
        if self._closed:
            raise RuntimeError("FFmpegAudio already closed")

        if self._started:
            return
        self._started = True

        ffmpeg_cmd = [
            "ffmpeg",
            "-i",
            self._source,
            "-ac",
            str(self._num_channels),
            "-ar",
            str(self._sample_rate),
            "-f",
            "s16le",
            "-",
        ]

        logger.info(f"Starting FFmpeg: {' '.join(ffmpeg_cmd)}")

        self._process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Internal: Read audio data from FFmpeg and push to LiveKit.

        This is an internal method that runs as a background task. It
        reads raw PCM data from FFmpeg's stdout and converts it to
        LiveKit AudioFrame objects, which are pushed to the AudioSource.

        The loop runs continuously until:
        - The audio source is closed
        - FFmpeg finishes outputting audio
        - An error occurs

        This method is automatically started when start() is called.
        """
        if self._process is None or self._audio_source is None:
            return

        bytes_per_sample = 2 * self._num_channels
        frame_size = self._sample_rate // 100 * bytes_per_sample
        logger.info(f"FFmpeg read loop started, frame_size={frame_size}")

        try:
            while True:
                if self._closed:
                    break

                stdout = self._process.stdout
                if stdout is None:
                    logger.error("FFmpeg stdout pipe not available")
                    return

                # stdout.read() is a blocking syscall; running it directly on
                # the event loop thread freezes the entire bot (heartbeats,
                # command handling, everything) the moment FFmpeg's read stalls
                # (e.g. a network hiccup on the source stream). Offload it.
                data = await asyncio.to_thread(stdout.read, frame_size)
                if not data:
                    logger.info("FFmpeg output ended")
                    break

                if len(data) != frame_size:
                    logger.warning(f"FFmpeg read incomplete: {len(data)}/{frame_size}")
                    continue

                frame = rtc.AudioFrame(
                    data=data,
                    sample_rate=self._sample_rate,
                    num_channels=self._num_channels,
                    samples_per_channel=self._sample_rate // 100,
                )
                await self._audio_source.capture_frame(frame)
        except Exception as e:
            logger.error(f"Error reading from FFmpeg: {e}")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the FFmpeg process and audio streaming.

        This method terminates the FFmpeg process gracefully. If the
        process doesn't terminate within 5 seconds, it is forcefully
        killed. Any FFmpeg stderr output is logged as a warning.

        Note:
            This method does not mark the audio source as closed.
            Use close() for full cleanup.
        """
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            stderr = self._process.stderr.read() if self._process.stderr else ""
            if stderr:
                logger.warning(f"FFmpeg stderr: {stderr.decode()[:500]}")
            self._process = None

    async def close(self) -> None:
        """Close and clean up the FFmpeg audio source.

        This method performs full cleanup:
        - Sets the closed flag to prevent new operations
        - Cancels the background read loop task
        - Stops the FFmpeg process
        - Clears all references to resources

        After calling this method, the FFmpegAudio instance can no
        longer be used.
        """
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.stop()
        self._audio_source = None
        self._track = None


class VoiceClient:
    """Client for streaming audio to a LiveKit room.

    This is the main class for playing audio in a LiveKit voice channel.
    It manages audio sources, tracks, and their publication to the room.

    The VoiceClient wraps a LiveKit Room and provides high-level methods
    for playing and stopping audio. Multiple audio sources can be managed
    simultaneously using named keys.

    Attributes:
        room: The LiveKit Room instance this client is connected to.

    Args:
        room: A connected LiveKit Room instance.
    """

    def __init__(self, room: Room) -> None:
        """Initialize the VoiceClient.

        Args:
            room: A connected LiveKit Room instance. The room should already
                be connected (e.g., via room.connect()).
        """
        self._room = room
        self._audio_sources: dict[str, FFmpegAudio] = {}
        self._published_tracks: set[str] = set()

        @room.on("track_published")
        def on_track_published(publication, participant):
            logger.info(
                f"Track published: {publication.sid}, is local: {participant.is_local()}"
            )
            if participant.is_local():
                self._published_tracks.add(publication.sid)

        @room.on("track_subscription_failed")
        def on_track_sub_failed(track_sid, participant, error):
            logger.error(
                f"Track subscription failed: track_sid={track_sid}, participant={participant}, error={error}"
            )

        @room.on("connection_state_changed")
        def on_connection_state_changed(state):
            logger.info(f"Connection state changed: {state}")

    @property
    def room(self) -> Room:
        """Get the LiveKit Room instance.

        Returns:
            Room: The LiveKit room this client is connected to.
        """
        return self._room

    async def play(self, source: str, name: str = "audio") -> FFmpegAudio:
        """Play audio from a source and publish it to the room.

        This method creates a new FFmpegAudio source, creates a track from
        it, publishes the track to the LiveKit room, and starts streaming
        the audio.

        If an audio source with the same name already exists, it will be
        stopped and replaced with the new one.

        The audio is published as a microphone source, which allows it to
        be heard by other participants in the room. It is published in
        stereo with an explicit music-appropriate Opus bitrate rather than
        LiveKit's voice-chat default.

        Args:
            source: URL or file path to the audio source. Can be any format
                supported by FFmpeg (MP3, WAV, OGG, streaming URLs, etc.).
            name: Identifier name for this audio source. Used to reference
                the source when stopping. Default: 'audio'.

        Returns:
            FFmpegAudio: The FFmpegAudio instance managing this playback.
                This can be used to control playback or monitor status.

        Raises:
            RuntimeError: If the room is not connected.
            Exception: If track publication fails. The audio source will
                be cleaned up before raising.
        """
        if name in self._audio_sources:
            await self._audio_sources[name].close()

        audio = FFmpegAudio(source)
        track = await audio.create_track()

        logger.info(f"Publishing track: {track.sid}")

        try:
            opts = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_MICROPHONE,
                audio_encoding=_room_pb2.AudioEncoding(max_bitrate=MUSIC_AUDIO_BITRATE),
            )
            publication = await asyncio.wait_for(
                self._room.local_participant.publish_track(track, opts), timeout=30.0
            )
            logger.info(f"Track published successfully: {publication.sid}")

            await audio.start()

        except Exception as e:
            logger.error(f"Failed to publish track: {e}, type: {type(e)}")
            import traceback

            traceback.print_exc()
            await audio.close()
            raise

        self._audio_sources[name] = audio
        return audio

    async def stop(self, name: str = "audio") -> None:
        """Stop playing audio from a specific audio source.

        This method stops the FFmpeg process, unpublishes the track from
        the room, and cleans up the audio source.

        Args:
            name: The name of the audio source to stop. Must match a
                previously passed to play(). Default: 'audio'.
        """
        if name in self._audio_sources:
            audio = self._audio_sources.pop(name)
            track = audio._track

            if track:
                try:
                    await self._room.local_participant.unpublish_track(track.sid)
                except Exception as e:
                    logger.warning(f"Error unpublishing track: {e}")

            await audio.close()

    async def stop_all(self) -> None:
        """Stop all currently playing audio sources.

        This method stops and cleans up all audio sources managed by this
        VoiceClient. After calling this method, no audio will be playing.
        """
        names = list(self._audio_sources.keys())
        for name in names:
            await self.stop(name)

    async def disconnect(self) -> None:
        """Stop all audio and disconnect from the room.

        This is a convenience method that calls stop_all() and then
        disconnects from the LiveKit room. After calling this method,
        the VoiceClient can no longer be used.
        """
        await self.stop_all()
        await self._room.disconnect()
