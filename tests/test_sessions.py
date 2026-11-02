import numpy as np
import pandas as pd

from chess_analysis import sessions as S


def _games(outcomes, starts_minutes, length_minutes=3.0):
    t0 = pd.Timestamp("2024-01-01 12:00", tz="UTC")
    start = [t0 + pd.Timedelta(minutes=m) for m in starts_minutes]
    return pd.DataFrame({
        "uuid": [f"g{i}" for i in range(len(outcomes))],
        "start_datetime": start,
        "end_datetime": [s + pd.Timedelta(minutes=length_minutes) for s in start],
        "outcome": outcomes,
    })  # fmt: skip


def test_session_features_split_on_the_gap_and_count_streaks():
    # three games back to back, a 60-minute pause, then two more
    games = _games(["loss", "loss", "win", "win", "loss"], [0, 4, 8, 71, 75])
    f = S.session_features(games, gap_minutes=30)
    assert list(f["session_id"]) == [1, 1, 1, 2, 2]
    assert list(f["game_in_session"]) == [1, 2, 3, 1, 2]
    assert list(f["is_last_in_session"]) == [False, False, True, False, True]
    assert list(f["loss_streak"]) == [0, 1, 2, 0, 0]  # streaks reset at a new session
    assert list(f["win_streak"]) == [0, 0, 0, 0, 1]
    assert pd.isna(f["prev_outcome"].iloc[0]) and f["prev_outcome"].iloc[1] == "loss" and pd.isna(f["prev_outcome"].iloc[3])
    assert f["prev2_outcomes"].iloc[2] == "LL" and pd.isna(f["prev2_outcomes"].iloc[1])
    assert f["prev_any_outcome"].iloc[3] == "win"  # the previous game overall, across the session boundary
    assert np.isnan(f["gap_before_minutes"].iloc[3]) and abs(f["break_before_minutes"].iloc[3] - 60) < 1e-9
    assert abs(f["session_minutes"].iloc[0] - 11) < 1e-9


def test_session_features_keep_the_input_index_order():
    games = _games(["win", "loss", "draw"], [10, 0, 5]).set_index(pd.Index([7, 8, 9]))
    f = S.session_features(games, gap_minutes=30)
    assert list(f.index) == [7, 8, 9]
    assert list(f["game_in_session"]) == [3, 1, 2]


def test_session_main_control_takes_the_majority_then_the_first_played():
    games = _games(["win"] * 5, [0, 4, 8, 100, 104]).assign(session_id=[1, 1, 1, 2, 2], tc_group=["bullet 1+1", "bullet 1+0", "bullet 1+0", "blitz 3+2", "bullet 1+0"])
    main = S.session_main_control(games)
    assert main.loc[1] == "bullet 1+0" and main.loc[2] == "blitz 3+2"


def test_metric_is_a_ratio_of_sums_and_the_difference_is_paired():
    rng = np.random.default_rng(0)
    n = 400
    frame = pd.DataFrame({"session_id": np.repeat(np.arange(100), 4), "my_ep_lost": rng.uniform(0, 2, n), "my_moves": rng.integers(10, 60, n).astype(float), "my_score": rng.integers(0, 2, n).astype(float)})
    est, lo, hi = S.metric(frame, "my_ep")
    assert abs(est - frame["my_ep_lost"].sum() / frame["my_moves"].sum()) < 1e-12 and lo < est < hi
    d, dlo, dhi = S.metric_diff(frame, frame, "my_ep")
    assert abs(d) < 1e-12 and abs(dlo) < 1e-12 and abs(dhi) < 1e-12  # the same games against themselves: no spread at all
    again = S.metric(frame, "my_ep")
    assert again == (est, lo, hi)  # fixed seed: deterministic


def test_cell_stats_puts_elo_next_to_the_excess():
    frame = pd.DataFrame({"session_id": np.arange(200) // 2, "my_score": [1.0, 0.0] * 100, "my_ep_start": 0.4, "excess_model": [0.6, -0.4] * 100})
    out = S.cell_stats(frame, ["excess"])
    assert list(out)[:8] == ["score", "expected", "excess", "excess_lo", "excess_hi", "excess_elo", "excess_elo_lo", "excess_elo_hi"]
    assert abs(out["excess"] - 0.1) < 1e-12 and out["excess_elo"] > 0
    # Elo-equivalent of scoring 0.5 against an expected 0.4
    assert abs(out["excess_elo"] - 400 * np.log10(1.5)) < 1e-9


def test_cell_stats_gives_the_elo_formula_excess_its_own_elo_equivalent():
    frame = pd.DataFrame({"session_id": np.arange(200) // 2, "my_score": [1.0, 0.0] * 100, "elo_expected": 0.4, "excess_rating": [0.6, -0.4] * 100})
    out = S.cell_stats(frame, ["excess_rating"])
    assert list(out)[:7] == ["elo_expected", "excess_rating", "excess_rating_lo", "excess_rating_hi", "excess_rating_elo", "excess_rating_elo_lo", "excess_rating_elo_hi"]
    assert abs(out["excess_rating_elo"] - 400 * np.log10(1.5)) < 1e-9


def test_cells_by_level_with_counts_and_thin_flags():
    rng = np.random.default_rng(0)
    n = 240
    games = pd.DataFrame({
        "session_id": np.arange(n) // 2, "time_class": np.where(np.arange(n) < 200, "bullet", "blitz"),
        "tc_group": np.where(np.arange(n) < 200, "bullet 1+0", "blitz 3+2"), "level": np.tile(["x", "y"], n // 2),
        "my_score": rng.integers(0, 2, n).astype(float), "my_ep_start": 0.5, "excess_model": rng.uniform(-0.5, 0.5, n),
    })  # fmt: skip
    assert len(S.subset(games, S.ALL)) == n and len(S.subset(games, "all blitz")) == 40 and len(S.subset(games, "bullet 1+0")) == 200
    assert S.groups_with_all(type("D", (), {"tc_groups": ["bullet 1+0"]})()) == ["bullet 1+0", S.ALL]
    table = S.by_level(games, ["bullet 1+0", "blitz 3+2", S.ALL], "level", ["x", "y"], ["excess"])
    assert list(table.columns[:2]) == ["tc_group", "level"] and list(table.columns[-3:]) == ["games", "sessions", "thin"]
    assert list(table["games"]) == [100, 100, 20, 20, 120, 120] and list(table["thin"]) == [False, False, True, True, False, False]
    assert S.thin_cell(games.iloc[:100]) is False and S.thin_cell(games.iloc[:100], min_games=101) is True
    assert S.counts(games.iloc[:10]) == {"games": 10, "sessions": 5}
