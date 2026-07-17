# vibebot

A voice-channel DJ for [Stoat](https://chat.supawok.com) that takes song requests and doesn't talk back — unless something breaks, in which case it will absolutely let you know.

Give it a `!play`, it finds the track on YouTube, hops into your voice channel, and streams it straight into the call over LiveKit. Queue up the next ten, skip the ones that were a mistake, and dial the volume in exactly how you like it.

## Features

- **YouTube playback** — search by name or drop a link, `yt-dlp` resolves it
- **Real queueing** — songs play in order, back to back, no babysitting required
- **Volume control** — 0–100%, adjustable live, mid-track
- **Auto-join** — finds whatever voice channel you're sitting in, no channel ID required
- **Per-server state** — every server gets its own independent player and queue

## Commands

| Command | What it does |
|---|---|
| `!play <song or url>` | Queue a track. Joins your voice channel if not already connected. |
| `!skip` | Skip the current track. |
| `!stop` | Stop playback and clear the queue. |
| `!queue` | Show what's queued up. |
| `!volume <level>` | Set playback volume. See below. |
| `!join` | Join the voice channel you're currently in. |
| `!leave` | Leave the voice channel. |

### Setting the volume

`!volume` accepts two formats:

```
!volume 0     -> 0%
!volume 7     -> 70%
!volume 10    -> 100%
!volume 30%   -> 30%
```

Plain numbers are read on a 0–10 dial; add a `%` if you want to set an exact percentage instead. Anything out of range gets clamped to 0–100%, so you can't blow anyone's eardrums out by accident.

## Setup

### Requirements

- Python 3.12+
- `ffmpeg` on your `PATH`
- A Stoat bot token

### Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```
BOT_TOKEN=your-bot-token-here
STOAT_HTTP_BASE=https://chat.supawok.com/api
STOAT_WS_BASE=wss://chat.supawok.com/ws
```

Only `BOT_TOKEN` is required — the two base URLs default to the values above if you leave them out.

### Run

```bash
python main.py
```

## Docker

Prefer containers? So does the `Dockerfile`:

```bash
docker compose up -d --build
```

It reads the same `.env` file, so set that up first.

## License

ISC
