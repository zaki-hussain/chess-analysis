import pandas as pd
import pytest

from chess_analysis import archive, dataset
from chess_analysis.cli import main


@pytest.fixture
def raw_dir(tmp_path, real_games):
    raw = tmp_path / "data" / "zakihussa1n" / "raw"
    games = list(real_games.values())
    archive.save_month(raw, "2022-01", {"games": games[:2]})
    archive.save_month(raw, "2023-06", {"games": games[2:]})
    return raw


def test_build_tables(raw_dir, real_games):
    games, moves = dataset.build_tables(raw_dir)

    assert len(games) == len(real_games)
    assert games["end_time"].is_monotonic_increasing
    assert list(games.columns[:3]) == ["uuid", "url", "archive_month"]
    assert games.columns[-1] == "pgn"
    assert games["white_rating"].dtype == "Int64"

    live = games.set_index("uuid").loc[real_games["live_with_extras"]["uuid"]]
    assert live["start_datetime"] == pd.Timestamp("2023-06-06 12:30:09", tz="UTC")
    assert live["end_datetime"] == pd.Timestamp(1686054777, unit="s", tz="UTC")

    assert list(moves.columns) == dataset.MOVE_COLUMNS
    assert len(moves) == sum(len(g["tcn"]) // 2 for g in real_games.values())
    # Moves are grouped by game in the same order as the games table.
    assert list(dict.fromkeys(moves["game_uuid"])) == [u for u in games["uuid"] if u in set(moves["game_uuid"])]


def test_a_game_saved_in_two_months_appears_once(tmp_path, real_games):
    game = real_games["daily"]
    archive.save_month(tmp_path, "2023-04", {"games": [game]})
    archive.save_month(tmp_path, "2023-05", {"games": [game]})
    games, moves = dataset.build_tables(tmp_path)
    assert len(games) == 1
    assert games.loc[0, "archive_month"] == "2023-05"
    assert len(moves) == len(game["tcn"]) // 2


def test_no_games(tmp_path):
    archive.save_month(tmp_path, "2023-05", {"games": []})
    games, moves = dataset.build_tables(tmp_path)
    assert games.empty and moves.empty


def test_save_tables_round_trip(tmp_path, raw_dir):
    games, moves = dataset.build_tables(raw_dir)
    dataset.save_tables({"games": games, "moves": moves}, tmp_path, csv=True)
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / "games.parquet"), games)
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / "moves.parquet"), moves)
    assert (tmp_path / "games.csv").exists() and (tmp_path / "moves.csv").exists()


def test_cli_build_then_load(tmp_path, raw_dir, monkeypatch):
    (tmp_path / "config.toml").write_text('username = "zakihussa1n"\n')
    assert main(["build", "--config", str(tmp_path / "config.toml")]) == 0

    monkeypatch.chdir(tmp_path)
    games, moves = dataset.load_games(), dataset.load_moves()
    assert len(games) == 5 and len(moves) > 0
    assert not (tmp_path / "data" / "zakihussa1n" / "games.csv").exists()


def game(end, white, black, w_rating, b_rating, result, rated=True, time_class="blitz", rules="chess"):
    return {
        "uuid": f"g{end}", "url": f"https://example.com/{end}", "start_datetime": pd.Timestamp(end, unit="s", tz="UTC"),
        "end_datetime": pd.Timestamp(end, unit="s", tz="UTC"), "time_class": time_class, "time_control": "180",
        "rules": rules, "rated": rated, "white_username": white, "black_username": black,
        "white_rating": w_rating, "black_rating": b_rating, "white_result": "?", "black_result": "?",
        "pgn_result": result,
    }  # fmt: skip


def test_my_games_ratings_and_perspective():
    games = pd.DataFrame(
        [
            game(1, "Me", "a", 1000, 900, "1-0"),
            game(2, "b", "me", 1200, 1490, "0-1", time_class="bullet"),
            game(3, "c", "me", 800, 990, "1-0"),
            game(4, "me", "d", 990, 1000, "1/2-1/2", rated=False),
            game(5, "me", "e", 1000, 1100, "1-0"),
        ]
    ).astype({"white_rating": "Int64", "black_rating": "Int64"})

    view = dataset.my_games(games, "ME").set_index("uuid")

    assert list(view["my_color"]) == ["white", "black", "black", "white", "white"]
    assert list(view["outcome"]) == ["win", "win", "loss", "draw", "win"]
    assert list(view["opponent"]) == ["a", "b", "c", "d", "e"]
    assert list(view["opponent_rating_after"]) == [900, 1200, 800, 1000, 1100]
    assert list(view["my_rating_after"]) == [1000, 1490, 990, 990, 1000]
    # Blitz pool: g1 -> g3 -> g5. Bullet pool only has g2. The unrated g4 changes nothing.
    assert view.loc["g1", "my_rating_before"] is pd.NA
    assert view.loc["g2", "my_rating_before"] is pd.NA
    assert view.loc["g3", "my_rating_before"] == 1000
    assert view.loc["g3", "my_rating_change"] == -10
    assert view.loc["g4", "my_rating_before"] == 990
    assert view.loc["g4", "my_rating_change"] == 0
    assert view.loc["g5", "my_rating_before"] == 990
    assert view.loc["g5", "my_rating_change"] == 10


def test_my_games_skips_games_without_the_player():
    games = pd.DataFrame([game(1, "me", "a", 1000, 900, "1-0"), game(2, "x", "y", 1, 2, "1-0")])
    assert list(dataset.my_games(games, "me")["uuid"]) == ["g1"]
