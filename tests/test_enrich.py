import pandas as pd
import pytest

from chess_analysis import dataset, enrich
from chess_analysis.parse import parse_game


@pytest.mark.parametrize(
    ("time_control", "expected"),
    [("180+2", (180.0, 2.0)), ("600", (600.0, 0.0)), ("60+1", (60.0, 1.0)), ("1/604800", (None, 0.0))],
)
def test_parse_time_control(time_control, expected):
    assert enrich.parse_time_control(time_control) == expected


@pytest.mark.parametrize(
    ("pieces", "move_number", "expected"),
    [(14, 5, "opening"), (14, 13, "middlegame"), (10, 5, "middlegame"), (6, 5, "endgame"), (0, 40, "endgame")],
)
def test_phase(pieces, move_number, expected):
    assert enrich.phase(pieces, move_number) == expected


def test_replay_board_state():
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    rows = enrich.replay(start, ["e4", "d5", "exd5", "Qxd5", "Nc3", "Qe5+"], [1, 1, 2, 2, 3, 3])

    assert rows[0]["fen_before"] == start
    assert rows[0]["legal_moves"] == 20
    assert (rows[0]["piece"], rows[0]["uci"], rows[0]["is_capture"]) == ("pawn", "e2e4", False)
    assert rows[2]["is_capture"] and rows[2]["material_black"] == 39
    assert rows[3]["material_white"] == 39 and rows[3]["material_black"] == 38  # after exd5
    assert rows[4]["material_white"] == 38 and rows[4]["material_black"] == 38
    assert rows[5]["gives_check"] is True
    assert rows[5]["pieces"] == 14 and rows[5]["phase"] == "opening"


def test_replay_matches_chess_com_final_position(real_games):
    game = real_games["live_with_extras"]
    _, moves = parse_game(game, "2023-06")
    rows = enrich.replay(game["initial_setup"], [m["san"] for m in moves], [m["move_number"] for m in moves])
    import chess

    board = chess.Board(rows[-1]["fen_before"])
    board.push_uci(rows[-1]["uci"])
    assert board.fen(en_passant="fen").split(" ")[:3] == game["fen"].split(" ")[:3]


def _moves_frame(clocks, time_class="blitz"):
    return pd.DataFrame(
        {
            "game_uuid": "g1",
            "ply": range(1, len(clocks) + 1),
            "move_number": [(i // 2) + 1 for i in range(len(clocks))],
            "color": ["white", "black"] * (len(clocks) // 2) + (["white"] if len(clocks) % 2 else []),
            "san": ["e4", "e5", "Nf3", "Nc6", "Bb5"][: len(clocks)],
            "clock": [str(c) for c in clocks],
            "clock_seconds": clocks,
        }
    )


def _games_frame(time_control="180+2", time_class="blitz", rules="chess"):
    return pd.DataFrame(
        [
            {
                "uuid": "g1", "initial_setup": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                "time_control": time_control, "time_class": time_class, "rules": rules,
                "white_username": "Me", "black_username": "them",
            }
        ]
    )  # fmt: skip


def test_enrich_moves_time_columns():
    # 180+2: white thinks 1.0 s then 3.5 s; black thinks 2.0 s then 0.5 s.
    moves = _moves_frame([181.0, 180.0, 179.5, 181.5, 170.0])
    out = enrich.enrich_moves(_games_frame(), moves, "me")

    assert list(out["clock_before_seconds"]) == [180.0, 180.0, 181.0, 180.0, 179.5]
    assert list(out["time_spent_seconds"]) == [1.0, 2.0, 3.5, 0.5, 11.5]
    assert list(out["opponent_clock_seconds"]) == [180.0, 181.0, 180.0, 179.5, 181.5]
    assert list(out["clock_diff_seconds"]) == [0.0, -1.0, 1.0, 0.5, -2.0]
    assert list(out["clock_fraction"].round(4)) == [round(c / 180, 4) for c in [181.0, 180.0, 179.5, 181.5, 170.0]]
    assert list(out["is_player_move"]) == [True, False, True, False, True]
    assert out["base_seconds"].iloc[0] == 180 and out["increment_seconds"].iloc[0] == 2
    assert out["time_class"].iloc[0] == "blitz"
    assert list(out["phase"]) == ["opening"] * 5


def test_enrich_moves_daily_has_no_time_columns():
    out = enrich.enrich_moves(_games_frame("1/86400", "daily"), _moves_frame([5.0, 7.0], "daily"), "me")
    assert out["time_spent_seconds"].isna().all() and out["clock_before_seconds"].isna().all()
    assert out["base_seconds"].isna().all()
    assert list(out["fen_before"].str.len() > 0) == [True, True]


def test_enrich_moves_skips_variants():
    out = enrich.enrich_moves(_games_frame(rules="crazyhouse"), _moves_frame([181.0, 180.0]), "me")
    assert out.empty


def _view(rows):
    frame = pd.DataFrame(rows)
    frame["start_datetime"] = pd.to_datetime(frame["start"], utc=True)
    frame["end_datetime"] = frame["start_datetime"] + pd.to_timedelta(frame["minutes"], unit="m")
    frame["uuid"] = [f"g{i}" for i in range(len(frame))]
    frame["time_control"] = "180"
    frame["rules"] = "chess"
    return frame


def test_enrich_games_sessions_local_time_and_pool_history():
    view = _view(
        [
            {"start": "2024-06-01 18:00", "minutes": 5, "time_class": "blitz"},
            {"start": "2024-06-01 18:06", "minutes": 5, "time_class": "blitz"},   # 1 min gap: same session
            {"start": "2024-06-01 18:50", "minutes": 5, "time_class": "bullet"},  # 39 min gap: new session
            {"start": "2024-06-20 09:00", "minutes": 5, "time_class": "blitz"},   # new session, 19 days later
            {"start": "2024-07-25 09:00", "minutes": 5, "time_class": "blitz"},   # > 30 days after any blitz game
        ]
    )  # fmt: skip
    out = enrich.enrich_games(view, "Europe/London", 30).set_index("uuid")

    # BST in June: 18:00 UTC is 19:00 local.
    assert out.loc["g0", "local_hour"] == 19 and out.loc["g0", "weekday"] == "Saturday"
    assert out.loc["g0", "local_start"] == pd.Timestamp("2024-06-01 19:00")
    assert list(out["session"]) == [1, 1, 2, 3, 4]
    assert list(out["game_in_session"]) == [1, 2, 1, 1, 1]
    assert list(out["session_games"]) == [2, 2, 1, 1, 1]
    assert out.loc["g1", "minutes_into_session"] == 6
    assert pd.isna(out.loc["g0", "days_since_last_in_pool"])
    assert pd.isna(out.loc["g2", "days_since_last_in_pool"])  # first bullet game
    # g1 ended 2024-06-01 18:11, g3 ended 2024-06-20 09:05: 19 days minus 9 h 6 min.
    assert out.loc["g3", "days_since_last_in_pool"] == pytest.approx(19 - (9 + 6 / 60) / 24, abs=1e-6)
    assert list(out["pool_games_last_30d"]) == [0, 1, 0, 2, 0]
    assert out.loc["g0", "base_seconds"] == 180 and out.loc["g0", "increment_seconds"] == 0


def test_cli_build_writes_enriched_tables(tmp_path, real_games, monkeypatch):
    from chess_analysis import archive
    from chess_analysis.cli import main

    raw = tmp_path / "data" / "zakihussa1n" / "raw"
    archive.save_month(raw, "2023-06", {"games": list(real_games.values())})
    (tmp_path / "config.toml").write_text('username = "zakihussa1n"\ntimezone = "Europe/London"\n')
    assert main(["build", "--config", str(tmp_path / "config.toml")]) == 0
    monkeypatch.chdir(tmp_path)
    enriched, mine = dataset.load_moves_enriched(), dataset.load_my_games()
    assert set(enriched["game_uuid"]) == {g["uuid"] for g in real_games.values() if g["rules"] == "chess" and g["tcn"]}
    assert len(mine) == len(real_games) and "session" in mine
