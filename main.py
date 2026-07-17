import asyncio
import logging
import os
import re

import stoat
from dotenv import load_dotenv
from stoat.ext import commands

from music.player import MusicPlayer, find_member_voice_channel, get_voice_node

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
HTTP_BASE = os.getenv("STOAT_HTTP_BASE", "https://chat.supawok.com/api")
WS_BASE = os.getenv("STOAT_WS_BASE", "wss://chat.supawok.com/ws")

MENTION_RE = re.compile(r"<@[A-z0-9]{26}>")
GREETINGS = {"hi", "hello", "hey", "howdy", "!hi"}


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
            player = MusicPlayer(bot=self, server_id=server_id)
            self.music_players[server_id] = player
        return player

    # Change the signature to make 'error' optional
    async def on_command_error(self, ctx, error=None) -> None:
        # If error is None, it means the library might have passed 
        # an event object as 'ctx', or we just don't have the error.
        # We can try to handle the standard cases.
        
        if error is None:
            # Fallback if no error was passed
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
    
    # 1. Use the attribute we found earlier: 'voice_channel'
    voice_channel = getattr(player, 'voice_channel', None)

    # 2. Fallback: If not connected, try to find the user
    if voice_channel is None:
        member = ctx.server.get_member(ctx.author_id)
        if member and hasattr(member, 'voice_states') and member.voice_states:
            channel_id = getattr(member.voice_states, 'channel_id', None)
            if channel_id:
                voice_channel = bot.get_channel(channel_id)

    # 3. Final check
    if voice_channel is None:
        await ctx.send("I'm not in a channel and I can't find you in one. Use `!join <channel_id>` first.")
        return

    # 4. Join and play
    node_name = getattr(voice_channel, 'voice_states', None)
    node_name = node_name.node if node_name else await get_voice_node(bot)

    await player.join(voice_channel, node_name)
    
    # Removed the crashing 'bot.voice_update' line.
    
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


@bot.command(name="leave")
async def leave(ctx: commands.Context) -> None:
    if ctx.server is None:
        return

    player = bot.get_player(ctx.server.id)
    await player.leave()
    await ctx.send("Left the voice channel.")

@bot.command(name="join")
async def join(ctx: commands.Context, channel_id: str) -> None:
    if ctx.server is None:
        await ctx.send("Music commands only work inside a server.")
        return

    channel = bot.get_channel(channel_id)
    
    # 1. Check if the channel exists
    if channel is None:
        await ctx.send(f"I can't see a channel with ID `{channel_id}`. Are you sure I have permission to view it?")
        return

    # 2. Relaxed check: Does it have voice capabilities?
    # Instead of 'isinstance(channel, stoat.VoiceChannel)', 
    # we check if it has a 'voice_states' attribute.
    if not hasattr(channel, "voice_states"):
        await ctx.send(f"I found the channel, but it doesn't appear to support voice (no voice_states).")
        return

    if channel.server_id != ctx.server.id:
        await ctx.send("That voice channel is not in this server.")
        return

    # 3. Join attempt
    try:
        # Use existing voice_states if available, otherwise get a new node
        node_name = channel.voice_states.node if channel.voice_states.participants else await get_voice_node(bot)
        player = bot.get_player(ctx.server.id)
        
        await player.join(channel, node_name)
        await ctx.send(f"Joined **{channel.name}**.")
    except Exception as e:
        await ctx.send(f"Failed to join: {e}")
        logging.exception("Error joining channel")

@bot.command(name="debug_voice")
async def debug_voice(ctx: commands.Context) -> None:
    # Check if the bot sees your voice state
    member = ctx.server.get_member(ctx.author_id)
    if member and member.voice_states:
        await ctx.send(f"I see you! Voice state found: {member.voice_states}")
    else:
        await ctx.send("I don't see you in a voice channel. My cache might be outdated.")

@bot.command(name="debug_user")
async def debug_player(ctx: commands.Context) -> None:
    player = bot.get_player(ctx.server.id)
    # This will print the contents of your player object to the chat
    await ctx.send(f"Player: {player.__dict__}")

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot.run()


if __name__ == "__main__":
    main()