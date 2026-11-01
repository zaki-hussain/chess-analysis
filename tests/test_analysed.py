import numpy as np
import pandas as pd

from chess_analysis import analysed
from chess_analysis.engine import EVAL_COLUMNS

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def ev(position, score_cp=None, mate_in=None, wdl=(500, 0, 500), terminal=False, best="e2e4", second="d2d4", second_cp=None, second_mate=None):
    row = dict.fromkeys(EVAL_COLUMNS)
    row.update(position=position, engine="t", nodes_limit=1, score_cp=score_cp, mate_in=mate_in, wdl_win=wdl[0], wdl_draw=wdl[1], wdl_loss=wdl[2], terminal=terminal, best_move=best, second_move=second, second_score_cp=second_cp, second_mate_in=second_mate)
    return row


def test_white_cp_maps_mates_and_terminal_positions():
    out = analysed.white_cp(
        pd.Series([35.0, np.nan, np.nan, np.nan, np.nan, -19866.0]),
        pd.Series([np.nan, 3, -2, 0, 0, np.nan], dtype="Int64"),
        pd.Series([500, 1000, 0, 0, 1000, 0], dtype="Int64"),
        pd.Series([False, False, False, True, True, False]),
    )
    assert list(out) == [35.0, 9997.0, -9998.0, -10000.0, 10000.0, -10000.0]


def test_analyse_moves_from_the_movers_view():
    moves = pd.DataFrame(
        {
            "game_uuid": ["g"] * 3,
            "ply": [1, 2, 3],
            "color": ["white", "black", "white"],
            "uci": ["e2e4", "e7e5", "d1h5"],
            "fen_before": ["A w - - 0 1", "B b - - 0 1", "C w - - 1 2"],
            "fen_after": ["B b - - 0 1", "C w - - 1 2", "D b - - 1 2"],
        }
    )
    evals = pd.DataFrame(
        [
            ev("A w - -", score_cp=30.0, wdl=(300, 500, 200), best="e2e4", second="d2d4", second_cp=25.0),
            ev("B b - -", score_cp=20.0, wdl=(250, 550, 200), best="c7c5", second="e7e5", second_cp=40.0),
            # White to move, +40 for White, but a forced mate for Black is available after the bad move.
            ev("C w - -", score_cp=40.0, wdl=(300, 500, 200), best="g1f3", second="b1c3", second_cp=-200.0),
            ev("D b - -", mate_in=-4, wdl=(0, 0, 1000), best="d8h4", second=None),
        ]
    )
    out = analysed.analyse_moves(moves, evals).set_index("ply")

    # Ply 1: White plays the best move; eval stays about the same.
    assert out.loc[1, "cp_before"] == 30.0 and out.loc[1, "cp_after"] == 20.0 and out.loc[1, "cp_loss"] == 10.0
    assert bool(out.loc[1, "is_best_move"]) and out.loc[1, "criticality"] == 5.0
    assert out.loc[1, "ep_sf_before"] == 0.55 and out.loc[1, "label"] == "good"
    # Ply 2: Black's view flips the sign; Black played the second-best move.
    assert out.loc[2, "cp_before"] == -20.0 and out.loc[2, "cp_after"] == -40.0 and out.loc[2, "cp_loss"] == 20.0
    assert not out.loc[2, "is_best_move"] and bool(out.loc[2, "is_second_move"])
    assert out.loc[2, "ep_sf_before"] == 0.475
    # Ply 3: White allows mate in 4; loss is clipped at the ±1000 scale.
    assert out.loc[3, "cp_after"] == -9996.0 and out.loc[3, "mate_after"] == -4
    assert out.loc[3, "cp_loss"] == 1040.0 and out.loc[3, "label"] == "blunder"
    assert bool(out.loc[3, "allowed_mate"]) and not out.loc[3, "missed_mate"]
    assert out.loc[3, "criticality"] == 240.0
    assert out.loc[3, "ep_sf_after"] == 0.0 and out.loc[3, "ep_sf_loss"] == 0.55


def test_checkmating_move_has_no_loss():
    moves = pd.DataFrame({"game_uuid": ["g"], "ply": [1], "color": ["black"], "uci": ["d8h4"], "fen_before": ["X b - - 0 1"], "fen_after": ["Y w - - 1 2"]})
    evals = pd.DataFrame([ev("X b - -", mate_in=-1, wdl=(0, 0, 1000), best="d8h4", second=None), ev("Y w - -", mate_in=0, wdl=(0, 0, 1000), terminal=True, best=None, second=None)])
    out = analysed.analyse_moves(moves, evals).iloc[0]
    assert out["cp_before"] == 9999.0 and out["cp_after"] == 10000.0 and out["cp_loss"] == 0.0
    assert pd.isna(out["criticality"]) and out["label"] == "good"
    # Delivering the mate is not missing it.
    assert not out["missed_mate"] and not out["allowed_mate"]
