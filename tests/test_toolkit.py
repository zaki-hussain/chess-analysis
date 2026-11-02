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
