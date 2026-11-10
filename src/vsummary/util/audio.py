"""Shared yt-dlp options and size limit for NotebookLM audio sources."""

from pathlib import Path

MAX_AUDIO_BYTES = 190 * 1024**2


def audio_options(output_template: Path | None = None) -> dict:
    """Build yt-dlp options, adding M4A conversion when given an output template."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
    }
    if output_template is not None:
        options["outtmpl"] = str(output_template)
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "64",
            }
        ]
    return options
