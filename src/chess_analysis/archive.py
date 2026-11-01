"""Raw monthly archives, saved exactly as chess.com returns them.

These files are the source of truth: everything else is rebuilt from them,
so the API only needs to be asked for months we don't have yet.
"""

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .api import ChessComClient, NotFound


def _log(message: str) -> None:
    """Progress output that appears immediately even when redirected to a file."""
    print(message, flush=True)


def month_key(archive_url: str) -> str:
    """".../games/2026/10" -> "2026-10"."""
    year, month = archive_url.rstrip("/").split("/")[-2:]
    return f"{year}-{month}"


def cached_months(raw_dir: Path) -> set[str]:
    return {p.stem for p in raw_dir.glob("*.json")}


def months_to_fetch(archive_urls: list[str], cached: set[str], refresh_all: bool = False) -> list[str]:
    """Archive URLs that need downloading.

    A finished month never changes, so cached months are skipped, except the
    latest cached one and anything after it: that month may have been saved
    before it was over.
    """
    if refresh_all or not cached:
        return list(archive_urls)
    latest = max(cached)
    return [url for url in archive_urls if month_key(url) not in cached or month_key(url) >= latest]


def save_month(raw_dir: Path, key: str, data: dict) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{key}.json"
    tmp = path.with_suffix(".json.tmp")
    # Pretty-printed so git diffs show which games were added.
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_months(raw_dir: Path) -> Iterator[tuple[str, dict]]:
    """(month key, archive JSON) for every saved month, oldest first."""
    for path in sorted(raw_dir.glob("*.json")):
        yield path.stem, json.loads(path.read_text(encoding="utf-8"))


def download(
    client: ChessComClient,
    username: str,
    raw_dir: Path,
    refresh_all: bool = False,
    log: Callable[[str], None] = _log,
) -> int:
    """Save any missing or possibly-incomplete months. Returns how many were downloaded."""
    try:
        client.player(username)
    except NotFound:
        raise NotFound(f"chess.com user {username!r} not found") from None
    urls = months_to_fetch(client.archive_urls(username), cached_months(raw_dir), refresh_all)
    for url in urls:
        key = month_key(url)
        data = client.month(url)
        save_month(raw_dir, key, data)
        log(f"  {key}: {len(data.get('games', []))} games")
    return len(urls)
