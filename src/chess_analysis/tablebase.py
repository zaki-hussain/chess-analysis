"""Exact results for small endgames from Syzygy tablebases.

`tablebase.parquet` has one row per distinct position with at most
MAX_PIECES pieces (kings included) and no castling rights, giving the exact
outcome from the side to move's view: wdl 2 = win, 1 = win spoiled by the
50-move rule, 0 = draw, -1/-2 the losses; dtz = distance to zeroing move.
The tables themselves (3-4-5 pieces, ~1 GB) live outside the repo.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

import chess
import chess.syzygy
import pandas as pd

TABLEBASE_FILE = "tablebase.parquet"
MAX_PIECES = 5
TABLEBASE_COLUMNS = ["position", "pieces_on_board", "wdl", "dtz"]


def pieces_on_board(position: str) -> int:
    return sum(c.isalpha() for c in position.split(" ")[0])


def probeable(position: str) -> bool:
    board = chess.Board(position + " 0 1")
    return len(board.piece_map()) <= MAX_PIECES and not board.castling_rights


def candidate_positions(moves_enriched: pd.DataFrame) -> list[str]:
    from .engine import position_key

    keys = pd.concat([moves_enriched["fen_before"], moves_enriched["fen_after"]]).map(position_key).unique()
    return sorted(k for k in keys if probeable(k))


def probe(positions: Iterable[str], syzygy_dir: Path) -> pd.DataFrame:
    with chess.syzygy.open_tablebase(str(syzygy_dir)) as tables:
        return probe_with(positions, lambda b: (tables.probe_wdl(b), tables.probe_dtz(b)))


def probe_with(positions: Iterable[str], probe_fn: Callable[[chess.Board], tuple[int, int]]) -> pd.DataFrame:
    rows = []
    for position in positions:
        board = chess.Board(position + " 0 1")
        if board.is_checkmate():
            wdl, dtz = -2, 0
        elif board.is_stalemate() or board.is_insufficient_material():
            wdl, dtz = 0, 0
        else:
            try:
                wdl, dtz = probe_fn(board)
            except (KeyError, chess.syzygy.MissingTableError):
                continue
        rows.append({"position": position, "pieces_on_board": len(board.piece_map()), "wdl": wdl, "dtz": dtz})
    return pd.DataFrame(rows, columns=TABLEBASE_COLUMNS).astype({"pieces_on_board": "int64", "wdl": "int64", "dtz": "int64"})


def load_tablebase(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=TABLEBASE_COLUMNS)


def save_tablebase(table: pd.DataFrame, path: Path) -> None:
    table = table.sort_values("position", kind="stable").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(path, index=False, compression="zstd")
