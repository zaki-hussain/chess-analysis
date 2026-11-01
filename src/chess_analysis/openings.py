"""Opening names from the Lichess chess-openings list (data/openings/*.tsv).

A game's opening is the deepest position it reaches that matches a named
line (transpositions included), and `book_ply` is the ply that reached it.
At club level that is usually only a few moves in.
"""

import csv
from functools import lru_cache
from pathlib import Path

import chess
import pandas as pd

from .engine import position_key


@lru_cache(maxsize=4)
def load_book(openings_dir: Path) -> dict[str, tuple[str, str]]:
    """position key -> (ECO code, opening name) for every named line."""
    book: dict[str, tuple[str, str]] = {}
    for path in sorted(Path(openings_dir).glob("*.tsv")):
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                board = chess.Board()
                for token in row["pgn"].split():
                    if not token[0].isdigit():
                        board.push_san(token)
                book[position_key(board.fen())] = (row["eco"], row["name"])
    return book


def in_book(moves_enriched: pd.DataFrame, book: dict[str, tuple[str, str]]) -> pd.Series:
    """True for moves whose resulting position is a named opening line."""
    return moves_enriched["fen_after"].map(position_key).isin(book)


def game_openings(moves_enriched: pd.DataFrame, book: dict[str, tuple[str, str]]) -> pd.DataFrame:
    """Per game: opening_eco, opening_name and book_ply (0 and empty when no line matched)."""
    keys = moves_enriched["fen_after"].map(position_key)
    matched = moves_enriched.loc[keys.isin(book), ["game_uuid", "ply"]].assign(position=keys[keys.isin(book)])
    deepest = matched.sort_values(["game_uuid", "ply"]).groupby("game_uuid").tail(1).set_index("game_uuid")
    out = pd.DataFrame(index=pd.Index(moves_enriched["game_uuid"].unique(), name="uuid"))
    out["opening_eco"] = deepest["position"].map(lambda p: book[p][0]).reindex(out.index).fillna("")
    out["opening_name"] = deepest["position"].map(lambda p: book[p][1]).reindex(out.index).fillna("")
    out["book_ply"] = deepest["ply"].reindex(out.index).fillna(0).astype("int64")
    return out
