"""Command-line entry point: `chess-analysis <command>` or `python -m chess_analysis <command>`."""

import argparse
import sys
from pathlib import Path

from . import archive
from .api import ApiError, ChessComClient
from .config import ConfigError, Settings, load_settings


def _fetch(settings: Settings, args: argparse.Namespace) -> None:
    print(f"Downloading archives for {settings.username} into {settings.raw_dir}")
    count = archive.download(ChessComClient(), settings.username, settings.raw_dir, args.refresh_all)
    print(f"Downloaded {count} month(s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chess-analysis", description="Download and analyse a chess.com player's games.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--username", help="chess.com username (default: username in config.toml)")
    common.add_argument("--config", type=Path, help="path to config.toml (default: search upwards from here)")

    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", parents=[common], help="download new games from chess.com")
    fetch.add_argument("--refresh-all", action="store_true", help="re-download every month, not just new ones")
    fetch.set_defaults(run=_fetch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.username, args.config)
        args.run(settings, args)
    except (ConfigError, ApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
