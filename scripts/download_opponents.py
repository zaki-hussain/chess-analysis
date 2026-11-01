"""Download the full game archives of the player's most frequent opponents.

Step 2 of Q12 (style fingerprint) needs games of ~200 opponents against
third parties. This is a serial, rate-limited job (one request at a time,
as chess.com requires); run it in the background:

    .venv/bin/python scripts/download_opponents.py --count 200 > /tmp/opponents.log 2>&1 &

Archives land in data/opponents/<name>/raw/YYYY-MM.json, the same layout as
the player's own, so `dataset.build_tables()` can read them. Opponents whose
account no longer exists are skipped and listed at the end.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

from chess_analysis.api import ApiError, ChessComClient, NotFound
from chess_analysis.archive import download
from chess_analysis.config import load_settings
from chess_analysis.dataset import load_my_games
from chess_analysis.toolkit import repo_root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", type=int, default=200, help="how many opponents, most frequent first")
    parser.add_argument("--username", default=None)
    args = parser.parse_args()

    settings = load_settings(args.username)
    games = load_my_games(args.username)
    games = games[(games["rules"] == "chess") & (games["time_class"] != "daily")]
    counts = games["opponent"].str.lower().value_counts()
    opponents = list(counts.index[: args.count])
    out_root = repo_root() / "data" / "opponents"
    client = ChessComClient()
    missing: list[str] = []
    started = time.time()
    for i, name in enumerate(opponents, 1):
        raw_dir = out_root / name / "raw"
        print(f"[{i}/{len(opponents)}] {name} ({counts[name]} games vs {settings.username})", flush=True)
        try:
            n = download(client, name, raw_dir, log=lambda m: print(m, flush=True))
            print(f"  {n} months downloaded ({time.time() - started:.0f}s elapsed)", flush=True)
        except NotFound:
            print("  not found (account closed?)", flush=True)
            missing.append(name)
        except (ApiError, requests.RequestException) as exc:
            # A failed opponent is skipped; months already saved are kept and a re-run resumes from them.
            print(f"  API error: {exc}", flush=True)
            missing.append(name)
            time.sleep(10)
    (out_root / "missing.txt").write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
    print(f"done: {len(opponents) - len(missing)} opponents downloaded, {len(missing)} missing", flush=True)


if __name__ == "__main__":
    sys.exit(main())
