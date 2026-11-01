import shutil

import chess
import pandas as pd
import pytest

from chess_analysis import engine


def test_position_key_drops_move_counters():
    assert engine.position_key("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 3 12") == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def test_positions_to_evaluate_are_distinct_and_include_final_positions():
    moves = pd.DataFrame(
        {
            "fen_before": ["A w - - 0 1", "B b - - 0 1", "A w - - 5 9"],
            "fen_after": ["B b - - 0 1", "C w - - 1 2", "D b - - 5 9"],
        }
    )
    assert engine.positions_to_evaluate(moves) == ["A w - -", "B b - -", "C w - -", "D b - -"]


class FakeEngine:
    id = {"name": "Fake 1.0"}


def test_evaluate_terminal_positions_without_an_engine():
    mated = engine.evaluate("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq -", 1000, FakeEngine())
    assert mated["terminal"] and mated["mate_in"] == 0 and mated["score_cp"] is None
    assert (mated["wdl_win"], mated["wdl_draw"], mated["wdl_loss"]) == (0, 0, 1000)  # White is mated
    stalemate = engine.evaluate("7k/5Q2/6K1/8/8/8/8/8 b - -", 1000, FakeEngine())
    assert stalemate["terminal"] and stalemate["score_cp"] == 0.0 and stalemate["wdl_draw"] == 1000
    assert mated["engine"] == "Fake 1.0" and mated["nodes_limit"] == 1000


def fake_chunk(positions, nodes):
    return [
        {**dict.fromkeys(engine.EVAL_COLUMNS), "position": p, "engine": "fake", "nodes_limit": nodes, "depth": 1, "score_cp": float(len(p)), "best_move": "e2e4", "terminal": False}
        for p in positions
    ]


def test_run_checkpoints_and_resumes(tmp_path):
    path = tmp_path / "evals.parquet"
    positions = [f"{i} w - -" for i in range(10)]
    logs: list[str] = []

    first = engine.run(positions[:6], path, "unused", nodes=5, checkpoint=2, chunk_size=2, log=logs.append, evaluate_chunk=fake_chunk)
    assert len(first) == 6 and path.exists()
    assert any("done" in line for line in logs)

    calls: list[list[str]] = []

    def counting_chunk(chunk, nodes):
        calls.append(chunk)
        return fake_chunk(chunk, nodes)

    second = engine.run(positions, path, "unused", nodes=5, chunk_size=3, log=logs.append, evaluate_chunk=counting_chunk)
    assert sorted(p for chunk in calls for p in chunk) == positions[6:]  # only the missing ones were evaluated
    assert list(second["position"]) == sorted(positions)
    assert second["nodes_limit"].dtype == "Int64" and second["terminal"].dtype == bool


def test_run_at_a_different_node_budget_re_evaluates(tmp_path):
    path = tmp_path / "evals.parquet"
    engine.run(["x w - -"], path, "unused", nodes=5, evaluate_chunk=fake_chunk, log=lambda _: None)
    calls = []
    engine.run(["x w - -"], path, "unused", nodes=50, evaluate_chunk=lambda c, n: calls.append(c) or fake_chunk(c, n), log=lambda _: None)
    assert calls == [["x w - -"]]
    assert engine.load_evals(path)["nodes_limit"].tolist() == [50]


def test_with_evals_joins_before_and_after():
    moves = pd.DataFrame({"fen_before": ["A w - - 0 1"], "fen_after": ["B b - - 0 1"], "uci": ["e2e4"]})
    evals = pd.DataFrame(fake_chunk(["A w - -", "B b - -"], 5))
    out = engine.with_evals(moves, evals)
    assert out.loc[0, "score_cp_before"] == 7.0 and out.loc[0, "score_cp_after"] == 7.0
    assert out.loc[0, "best_move_before"] == "e2e4" and bool(out.loc[0, "is_best_move"])


@pytest.mark.skipif(shutil.which("stockfish") is None, reason="stockfish not installed")
def test_real_engine_round_trip(tmp_path):
    path = tmp_path / "evals.parquet"
    positions = [engine.position_key(chess.STARTING_FEN), "8/8/8/8/8/4k3/4p3/4K3 w - -"]
    out = engine.run(positions, path, "stockfish", nodes=20_000, workers=2, log=lambda _: None)
    assert len(out) == 2
    start = out.set_index("position").loc[positions[0]]
    assert start["best_move"] in {"e2e4", "d2d4", "g1f3", "c2c4", "e2e3"} and start["nodes"] >= 20_000
    assert start["engine"].startswith("Stockfish") and start["wdl_win"] + start["wdl_draw"] + start["wdl_loss"] == 1000
    assert out.set_index("position").loc[positions[1], "score_cp"] == 0.0  # dead-drawn king and pawn endgame
