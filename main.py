import asyncio
import logging
import os
import re
import stoat
from dotenv import load_dotenv
from stoat.ext import commands
from jokes import random_dad_joke
from music.player import MusicPlayer, find_member_voice_channel, get_voice_node, parse_volume
from typing import Optional

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
HTTP_BASE = os.getenv("STOAT_HTTP_BASE", "https://chat.supawok.com/api")
WS_BASE = os.getenv("STOAT_WS_BASE", "wss://chat.supawok.com/ws")
MENTION_RE = re.compile(r"<@[A-z0-9]{26}>")
GREETINGS = {"hi", "hello", "hey", "howdy", "!hi"}

HELP_TEXT = (
    "**Music**\n"
    "`!play <song name or URL>` -- Queue a song. I'll join your voice channel first if I'm not already in one.\n"
    "`!queue` -- Show what's queued up next.\n"
    "`!skip` / `!next` -- Skip the current song and move to the next one in the queue.\n"
    "`!pause` -- Pause the current song.\n"
    "`!resume` -- Resume a paused song.\n"
    "`!stop` -- Stop playback and clear the whole queue.\n"
    "`!volume <level>` -- Set playback volume. Accepts a `0-10` scale (e.g. `!volume 7`) "
    "or a percentage (e.g. `!volume 70%`). Defaults to 10%.\n"
    "\n"
    "**Voice Channel**\n"
    "`!join` -- Join the voice channel you're currently in.\n"
    "`!leave` -- Leave the voice channel and clear the queue.\n"
    "(I'll also leave on my own after 10 minutes of nothing playing.)\n"
    "\n"
    "**Fun**\n"
    "`!dadjoke` -- Get a random dad joke.\n"
    "\n"
    "`!help` -- Show this message."
)


def message_text(message: stoat.Message) -> str:
    text = MENTION_RE.sub("", message.content).strip().lower()
    return text.rstrip("!?., ")


class VibeBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            token=TOKEN,
            bot=True,
            http_base=HTTP_BASE,
            websocket_base=WS_BASE,
        )
        self.music_players: dict[str, MusicPlayer] = {}

    def get_player(self, server_id: str) -> MusicPlayer:
        player = self.music_players.get(server_id)
        if player is None:
            player = MusicPlayer(bot=self, server_id=server_id)  # real dataclass from music/player.py
            self.music_players[server_id] = player
        return player

    async def on_command_error(self, ctx, error=None) -> None:
        if error is None:
            logging.error(f"Command error occurred in {ctx}")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`")
            return
        if isinstance(error, commands.CommandNotFound):
            return
        logging.exception("Command error in %s", ctx.command, exc_info=error)
        await ctx.send("Something went wrong running that command.")


bot = VibeBot()


@bot.listen("ready")
async def on_ready() -> None:
    print(f"Logged in as {bot.me.name}!")


@bot.listen()
async def on_message_create(event: stoat.MessageCreateEvent) -> None:
    message = event.message
    if message.author_id == bot.me.id:
        return
    if message.content.startswith("!"):
        return
    text = message_text(message)
    mentioned = bot.me and bot.me.id in message.mentions
    if text in GREETINGS or (mentioned and not text):
        await message.channel.send("Hi! 👋")


@bot.command(name="play")
async def play(ctx: commands.Context, *, query: str) -> None:
    if ctx.server is None:
        await ctx.send("Music commands only work inside a server.")
        return

    player = bot.get_player(ctx.server.id)

    # 1. Already connected somewhere? Use that, no need to look up the user.
    voice_channel = player.voice_channel
    node_name = player.node_name

    # 2. Not connected yet -> find the user's current voice channel + its node.
    if voice_channel is None:
        voice_channel, node_name = find_member_voice_channel(bot, ctx.server.id, ctx.author_id)

    # 3. Final check
    if voice_channel is None:
        await ctx.send("I'm not in a channel and I can't find you in one. Join a voice channel first.")
        return

    # 4. Join (no-op if already connected to this channel) and enqueue
    await player.join(voice_channel, node_name)
    await player.enqueue(query, ctx.channel.id)
    await ctx.send(f"Queued: `{query}`")


@bot.command(name="skip")
async def skip(ctx: commands.Context) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    if await player.skip():
        await ctx.send("Skipped.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="stop")
async def stop(ctx: commands.Context) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    await player.stop()
    await ctx.send("Stopped playback and cleared the queue.")


@bot.command(name="next")
async def next_song(ctx: commands.Context) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    if await player.skip():
        await ctx.send("Playing next song.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="pause")
async def pause(ctx: commands.Context) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    if await player.pause():
        await ctx.send("Paused.")
    else:
        await ctx.send("Nothing to pause.")


@bot.command(name="resume")
async def resume(ctx: commands.Context) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    if await player.resume():
        await ctx.send("Resumed.")
    else:
        await ctx.send("Nothing to resume.")


@bot.command(name="volume")
async def volume(ctx: commands.Context, *, level: str) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    try:
        percent = parse_volume(level)
    except ValueError as e:
        await ctx.send(str(e))
        return
    await player.set_volume(percent)
    await ctx.send(f"Volume set to {percent}%.")


@bot.command(name="dadjoke")
async def dadjoke(ctx: commands.Context) -> None:
    await ctx.send(random_dad_joke())


@bot.command(name="leave")
async def leave(ctx: commands.Context) -> None:
    if ctx.server is None:
        return
    player = bot.get_player(ctx.server.id)
    await player.leave()
    await ctx.send("Left the voice channel.")


@bot.command(name="join")
async def join(ctx: commands.Context) -> None:
    """
    Joins the voice channel the invoking user is currently in.
    Uses the real find_member_voice_channel() from music/player.py, which
    returns (channel, node) together by checking bot.voice_states.
    (Join-by-name/ID for mods is deliberately deferred until this is solid.)
    """
    if ctx.server is None:
        await ctx.send("Music commands only work inside a server.")
        return

    channel, node_name = find_member_voice_channel(bot, ctx.server.id, ctx.author_id)

    if channel is None:
        await ctx.send("I can't find you in a voice channel. Join one first!")
        return

    try:
        player = bot.get_player(ctx.server.id)
        await player.join(channel, node_name)
        await ctx.send(f"Joined **{channel.name}**.")
    except Exception as e:
        await ctx.send(f"Failed to join: {e}")
        logging.exception("Error joining channel")


@bot.command(name="queue")
async def queue(ctx: commands.Context) -> None:
    player = bot.get_player(ctx.server.id)

    # The real MusicPlayer only exposes an asyncio.Queue, not a plain list,
    # so we peek at its internal deque to display contents without
    # consuming them. (asyncio.Queue has no public "peek all" API.)
    items = list(player.queue._queue)

    if not items:
        await ctx.send("The queue is empty.")
        return

    queue_msg = "**Current Queue:**\n"
    for i, song in enumerate(items, 1):
        queue_msg += f"{i}. {song}\n"

    await ctx.send(queue_msg)


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.send(HELP_TEXT)


@bot.command(name="debug_methods")
async def debug_methods(ctx: commands.Context) -> None:
    player = bot.get_player(ctx.server.id)
    methods = [method for method in dir(player) if callable(getattr(player, method)) and not method.startswith("__")]
    await ctx.send(f"Available methods on player: {', '.join(methods)}")


@bot.command(name="debug_user")
async def debug_player(ctx: commands.Context) -> None:
    player = bot.get_player(ctx.server.id)
    await ctx.send(f"Player: {player.__dict__}")


@bot.command(name="debug_voice")
async def debug_voice(ctx: commands.Context) -> None:
    """TEMPORARY: lists every channel in the server that has voice_states,
    plus its participants, so we can confirm resolve_member_voice_channel()
    is scanning/matching correctly. Remove once !join is solid."""
    lines = []
    for channel in ctx.server.channels:
        vs = getattr(channel, "voice_states", None)
        if vs is None:
            continue
        participants = getattr(vs, "participants", None) or []
        pids = [getattr(p, "id", p) for p in participants]
        lines.append(f"{channel.name} ({channel.id}): participants={pids}")
    if not lines:
        await ctx.send("No channels with voice_states found in this server.")
        return
    await ctx.send("**Voice channels:**\n" + "\n".join(lines) + f"\n\nYour ID: `{ctx.author_id}`")


@bot.command(name="debug_bot_voice")
async def debug_bot_voice(ctx: commands.Context) -> None:
    """TEMPORARY: inspects bot.voice_states directly — this is exactly what
    find_member_voice_channel() reads from, so if !join can't find you,
    this tells us whether the dict is empty, has the wrong keys/shape, or
    just doesn't contain your ID."""
    vs = getattr(bot, "voice_states", None)
    if vs is None:
        await ctx.send("bot.voice_states does not exist (None/missing attribute).")
        return
    if not vs:
        await ctx.send(f"bot.voice_states exists but is empty: {vs!r}")
        return

    lines = [f"bot.voice_states has {len(vs)} entries:"]
    for channel_id, container in vs.items():
        participants = getattr(container, "participants", "<no participants attr>")
        node = getattr(container, "node", "<no node attr>")
        lines.append(f"  channel_id={channel_id} node={node} participants={participants}")
    lines.append(f"\nYour author_id: `{ctx.author_id}`")
    lines.append(f"Your server.id: `{ctx.server.id}`")
    await ctx.send("\n".join(lines))


@bot.command(name="debug_channel_lookup")
async def debug_channel_lookup(ctx: commands.Context) -> None:
    """TEMPORARY: reproduces the exact checks inside find_member_voice_channel
    for the one channel_id in bot.voice_states, to see which check fails."""
    vs = getattr(bot, "voice_states", None)
    if not vs:
        await ctx.send("bot.voice_states is empty, nothing to check.")
        return

    lines = []
    for channel_id, container in vs.items():
        channel = bot.get_channel(channel_id)
        lines.append(f"channel_id={channel_id}")
        lines.append(f"  bot.get_channel() -> {channel!r} (type={type(channel).__name__})")
        lines.append(f"  isinstance(channel, stoat.VoiceChannel) -> {isinstance(channel, stoat.VoiceChannel)}")
        if channel is not None:
            lines.append(f"  channel.server_id -> {getattr(channel, 'server_id', '<no server_id attr>')!r}")
        lines.append(f"  ctx.server.id -> {ctx.server.id!r}")

    await ctx.send("\n".join(lines))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # This library logs an INFO line for every single audio frame pushed
    # (~every 10-20ms during playback), which floods the console. Silence
    # it specifically rather than dropping INFO logging globally.
    logging.getLogger("livekit_simple_audio_source_streaming").setLevel(logging.WARNING)
    bot.run()


if __name__ == "__main__":
    main()