import chess
import pandas as pd
import pytest

from chess_analysis import analysed, tablebase
from chess_analysis.engine import EVAL_COLUMNS


def test_probeable_rules():
    assert tablebase.probeable("8/8/8/8/8/4k3/4p3/4K3 w - -")
    assert not tablebase.probeable("8/8/8/8/8/4k3/4p3/R3K3 w Q -")  # castling rights
    assert not tablebase.probeable("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -")


def test_candidate_positions_are_distinct_small_endgames():
    moves = pd.DataFrame({"fen_before": ["8/8/8/8/8/4k3/4p3/4K3 w - - 0 1", "8/8/8/8/8/4k3/4p3/4K3 w - - 5 9"], "fen_after": ["8/8/8/8/8/4k3/4p3/3K4 b - - 1 1", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"]})
    assert tablebase.candidate_positions(moves) == ["8/8/8/8/8/4k3/4p3/3K4 b - -", "8/8/8/8/8/4k3/4p3/4K3 w - -"]


def test_probe_with_handles_terminal_positions_and_missing_tables():
    def fake(board):
        if board.fen().startswith("8/8/8/8/8/4k3/4p3/4K3"):
            return 0, 0
        raise KeyError("no table")

    out = tablebase.probe_with(["8/8/8/8/8/4k3/4p3/4K3 w - -", "7k/5Q2/6K1/8/8/8/8/8 b - -", "7k/6Q1/6K1/8/8/8/8/8 b - -", "8/8/8/8/8/4k3/8/4K2R w - -"], fake)
    assert out.set_index("position")["wdl"].to_dict() == {"8/8/8/8/8/4k3/4p3/4K3 w - -": 0, "7k/5Q2/6K1/8/8/8/8/8 b - -": 0, "7k/6Q1/6K1/8/8/8/8/8 b - -": -2}  # stalemate, checkmate; rook ending skipped


def test_analyse_moves_adds_tablebase_error():
    moves = pd.DataFrame({"game_uuid": ["g"] * 2, "ply": [1, 2], "color": ["white", "black"], "uci": ["a1a2", "h8h7"], "fen_before": ["A w - - 0 1", "B b - - 0 1"], "fen_after": ["B b - - 0 1", "C w - - 1 2"]})
    evals = pd.DataFrame([dict(dict.fromkeys(EVAL_COLUMNS), position=p, score_cp=s, wdl_win=500, wdl_draw=0, wdl_loss=500, terminal=False, best_move="x", nodes_limit=1, engine="t") for p, s in [("A w - -", 600.0), ("B b - -", 0.0), ("C w - -", 0.0)]])
    tb = pd.DataFrame({"position": ["A w - -", "B b - -", "C w - -"], "pieces_on_board": [4, 4, 4], "wdl": [2, 0, 0], "dtz": [10, 0, 0]})
    out = analysed.analyse_moves(moves, evals, tb).set_index("ply")
    assert out.loc[1, "tb_wdl_before"] == 2 and out.loc[1, "tb_wdl_after"] == 0 and bool(out.loc[1, "tb_error"])  # White threw away a win
    assert out.loc[2, "tb_wdl_before"] == 0 and out.loc[2, "tb_wdl_after"] == 0 and not out.loc[2, "tb_error"]
    assert out.loc[1, "tb_dtz_before"] == 10


@pytest.mark.skipif(not (tablebase.Path("/opt/syzygy") / "KQvK.rtbw").exists(), reason="Syzygy tables not installed")
def test_real_probe():
    out = tablebase.probe(["8/8/8/8/8/4k3/8/3QK3 w - -", "8/8/8/8/8/4k3/4p3/4K3 w - -"], tablebase.Path("/opt/syzygy"))
    assert out.set_index("position")["wdl"].to_dict() == {"8/8/8/8/8/4k3/8/3QK3 w - -": 2, "8/8/8/8/8/4k3/4p3/4K3 w - -": 0}
