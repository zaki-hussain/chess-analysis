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


def _synthetic(n_games=120, plies=30, seed=0):
    rng = np.random.default_rng(seed)
    rows, games, my_games = [], [], []
    for g in range(n_games):
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
                    "clock_seconds": 95.0, "base_seconds": 180.0, "increment_seconds": 0.0, "time_class": "blitz", "second_cp": cp - 50, "ep_sf_before": 0.5,
                }
            )  # fmt: skip
    a = pd.DataFrame(rows).astype({"mate_before": "Int64", "mate_after": "Int64"})
    return a, pd.DataFrame(games), pd.DataFrame(my_games)


def test_run_writes_consistent_expected_points(tmp_path):
    a, games, my_games = _synthetic()
    out = calibrate.run(a, games, my_games, tmp_path / "ep.parquet", report_path=tmp_path / "report.md", folds=2, log=lambda _: None)

    assert list(out.columns) == calibrate.OUTPUT_COLUMNS and len(out) == len(a)
    assert out["ep_before"].between(0, 1).all() and out["ep_after"].between(0, 1).all()
    assert (out["ep_loss"] >= 0).all() and (out["ep_criticality"] >= 0).all()
    probs = out[["p_win_before", "p_draw_before", "p_loss_before"]].sum(axis=1)
    assert np.allclose(probs, 1, atol=1e-3)
    # Better evals should mean more expected points, for either colour.
    merged = out.merge(a[["game_uuid", "ply", "cp_before"]], on=["game_uuid", "ply"])
    assert merged["ep_before"].corr(merged["cp_before"]) > 0.8
    assert (tmp_path / "report.md").read_text().startswith("# Win-probability calibration")
    assert (tmp_path / "report.json").exists()
