from __future__ import annotations

DISCORD_MESSAGE_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into non-empty Discord-sized chunks without losing content."""
    if not text:
        return []
    if limit <= 0:
        raise ValueError("limit must be positive")

    chunks: list[str] = []
    remaining = text.lstrip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            punctuation = max(
                remaining.rfind(". ", 0, limit + 1),
                remaining.rfind("! ", 0, limit + 1),
                remaining.rfind("? ", 0, limit + 1),
            )
            split_at = punctuation + 1 if punctuation > 0 else limit

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks


async def send_chunked(sender, text: str, **kwargs) -> None:
    """Send every non-empty chunk of ``text`` through an async sender."""
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        await sender(chunk, **kwargs if index == 0 else {})
