import numpy as np
import pandas as pd

from chess_analysis import calibrate


def test_lichess_curve_is_symmetric_and_bounded():
    cp = pd.Series([-5000.0, -300.0, 0.0, 300.0, 5000.0])
    ep = calibrate.lichess_expected_points(cp)
    assert ep[2] == 0.5 and abs(ep[1] + ep[3] - 1) < 1e-9 and ep[4] < 1 and ep[0] > 0


def test_pre_game_ratings_avoid_post_game_leak():
    my_games = pd.DataFrame(
        {
            "uuid": ["g1", "g2", "g3"],
            "my_color": ["white", "black", "white"],
            "my_rating_before": [1000, 1010, pd.NA],
            "my_rating_change": [8, -7, 5],
            "opponent_rating_after": [992, 1017, 1000],
        }
    ).astype({"my_rating_before": "Int64", "my_rating_change": "Int64", "opponent_rating_after": "Int64"})
    r = calibrate.pre_game_ratings(my_games)
    assert r.loc["g1"].to_dict() == {"white_before": 1000.0, "black_before": 1000.0}  # opponent lost 8 -> was 1000
    assert r.loc["g2"].to_dict() == {"white_before": 1010.0, "black_before": 1010.0}
    assert pd.isna(r.loc["g3", "white_before"]) and r.loc["g3", "black_before"] == 1005.0


# (time class, base seconds, increment seconds): several exact controls, as in the real data.
CONTROLS = [("bullet", 60.0, 0.0), ("bullet", 60.0, 1.0), ("blitz", 180.0, 2.0), ("rapid", 600.0, 0.0)]


def _synthetic(n_games=120, plies=30, seed=0, controls=CONTROLS):
    rng = np.random.default_rng(seed)
    rows, games, my_games = [], [], []
    for g in range(n_games):
        time_class, base, increment = controls[g % len(controls)]
        white_strength = rng.normal()
        score = 1.0 if white_strength > 0.3 else (0.0 if white_strength < -0.3 else 0.5)
        result = {1.0: "1-0", 0.0: "0-1", 0.5: "1/2-1/2"}[score]
        games.append({"uuid": f"g{g}", "pgn_result": result})
        my_games.append({"uuid": f"g{g}", "my_color": "white", "my_rating_before": 1000, "my_rating_change": 0, "opponent_rating_after": 1000})
        for ply in range(1, plies + 1):
            white = ply % 2 == 1
            cp_white = 400 * white_strength + rng.normal(scale=100)
            cp = cp_white if white else -cp_white
            rows.append(
                {
                    "game_uuid": f"g{g}", "ply": ply, "color": "white" if white else "black", "cp_before": cp, "cp_after": cp - 10, "mate_before": pd.NA, "mate_after": pd.NA,
                    "phase": "middlegame", "pieces": 10, "material_white": 30, "material_black": 30, "clock_before_seconds": 100.0, "opponent_clock_seconds": 100.0,
                    "clock_seconds": 95.0, "base_seconds": base, "increment_seconds": increment, "time_class": time_class, "second_cp": cp - 50, "ep_sf_before": 0.5,
                }
            )  # fmt: skip
    a = pd.DataFrame(rows).astype({"mate_before": "Int64", "mate_after": "Int64"})
    return a, pd.DataFrame(games), pd.DataFrame(my_games)


def test_run_writes_consistent_expected_points(tmp_path):
    a, games, my_games = _synthetic()
    out = calibrate.run(a, games, my_games, tmp_path / "ep.parquet", presentation_dir=tmp_path / "presentation", folds=2, n_boot=50, log=lambda _: None)

    assert list(out.columns) == calibrate.OUTPUT_COLUMNS and len(out) == len(a)
    assert out["ep_before"].between(0, 1).all() and out["ep_after"].between(0, 1).all()
    assert (out["ep_loss"] >= 0).all() and (out["ep_criticality"] >= 0).all()
    probs = out[["p_win_before", "p_draw_before", "p_loss_before"]].sum(axis=1)
    assert np.allclose(probs, 1, atol=1e-3)
    # Better evals should mean more expected points, for either colour.
    merged = out.merge(a[["game_uuid", "ply", "cp_before"]], on=["game_uuid", "ply"])
    assert merged["ep_before"].corr(merged["cp_before"]) > 0.8

    # The evaluation is written as CSVs and SVGs, per exact time control as well as overall.
    data = tmp_path / "presentation" / "data" / calibrate.SLUG
    index = pd.read_csv(data / "_index.csv")
    assert set(index["table"]) == {"1_overall.csv", "2_reliability.csv", "3_by_tc.csv", "3b_reliability_by_tc.csv", "3c_metrics_by_tc.csv"}
    overall = pd.read_csv(data / "1_overall.csv")
    assert list(overall["predictor"]) == calibrate.PREDICTORS and (overall["tc_group"] == calibrate.OVERALL).all()
    assert overall.loc[overall["predictor"] == "model", "brier_minus_model"].iloc[0] == 0
    summary = pd.read_csv(data / "3_by_tc.csv")
    assert list(summary["tc_group"]) == ["bullet 1+0", "bullet 1+1", "blitz 3+2", "rapid 10+0"]
    assert (summary["n_games"] == 30).all() and summary["thin"].all()  # 30 games each < MIN_GROUP_GAMES
    by_tc = pd.read_csv(data / "3b_reliability_by_tc.csv")
    assert (by_tc["bins"] == calibrate.THIN_RELIABILITY_BINS).all() and by_tc.groupby("tc_group").size().eq(calibrate.THIN_RELIABILITY_BINS).all()
    reliability = pd.read_csv(data / "2_reliability.csv")
    sizes = reliability.groupby("predictor").size()
    assert sizes["model"] == sizes["lichess_curve"] == calibrate.RELIABILITY_BINS
    assert sizes["stockfish_wdl"] == sizes["ratings_only"] == 1  # constant here (0.5 for all): one tied value, one bin
    assert {"gap_elo", "gap_lo_elo", "gap_hi_elo", "observed_lo", "observed_hi"} <= set(reliability.columns)
    figures = tmp_path / "presentation" / "figures" / calibrate.SLUG
    assert {p.name for p in figures.iterdir()} == {"1_overall.svg", "2_reliability.svg", "3_by_tc.svg", "3b_reliability_by_tc.svg"}


def test_control_groups_orders_exact_controls_and_keeps_daily():
    a = pd.DataFrame(
        {
            "game_uuid": ["a", "b", "c", "d", "e"],
            "time_class": ["blitz", "bullet", "bullet", "daily", "rapid"],
            "base_seconds": [180.0, 60.0, 60.0, np.nan, 600.0],
            "increment_seconds": [2.0, 1.0, 0.0, 0.0, 0.0],
        }
    )
    labels, order = calibrate.control_groups(a)
    assert list(labels) == ["blitz 3+2", "bullet 1+1", "bullet 1+0", "daily", "rapid 10+0"]
    assert order == ["bullet 1+0", "bullet 1+1", "blitz 3+2", "rapid 10+0", "daily"]


def test_run_charts_controls_with_enough_games(tmp_path, monkeypatch):
    # Three games in four are 1+0 (60 of 80, >= MIN_GROUP_GAMES); 3+2 gets the other 20 and stays thin.
    controls = [("bullet", 60.0, 0.0)] * 3 + [("blitz", 180.0, 2.0)]
    a, games, my_games = _synthetic(n_games=80, controls=controls)
    monkeypatch.setattr(calibrate, "DECILE_MIN_GAMES", 55)  # 1+0 gets deciles, 3+2 quintiles
    monkeypatch.setattr(calibrate, "RELIABILITY_MIN_BIN_GAMES", 5)  # some bins thin, most not
    calibrate.run(a, games, my_games, tmp_path / "ep.parquet", presentation_dir=tmp_path / "presentation", folds=2, n_boot=20, log=lambda _: None)

    data = tmp_path / "presentation" / "data" / calibrate.SLUG
    summary = pd.read_csv(data / "3_by_tc.csv").set_index("tc_group")
    assert summary.loc["bullet 1+0", "n_games"] == 60 and not summary.loc["bullet 1+0", "thin"]
    assert summary.loc["blitz 3+2", "n_games"] == 20 and summary.loc["blitz 3+2", "thin"]
    assert summary.loc["bullet 1+0", "reliability_bins"] == calibrate.RELIABILITY_BINS
    by_tc = pd.read_csv(data / "3b_reliability_by_tc.csv")
    sizes = by_tc.groupby("tc_group")["bin"].count()
    assert sizes["bullet 1+0"] == calibrate.RELIABILITY_BINS and sizes["blitz 3+2"] == calibrate.THIN_RELIABILITY_BINS
    assert (by_tc["thin"] == (by_tc["n_games"] < 5)).all()
    figures = tmp_path / "presentation" / "figures" / calibrate.SLUG
    assert (figures / "3_by_tc.svg").stat().st_size > 0 and (figures / "3b_reliability_by_tc.svg").stat().st_size > 0


def test_reliability_bins_have_equal_counts_and_heavy_ties_get_their_own_bin():
    # 60 of 100 predictions are exactly 0.5: they straddle six deciles, so they become one bin of their own;
    # the other 40 keep their equal-count deciles.
    part = pd.DataFrame({"y": [1.0, 0.0] * 50, "game_uuid": [f"g{i // 4}" for i in range(100)], "p": [0.5] * 60 + list(np.linspace(0.6, 0.9, 40))})
    rows = calibrate._reliability(part, "p", 10, n_boot=20)
    assert [r["n_positions"] for r in rows] == [60, 10, 10, 10, 10]
    assert rows[0]["predicted"] == 0.5 and [r["bin"] for r in rows] == [1, 2, 3, 4, 5]
    # Without heavy ties every bin has the same count.
    part["p"] = np.linspace(0, 1, 100)
    assert [r["n_positions"] for r in calibrate._reliability(part, "p", 10, n_boot=20)] == [10] * 10
