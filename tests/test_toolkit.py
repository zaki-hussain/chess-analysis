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


def test_tc_labels_and_group_order():
    assert toolkit.tc_label(60, 0) == "1+0" and toolkit.tc_label(30, 0) == "0.5+0" and toolkit.tc_label(600, 0) == "10+0" and toolkit.tc_label(180, 2) == "3+2"
    games = pd.DataFrame({"time_class": ["blitz", "bullet", "bullet", "rapid", "bullet"], "base_seconds": [180, 60, 60, 600, 120], "increment_seconds": [2, 1, 0, 0, 1]})
    games["tc_group"] = toolkit.tc_group(games["time_class"], games["base_seconds"], games["increment_seconds"])
    assert toolkit.tc_group_order(games) == ["bullet 1+0", "bullet 1+1", "bullet 2+1", "blitz 3+2", "rapid 10+0"]


def test_elo_conversion_is_exact_at_the_ends():
    assert abs(toolkit.elo_from_score(0.5)) < 1e-12
    assert abs(toolkit.elo_from_score(0.7597469) - 200) < 0.01
    # scoring 0.76 where 0.5 was expected is worth ~200 Elo; the conversion is monotone in the excess
    elo = toolkit.excess_to_elo([0.5, 0.5, 0.5], [0.2597469, 0.0, -0.1])
    assert abs(elo[0] - 200) < 0.01 and elo[1] == 0 and elo[2] < 0
    frame = toolkit.add_elo(pd.DataFrame({"exp": [0.6], "x": [0.05], "x_lo": [0.0], "x_hi": [0.1]}), "exp", ["x", "x_lo", "x_hi"])
    assert frame.loc[0, "x_lo_elo"] == 0 < frame.loc[0, "x_elo"] < frame.loc[0, "x_hi_elo"]


def test_shared_model_helpers():
    rng = np.random.default_rng(1)
    groups = np.repeat(np.arange(50), 10)
    x = rng.normal(size=500)
    y = 2.0 * x + 1.0 + rng.normal(scale=0.1, size=500)
    beta, draws = toolkit.cluster_ols(np.column_stack([np.ones(500), x]), y, groups, n_boot=50)
    assert abs(beta[1] - 2.0) < 0.05 and draws.shape == (50, 2)
    est, lo, hi = toolkit.ratio_bootstrap(pd.Series(np.ones(500)), pd.Series(np.full(500, 2.0)), pd.Series(groups), n_boot=50)
    assert est == 0.5 and lo == hi == 0.5
    d = toolkit.dummies(pd.Series(["a", "b", "c"]), "a", "k")
    assert list(d.columns) == ["k=b", "k=c"]
    pre = toolkit.opponent_rating_pre(pd.DataFrame({"my_rating_change": [8, 8], "rated": [True, False], "opponent_rating_after": [1000, 1000]}))
    assert list(pre) == [1008, 1000]


def test_results_writes_csv_svg_and_index(tmp_path):
    from chess_analysis import charts

    charts.style()
    results = toolkit.Results("99_test", root=tmp_path)
    (results.data_dir / "stale.csv").write_text("old")
    results = toolkit.Results("99_test", root=tmp_path)  # a rerun clears old outputs
    assert not (results.data_dir / "stale.csv").exists()
    frame = pd.DataFrame({"tc_group": pd.Categorical(["bullet 1+0"]), "player": [0.1234567891], "player_lo": [0.1], "player_hi": [0.2], "opponent": [0.2], "opponent_lo": [0.1], "opponent_hi": [0.3]})
    path = results.table("1_example", frame, "an example")
    assert path.read_text().splitlines()[1] == "bullet 1+0,0.123457,0.1,0.2,0.2,0.1,0.3"
    fig, axes = charts.group_grid(["bullet 1+0", "bullet 1+1", "blitz 3+2"], title="Example?")
    charts.side_bars(axes["bullet 1+0"], frame, "tc_group")
    svg = results.figure(fig, "1_example")
    first = svg.read_bytes()
    fig, axes = charts.group_grid(["bullet 1+0", "bullet 1+1", "blitz 3+2"], title="Example?")
    charts.side_bars(axes["bullet 1+0"], frame, "tc_group")
    assert results.figure(fig, "1_example").read_bytes() == first  # deterministic SVG
    index = pd.read_csv(results.finish())
    assert list(index["table"]) == ["1_example.csv"] and index.loc[0, "rows"] == 1


def test_iid_bootstrap_helpers_and_ci_cols():
    v = np.array([0.0, 1.0, np.nan, 1.0, 0.0, 1.0])
    est, lo, hi = toolkit.boot_mean(v, n_boot=200, seed=1)
    assert est == 0.6 and 0 <= lo < est < hi <= 1  # NaN dropped
    assert toolkit.boot_mean(v, n_boot=200, seed=1) == (est, lo, hi)  # seeded
    assert np.isnan(toolkit.boot_mean([2.0])[1]) and all(np.isnan(toolkit.boot_mean([])))
    d, dlo, dhi = toolkit.boot_mean_diff(pd.Series([1.0, 1.0, 0.0, 1.0]), [0.0, 0.0, 1.0, np.nan], n_boot=200)
    assert abs(d - (0.75 - 1 / 3)) < 1e-12 and dlo < d < dhi
    assert all(np.isnan(toolkit.boot_mean_diff([1.0], [0.0, 1.0])))
    assert toolkit.ci_cols("x", (1.0, 0.5, 1.5, 7)) == {"x": 1.0, "x_lo": 0.5, "x_hi": 1.5}


def test_chart_helpers():
    import matplotlib.pyplot as plt

    from chess_analysis import charts

    charts.style()
    frame = pd.DataFrame({"g": ["a", "b", "c"], "estimate": [0.1, 0.2, 0.3], "estimate_lo": [0.0, 0.1, 0.2], "estimate_hi": [0.2, 0.3, 0.4], "thin": [False, True, False]})
    # several horizontal series on one axes (or on axes sharing y): first level on top, whatever the number of calls
    fig, (left, right) = plt.subplots(1, 2, sharey=True)
    for ax in (left, right):
        charts.estimate(ax, frame, "g", lo="estimate_lo", hi="estimate_hi", horizontal=True)
    assert left.yaxis_inverted() and right.yaxis_inverted()
    plt.close(fig)

    shown = charts.blank_thin(frame, ["estimate", "estimate_lo", "missing"])
    assert np.isnan(shown.loc[1, "estimate"]) and np.isnan(shown.loc[1, "estimate_lo"]) and shown.loc[1, "estimate_hi"] == 0.3 and frame.loc[1, "estimate"] == 0.2

    fig, axes = charts.group_grid(["bullet 1+0", "bullet 1+1", "blitz 3+2"], sharey=True)
    drawn = {g: charts.panel_estimates(ax, frame if g != "bullet 1+1" else frame.assign(thin=True), "g", ["a", "b", "c"]) for g, ax in axes.items()}
    assert drawn == {"bullet 1+0": True, "bullet 1+1": False, "blitz 3+2": True}
    assert axes["bullet 1+0"].get_xlim() == (-0.5, 2.5)
    charts.finish_grid(fig, axes, drawn, "score", "level", rotate=45)
    assert [ax.get_ylabel() for ax in axes.values()] == ["score", "", "score"]  # first panel of each row
    assert axes["bullet 1+1"].texts[0].get_text() == "too few games"
    assert axes["bullet 1+0"].yaxis.get_tick_params()["labelleft"]  # a thin neighbour leaves the shared y labels alone
    assert axes["blitz 3+2"].get_xticklabels()[0].get_rotation() == 45
    plt.close(fig)

    fig, ax = charts.single()
    charts.thin_note(ax, "none")
    params = ax.yaxis.get_tick_params()
    assert not params["left"] and not params["labelleft"]
    fig2, ax2 = charts.single()
    charts.thin_note(ax2, keep_y=True)
    assert ax2.yaxis.get_tick_params().get("labelleft", True) and not ax2.xaxis.get_tick_params()["labelbottom"]
    plt.close(fig)
    plt.close(fig2)

    fig, ax = charts.single()
    totals = charts.stacked_bars(ax, [0, 1], [[0.2, np.nan], [0.3, 0.5]], ["red", "blue"], labels=["x", "y"], horizontal=True)
    assert list(totals) == [0.5, 0.5]
    bars = ax.patches
    assert len(bars) == 4 and bars[2].get_x() == 0.2 and bars[3].get_x() == 0.0  # the NaN segment adds nothing
    assert ax.get_legend_handles_labels()[1] == ["x", "y"]
    plt.close(fig)

    fig, ax = charts.single()
    image = charts.heatmap(ax, np.array([[0.1, np.nan], [0.9, 0.5]]), plt.get_cmap(charts.SEQUENTIAL), None, texts=[["a", None], ["b", "c"]], text_colours=[["k", "k"], ["w", "k"]], fontsize=7, xlabels=["x1", "x2"], ylabels=["r1", "r2"])
    assert image.get_array().shape == (2, 2)
    assert [(t.get_text(), t.get_position(), t.get_color()) for t in ax.texts] == [("a", (0, 0), "k"), ("b", (0, 1), "w"), ("c", (1, 1), "k")]
    assert [t.get_text() for t in ax.get_yticklabels()] == ["r1", "r2"] and not any(s.get_visible() for s in ax.spines.values())
    plt.close(fig)
