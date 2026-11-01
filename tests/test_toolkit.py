import numpy as np
import pandas as pd

from chess_analysis import toolkit


def _moves(n_games=40, per_game=20, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for g in range(n_games):
        game_effect = rng.normal(scale=0.01)
        for ply in range(1, per_game + 1):
            player = ply % 2 == 1
            rows.append({"game_uuid": f"g{g}", "ply": ply, "is_player_move": player, "time_class": "blitz" if g % 2 else "bullet", "clock_before_seconds": rng.uniform(0, 180), "ep_loss": max(0.0, 0.03 + game_effect + (0.01 if not player else 0.0) + rng.normal(scale=0.02))})
    return pd.DataFrame(rows)


def test_cluster_bootstrap_mean_matches_plain_mean_and_brackets_it():
    m = _moves()
    est, lo, hi = toolkit.cluster_bootstrap_mean(m["ep_loss"], m["game_uuid"])
    assert abs(est - m["ep_loss"].mean()) < 1e-12 and lo < est < hi
    one = m[m["game_uuid"] == "g0"]
    est1, lo1, hi1 = toolkit.cluster_bootstrap_mean(one["ep_loss"], one["game_uuid"])
    assert np.isnan(lo1) and np.isnan(hi1)  # one game: no CI
    assert all(np.isnan(x) for x in toolkit.cluster_bootstrap_mean(pd.Series([], dtype=float), pd.Series([], dtype=str)))


def test_cluster_bootstrap_generic_statistic():
    m = _moves()
    est, lo, hi = toolkit.cluster_bootstrap(m, "game_uuid", lambda f: f["ep_loss"].median(), n_boot=100)
    assert abs(est - m["ep_loss"].median()) < 1e-12 and lo <= est <= hi


def test_compare_sides_detects_a_built_in_difference():
    m = _moves()
    out = toolkit.compare_sides(m, "ep_loss", by=["time_class"])
    assert set(out["time_class"]) == {"bullet", "blitz"}
    row = out.set_index("time_class").loc["bullet"]
    assert row["player"] < row["opponent"] and row["diff"] < 0 and row["diff_hi"] < 0  # opponents lose 0.01 more by construction
    assert row["games"] == 20 and row["n"] == 400
    plain = toolkit.compare_sides(m, "ep_loss")
    assert len(plain) == 1 and plain.loc[0, "games"] == 40


def test_binned_and_mean_with_ci():
    m = _moves()
    out = toolkit.binned(m, "clock_before_seconds", [0, 60, 120, 180.01], "ep_loss", labels=["<60", "60-120", "120+"])
    assert list(out["bin"]) == ["<60", "60-120", "120+"] and (out["n"] > 0).all()
    mc = toolkit.mean_with_ci(m, "ep_loss", by=["time_class"])
    assert set(mc.columns) == {"time_class", "mean", "lo", "hi", "n", "games"} and len(mc) == 2


def test_elo_rating_band_and_per_game():
    assert toolkit.elo_expected_score(pd.Series([1000.0]), pd.Series([1000.0]))[0] == 0.5
    assert abs(toolkit.elo_expected_score(pd.Series([1200.0]), pd.Series([1000.0]))[0] - 0.7597) < 1e-3
    assert list(toolkit.rating_band(pd.Series([999, 1000, 1150], dtype="Int64"))) == [900, 1000, 1100]
    m = _moves(n_games=2)
    m["side"] = np.where(m["is_player_move"], "player", "opponent")
    pg = toolkit.per_game(m, "ep_loss", "sum")
    assert list(pg.index) == ["g0", "g1"]
    assert abs(pg["g0"] - m[(m.game_uuid == "g0") & m.is_player_move]["ep_loss"].sum()) < 1e-12


def test_format_table_and_report(tmp_path):
    frame = pd.DataFrame({"a": ["x", "y"], "b": [1.23456, np.nan], "c": [1, 2]})
    text = toolkit.format_table(frame, ".2f")
    assert text.splitlines()[0] == "| a | b | c |" and "| x | 1.23 | 1 |" in text and "| y |  | 2 |" in text
    toolkit.figure_style()
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    report = toolkit.Report("99_test", "Test", figures_dir=tmp_path / "figs")
    report.heading("H").text("body").table(frame).figure(fig, "line", "a line")
    path = report.write(tmp_path / "99_test.md")
    written = path.read_text()
    assert written.startswith("# Test\n\n## H\n") and "![a line](figures/99_test/line.png)" in written
    assert (tmp_path / "figs" / "line.png").exists()
    assert toolkit.ci_column(pd.DataFrame({"m": [0.5], "lo": [0.4], "hi": [0.6]}), "m", "lo", "hi", ".1f")[0] == "0.5 [0.4, 0.6]"
