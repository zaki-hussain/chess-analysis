"""Command-line entry point: `chess-analysis <command>` or `python -m chess_analysis <command>`."""

import argparse
import sys
from pathlib import Path

import os

from . import archive, dataset, engine, enrich
from .api import ApiError, ChessComClient
from .config import ConfigError, Settings, load_settings


def _fetch(settings: Settings, args: argparse.Namespace) -> None:
    print(f"Downloading archives for {settings.username} into {settings.raw_dir}")
    count = archive.download(ChessComClient(), settings.username, settings.raw_dir, args.refresh_all)
    print(f"Downloaded {count} month(s)")
    _build(settings, args)


def _build(settings: Settings, args: argparse.Namespace) -> None:
    games, moves = dataset.build_tables(settings.raw_dir)
    moves_enriched = enrich.enrich_moves(games, moves, settings.username)
    my_games = enrich.enrich_games(dataset.my_games(games, settings.username), settings.timezone, settings.session_gap_minutes)
    tables = {"games": games, "moves": moves, "moves_enriched": moves_enriched, "my_games": my_games}
    dataset.save_tables(tables, settings.user_dir, csv=args.csv)
    print(f"Wrote {len(games)} games, {len(moves)} moves ({len(moves_enriched)} enriched) to {settings.user_dir}")


def _analyse(settings: Settings, args: argparse.Namespace) -> None:
    moves = dataset.load_moves_enriched(settings.username)
    positions = engine.positions_to_evaluate(moves)
    if args.limit:
        positions = positions[: args.limit]
    path = settings.user_dir / engine.EVALS_FILE
    command = args.engine or settings.stockfish
    print(f"Evaluating {len(positions)} distinct positions with {command}, {args.workers or os.cpu_count()} workers -> {path}")
    evals = engine.run(positions, path, command, nodes=args.nodes, workers=args.workers, checkpoint=args.checkpoint)
    print(f"{len(evals)} positions in {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chess-analysis", description="Download and analyse a chess.com player's games.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--username", help="chess.com username (default: username in config.toml)")
    common.add_argument("--config", type=Path, help="path to config.toml (default: search upwards from here)")
    common.add_argument("--csv", action="store_true", help="also write games.csv and moves.csv")

    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", parents=[common], help="download new games from chess.com")
    fetch.add_argument("--refresh-all", action="store_true", help="re-download every month, not just new ones")
    fetch.set_defaults(run=_fetch)
    build = commands.add_parser("build", parents=[common], help="rebuild the tables from downloaded archives (no network)")
    build.set_defaults(run=_build)
    analyse = commands.add_parser("analyse", parents=[common], help="evaluate every position with Stockfish (resumable)")
    analyse.add_argument("--nodes", type=int, default=engine.DEFAULT_NODES, help=f"nodes per position (default {engine.DEFAULT_NODES})")
    analyse.add_argument("--workers", type=int, help="engine processes (default: one per CPU)")
    analyse.add_argument("--engine", help="Stockfish command or path (default: stockfish in config.toml)")
    analyse.add_argument("--limit", type=int, help="only evaluate the first N positions (for testing)")
    analyse.add_argument("--checkpoint", type=int, default=2000, help="save after this many positions")
    analyse.set_defaults(run=_analyse)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.username, args.config)
        args.run(settings, args)
    except (ConfigError, ApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; progress so far is saved", file=sys.stderr)
        return 130
    return 0
