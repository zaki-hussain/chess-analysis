"""Build, save and load the games and moves tables.

Both tables are rebuilt from the raw monthly archives each time, so they can
always be regenerated with `chess-analysis build`. Rows are sorted by game
end time so that rebuilding unchanged data writes identical files.
"""

import os
from pathlib import Path

import pandas as pd

from . import archive
from .config import load_settings
from .parse import parse_game

GAMES_FILE = "games.parquet"
MOVES_FILE = "moves.parquet"
MOVES_ENRICHED_FILE = "moves_enriched.parquet"
MY_GAMES_FILE = "my_games.parquet"

MOVE_COLUMNS = ["game_uuid", "ply", "move_number", "color", "san", "clock", "clock_seconds"]
# Shown first; every other column follows in the order chess.com gives it, with the full PGN last.
_LEADING_GAME_COLUMNS = [
    "uuid", "url", "archive_month", "start_datetime", "end_datetime",
    "time_class", "time_control", "rules", "rated",
    "white_username", "white_rating", "white_result",
    "black_username", "black_rating", "black_result",
    "accuracies_white", "accuracies_black",
]  # fmt: skip
_INTEGER_COLUMNS = ["end_time", "start_time", "white_rating", "black_rating"]


def build_tables(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse every saved month into (games, moves)."""
    parsed: dict[str, tuple[dict, list[dict]]] = {}
    for month, data in archive.load_months(raw_dir):
        for game in data.get("games", []):
            row, moves = parse_game(game, month)
            parsed[row["uuid"]] = (row, moves)
    ordered = sorted(parsed.values(), key=lambda item: (item[0]["end_time"], item[0]["uuid"]))

    games = pd.DataFrame([row for row, _ in ordered])
    moves = pd.DataFrame([move for _, game_moves in ordered for move in game_moves], columns=MOVE_COLUMNS)
    return _tidy_games(games), _tidy_moves(moves)


def _tidy_games(games: pd.DataFrame) -> pd.DataFrame:
    if games.empty:
        return pd.DataFrame(columns=_LEADING_GAME_COLUMNS)
    games["start_datetime"] = pd.to_datetime(
        games["pgn_utc_date"] + " " + games["pgn_utc_time"], format="%Y.%m.%d %H:%M:%S", utc=True, errors="coerce"
    )
    games["end_datetime"] = pd.to_datetime(games["end_time"], unit="s", utc=True)
    for column in _INTEGER_COLUMNS:
        if column in games:
            games[column] = games[column].astype("Int64")

    leading = [c for c in _LEADING_GAME_COLUMNS if c in games]
    rest = [c for c in games.columns if c not in leading and c != "pgn"]
    return games[leading + rest + ["pgn"]]


def _tidy_moves(moves: pd.DataFrame) -> pd.DataFrame:
    return moves.astype({"ply": "int64", "move_number": "int64", "clock_seconds": "float64"})


def save_tables(tables: dict[str, pd.DataFrame], user_dir: Path, csv: bool = False) -> None:
    """Write each table as <name>.parquet (and optionally <name>.csv) in user_dir."""
    user_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        path = user_dir / f"{name}.parquet"
        tmp = path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False, compression="zstd")
        os.replace(tmp, path)
        if csv:
            frame.to_csv(user_dir / f"{name}.csv", index=False)


def _read(username: str | None, filename: str) -> pd.DataFrame:
    path = load_settings(username).user_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `chess-analysis fetch` first")
    return pd.read_parquet(path)


def load_games(username: str | None = None) -> pd.DataFrame:
    """The games table. `username` defaults to the one in config.toml."""
    return _read(username, GAMES_FILE)


def load_moves(username: str | None = None) -> pd.DataFrame:
    """The moves table. `username` defaults to the one in config.toml."""
    return _read(username, MOVES_FILE)


def load_moves_enriched(username: str | None = None) -> pd.DataFrame:
    """Moves with board state and time-use columns (standard-rules games only)."""
    return _read(username, MOVES_ENRICHED_FILE)


def load_my_games(username: str | None = None) -> pd.DataFrame:
    """`my_games()` for the configured player, plus sessions, local time and pool history."""
    return _read(username, MY_GAMES_FILE)


def my_games(games: pd.DataFrame, username: str) -> pd.DataFrame:
    """One row per game, seen from `username`'s side of the board.

    chess.com stores each player's rating *after* the game. A rated game's
    rating before is the rating after the previous rated game in the same
    pool (each rules + time class pair has its own rating). Unrated games
    don't change the rating, so before and after are the same. The first
    game in each pool has no rating before.
    """
    username = username.lower()
    is_white = games["white_username"].str.lower() == username
    games = games[is_white | (games["black_username"].str.lower() == username)]
    is_white = is_white[games.index]

    def column(name: str) -> pd.Series:
        return games[name] if name in games else pd.Series(pd.NA, index=games.index)

    def mine(white: str, black: str) -> pd.Series:
        return column(white).where(is_white, column(black))

    def theirs(white: str, black: str) -> pd.Series:
        return column(black).where(is_white, column(white))

    white_score = games["pgn_result"].map({"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5})
    my_score = white_score.where(is_white, 1 - white_score)

    view = pd.DataFrame(
        {
            "uuid": games["uuid"],
            "url": games["url"],
            "start_datetime": games["start_datetime"],
            "end_datetime": games["end_datetime"],
            "time_class": games["time_class"],
            "time_control": games["time_control"],
            "rules": games["rules"],
            "rated": games["rated"],
            "my_color": is_white.map({True: "white", False: "black"}),
            "my_rating_before": pd.Series(pd.NA, index=games.index, dtype="Int64"),
            "my_rating_after": mine("white_rating", "black_rating"),
            "my_rating_change": pd.Series(pd.NA, index=games.index, dtype="Int64"),
            "outcome": my_score.map({1.0: "win", 0.5: "draw", 0.0: "loss"}),
            "my_result": mine("white_result", "black_result"),
            "opponent": theirs("white_username", "black_username"),
            "opponent_rating_after": theirs("white_rating", "black_rating"),
            "opponent_result": theirs("white_result", "black_result"),
            "my_accuracy": mine("accuracies_white", "accuracies_black"),
            "opponent_accuracy": theirs("accuracies_white", "accuracies_black"),
            "eco": column("pgn_eco"),
            "opening_url": column("eco"),
            "termination": column("pgn_termination"),
        }
    )

    rated = view[view["rated"]].sort_values("end_datetime", kind="stable")
    view.loc[rated.index, "my_rating_before"] = rated.groupby(["rules", "time_class"])["my_rating_after"].shift(1)
    unrated = ~view["rated"]
    view.loc[unrated, "my_rating_before"] = view.loc[unrated, "my_rating_after"]
    view["my_rating_change"] = view["my_rating_after"] - view["my_rating_before"]
    return view.reset_index(drop=True)
