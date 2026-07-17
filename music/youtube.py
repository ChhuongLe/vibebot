from __future__ import annotations

import yt_dlp

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "noplaylist": True,
}


def resolve_youtube(query: str) -> tuple[str, str]:
    """Resolve a YouTube URL or search query to (title, stream_url)."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)

    if info is None:
        raise ValueError("Could not find that track.")

    if "entries" in info:
        entries = info["entries"] or []
        if not entries:
            raise ValueError("Could not find that track.")
        info = entries[0]

    stream_url = info.get("url")
    if not stream_url:
        raise ValueError("Could not get an audio stream for that track.")

    title = info.get("title") or query
    return title, stream_url
