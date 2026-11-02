"""Calibrated win probability for this level of play.

Stockfish's centipawns (and its own WDL model) describe engine-vs-engine
outcomes. Here a model is fitted on the player's own games: for every
position a player faced, the features are the engine eval, the phase, both
clocks, the time control and the ratings, and the label is how the game
ended for that player. The result is P(win/draw/loss) and expected points
from the mover's view, which make move quality comparable across phases
and time controls ("expected points lost").

`expected_points.parquet` holds the predictions per move, keyed by
(game_uuid, ply). The model itself is refitted by `chess-analysis calibrate`
and not committed.

The model is checked before it is used: 5-fold cross-validation grouped by
game gives an out-of-fold prediction for every position, which is scored
against the game result next to three simpler predictors (the Lichess cp
curve, Stockfish's WDL, the pre-game rating gap alone), overall and per
exact time control ("bullet 1+0", "blitz 3+2", ...). The evaluation is
written as CSV tables and SVG charts through `toolkit.Results("00_calibration")`
under presentation/ (appendix A of presentation/README.md); no prose.

Run: `.venv/bin/chess-analysis calibrate` (about 4 minutes: five CV fits,
one final fit, ~30 s of bootstraps). Deterministic: single-threaded fits
with fixed seeds, fixed bootstrap seeds.
"""

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold
from threadpoolctl import threadpool_limits

from . import toolkit
from .analysed import CLIP_CP, MATE_CP

EXPECTED_POINTS_FILE = "expected_points.parquet"
FEATURES = [
    "cp", "has_mate_for", "has_mate_against", "ply", "phase_code", "pieces", "material_diff",
    "clock", "opponent_clock", "clock_fraction", "clock_diff", "base_seconds", "increment_seconds",
    "time_class_code", "rating", "rating_diff",
]  # fmt: skip
CATEGORICAL = ["phase_code", "time_class_code"]
PHASES = {"opening": 0, "middlegame": 1, "endgame": 2}
TIME_CLASSES = {"bullet": 0, "blitz": 1, "rapid": 2, "daily": 3}
RESULT_WHITE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}
SLUG = "00_calibration"  # presentation/data/<SLUG>/ and presentation/figures/<SLUG>/
OVERALL = "all"  # tc_group label of the rows that pool every position
# The predictors compared, in chart order: the model and three simpler references.
PREDICTORS = ["model", "lichess_curve", "stockfish_wdl", "ratings_only"]
PREDICTOR_LABELS = {"model": "this model", "lichess_curve": "Lichess cp curve", "stockfish_wdl": "Stockfish WDL", "ratings_only": "ratings only (Elo)"}
# Column holding each predictor's P(win), where it has one. The Lichess curve is a win% that counts draws as
# half, so it is scored as a P(win) too (as Lichess presents it); Stockfish WDL and the Elo formula are
# only scored as expected points here.
PREDICTOR_WIN_PROBABILITY = {"model": "model_p_win", "lichess_curve": "lichess_curve"}
WIN_PROB_CLIP = 1e-6  # keeps log loss finite for a prediction of exactly 0 or 1
N_BOOT = 1000  # game-clustered bootstrap resamples per CI: stable to the third decimal of a Brier score
RELIABILITY_BINS = 10  # deciles, as in the standard reliability diagram
# A control needs this many games for deciles: positions of one game spread over ~3-4 bins, so each decile
# then still holds positions from ~100 games (CI about +-0.1 on its observed score); thinner controls get
# quintiles so each point is not mostly noise.
DECILE_MIN_GAMES = 300
THIN_RELIABILITY_BINS = 5
# A reliability point whose positions come from fewer games than this has a CI of roughly +-0.2 or worse;
# it is flagged thin and left off the per-control chart.
RELIABILITY_MIN_BIN_GAMES = 30
# A single prediction value held by at least this share of a bin's positions that straddles a bin edge gets
# a reliability point of its own (Stockfish WDL says exactly 0 or 1 for ~25% of positions, the Elo formula
# exactly 0.5 when a rating is unknown). Splitting such a tie across equal-count bins would only sort it by
# row order (game date), drawing several points at one x; half a bin is far above chance ties of a
# continuous prediction.
TIE_MIN_SHARE_OF_BIN = 0.5
OUTPUT_COLUMNS = [
    "game_uuid", "ply", "ep_before", "ep_after", "ep_loss", "ep_criticality",
    "p_win_before", "p_draw_before", "p_loss_before", "p_win_after", "p_draw_after", "p_loss_after",
]  # fmt: skip


def lichess_expected_points(cp: pd.Series) -> pd.Series:
    """Lichess's cp -> win% curve (draws counted as half), as a baseline."""
    return 1 / (1 + np.exp(-0.00368208 * cp.clip(-CLIP_CP, CLIP_CP)))


def _side_features(cp, mate, ply, phase, pieces, material_diff, clock, opponent_clock, base, increment, time_class, rating, opponent_rating) -> pd.DataFrame:
    """Feature frame from one side's point of view; every argument is a Series aligned on the same index."""
    cp = cp.clip(-MATE_CP, MATE_CP)
    return pd.DataFrame(
        {
            "cp": cp.clip(-CLIP_CP, CLIP_CP),
            "has_mate_for": (mate.fillna(0) > 0).astype(int),
            "has_mate_against": (mate.fillna(0) < 0).astype(int),
            "ply": ply,
            "phase_code": phase.map(PHASES),
            "pieces": pieces,
            "material_diff": material_diff,
            "clock": clock,
            "opponent_clock": opponent_clock,
            "clock_fraction": clock / base,
            "clock_diff": clock - opponent_clock,
            "base_seconds": base,
            "increment_seconds": increment,
            "time_class_code": time_class.map(TIME_CLASSES),
            "rating": rating,
            "rating_diff": rating - opponent_rating,
        }
    )


def pre_game_ratings(my_games: pd.DataFrame) -> pd.DataFrame:
    """Ratings *before* each game, by colour, indexed by game uuid.

    chess.com stores ratings after the game, which already include this
    game's result, so using them to predict the result would leak. The
    player's pre-game rating is known; the opponent's is estimated as their
    post-game rating plus the player's rating change (Glicko moves are close
    to symmetric). The player's own is missing for the first game in a pool.
    """
    v = my_games.set_index("uuid")
    mine = v["my_rating_before"].astype("float64")
    theirs = v["opponent_rating_after"].astype("float64") + v["my_rating_change"].astype("float64")
    white = v["my_color"] == "white"
    return pd.DataFrame({"white_before": mine.where(white, theirs), "black_before": theirs.where(white, mine)})


def _ratings(a: pd.DataFrame, ratings: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    white = (a["color"] == "white").to_numpy()
    w = ratings["white_before"].reindex(a["game_uuid"]).to_numpy()
    b = ratings["black_before"].reindex(a["game_uuid"]).to_numpy()
    return np.where(white, w, b), np.where(white, b, w)


def training_frame(analysed: pd.DataFrame, games: pd.DataFrame, ratings: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """(features from the mover's view, mover's game score, game id) for every move."""
    g = games.set_index("uuid")[["pgn_result"]]
    a = analysed.reset_index(drop=True)
    white = a["color"] == "white"
    my_rating, their_rating = _ratings(a, ratings)
    material_diff = np.where(white, a["material_white"] - a["material_black"], a["material_black"] - a["material_white"])
    X = _side_features(
        a["cp_before"], a["mate_before"], a["ply"], a["phase"], a["pieces"], pd.Series(material_diff),
        a["clock_before_seconds"], a["opponent_clock_seconds"], a["base_seconds"], a["increment_seconds"],
        a["time_class"], pd.Series(my_rating, dtype="float64"), pd.Series(their_rating, dtype="float64"),
    )  # fmt: skip
    white_score = g["pgn_result"].reindex(a["game_uuid"]).map(RESULT_WHITE).to_numpy()
    y = pd.Series(np.where(white, white_score, 1 - white_score), dtype="float64")
    return X, y, a["game_uuid"].reset_index(drop=True)


def after_move_frame(analysed: pd.DataFrame, ratings: pd.DataFrame, cp: pd.Series | None = None) -> pd.DataFrame:
    """Features for the position after each move, from the *opponent's* view (they're to move).

    `cp` overrides the opponent's eval (mover's view negated), used to score
    the position after the second-best move."""
    a = analysed.reset_index(drop=True)
    white = a["color"] == "white"
    my_rating, their_rating = _ratings(a, ratings)
    # Material after the move isn't stored; the position before is a close enough proxy for a model feature.
    material_diff = np.where(white, a["material_white"] - a["material_black"], a["material_black"] - a["material_white"])
    mover_cp = a["cp_after"] if cp is None else cp
    mover_mate = a["mate_after"] if cp is None else pd.Series(pd.NA, index=a.index, dtype="Int64")
    return _side_features(
        -mover_cp, -mover_mate.astype("float64"), a["ply"] + 1, a["phase"], a["pieces"], pd.Series(-material_diff),
        a["opponent_clock_seconds"], a["clock_seconds"], a["base_seconds"], a["increment_seconds"],
        a["time_class"], pd.Series(their_rating, dtype="float64"), pd.Series(my_rating, dtype="float64"),
    )  # fmt: skip


class WinModel:
    """Expected points (monotone in eval) plus P(win) and P(loss) classifiers; P(draw) is the remainder."""

    def __init__(self, random_state: int = 0):
        cat = [c in CATEGORICAL for c in FEATURES]
        mono = [1 if c == "cp" else 0 for c in FEATURES]
        common = dict(max_iter=300, learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=200, categorical_features=cat, random_state=random_state, early_stopping=False)
        self.ep = HistGradientBoostingRegressor(monotonic_cst=mono, **common)
        self.win = HistGradientBoostingClassifier(monotonic_cst=mono, **common)
        self.loss = HistGradientBoostingClassifier(monotonic_cst=[-m for m in mono], **common)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "WinModel":
        with threadpool_limits(1):
            self.ep.fit(X[FEATURES], y)
            self.win.fit(X[FEATURES], y == 1.0)
            self.loss.fit(X[FEATURES], y == 0.0)
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        with threadpool_limits(1):
            ep = np.clip(self.ep.predict(X[FEATURES]), 0, 1)
            p_win = self.win.predict_proba(X[FEATURES])[:, 1]
            p_loss = self.loss.predict_proba(X[FEATURES])[:, 1]
        p_draw = np.clip(1 - p_win - p_loss, 0, 1)
        total = p_win + p_draw + p_loss
        return pd.DataFrame({"ep": ep, "p_win": p_win / total, "p_draw": p_draw / total, "p_loss": p_loss / total}, index=X.index)


# --- evaluation: out-of-fold predictions, metrics and reliability, written through toolkit.Results ----------


def out_of_fold(X: pd.DataFrame, y: pd.Series, groups: pd.Series, folds: int = 5) -> pd.DataFrame:
    """Predictions for every row from a model that never saw that row's game (GroupKFold by game)."""
    oof = pd.DataFrame(index=X.index, columns=["ep", "p_win", "p_draw", "p_loss"], dtype="float64")
    for train, test in GroupKFold(folds).split(X, y, groups):
        model = WinModel().fit(X.iloc[train], y.iloc[train])
        oof.iloc[test] = model.predict(X.iloc[test]).to_numpy()
    return oof


def ratings_only_expected_points(X: pd.DataFrame) -> pd.Series:
    """Elo expected score from the pre-game rating gap alone (ignores the board); 0.5 where a rating is unknown."""
    return (1 / (1 + 10 ** (-X["rating_diff"] / 400))).fillna(0.5)


def control_groups(analysed: pd.DataFrame) -> tuple[pd.Series, list[str]]:
    """Exact time-control label per row ('bullet 1+0', ..., 'daily') and the labels in reporting order."""
    a = analysed.reset_index(drop=True)
    labels = toolkit.tc_group(a["time_class"], a["base_seconds"], a["increment_seconds"]).fillna(a["time_class"])
    games = pd.DataFrame({"game_uuid": a["game_uuid"], "time_class": a["time_class"], "base_seconds": a["base_seconds"], "increment_seconds": a["increment_seconds"], "tc_group": labels})
    games = games.drop_duplicates("game_uuid")
    order = toolkit.tc_group_order(games)
    order += sorted(set(labels) - set(order))  # daily games have no base time, so tc_group_order leaves them out
    return labels.astype(str), order


def _metrics(part: pd.DataFrame, predictor: str, n_boot: int) -> dict:
    """Accuracy and calibration-in-the-large of one predictor on one set of positions, CIs clustered by game."""
    y, pred, g = part["y"], part[predictor], part["game_uuid"]
    model_sq = (y - part["model"]) ** 2
    row: dict = {"predictor": predictor}

    def put(name: str, values: pd.Series) -> None:
        row[name], row[f"{name}_lo"], row[f"{name}_hi"] = toolkit.cluster_bootstrap_mean(values, g, n_boot=n_boot, seed=0)

    row["predicted_mean"] = float(pred.mean())
    row["observed_mean"] = float(y.mean())
    put("bias", y - pred)
    put("ep_mae", (y - pred).abs())
    put("ep_brier", (y - pred) ** 2)
    put("brier_minus_model", (y - pred) ** 2 - model_sq)
    win_prob = PREDICTOR_WIN_PROBABILITY.get(predictor)
    if win_prob:
        p = part[win_prob].clip(WIN_PROB_CLIP, 1 - WIN_PROB_CLIP)
        won = (y == 1.0).astype(float)
        put("win_log_loss", -(won * np.log(p) + (1 - won) * np.log(1 - p)))
        put("win_brier", (won - part[win_prob]) ** 2)
    else:
        for name in ("win_log_loss", "win_brier"):
            row[name] = row[f"{name}_lo"] = row[f"{name}_hi"] = np.nan
    row["n_positions"] = len(part)
    row["n_games"] = int(g.nunique())
    return row


def _reliability(part: pd.DataFrame, predictor: str, bins: int, n_boot: int) -> list[dict]:
    """Mean predicted vs mean observed score in bins of the predictor's own prediction.

    Bins have equal counts (ranking first, so ties are no obstacle), except that a heavily tied prediction
    value straddling a bin edge (see `TIE_MIN_SHARE_OF_BIN`) is pulled out into a bin of its own; the
    leftover parts of the bins it straddled stay as (smaller) bins. Bins are numbered by mean prediction.
    """
    pred = part[predictor]
    codes = pd.Series(pd.qcut(pred.rank(method="first"), bins, labels=False), index=part.index).astype(float)
    counts = pred.value_counts()
    heavy = counts[counts >= TIE_MIN_SHARE_OF_BIN * len(part) / bins].index
    for k, value in enumerate(sorted(heavy)):
        at = pred == value
        if codes[at].nunique() > 1:
            codes[at] = bins + k  # a new code; renumbered by mean prediction below
    order = pred.groupby(codes).mean().sort_values(kind="stable").index
    codes = codes.map({c: n for n, c in enumerate(order)})
    rows = []
    for b, cell in part.groupby(codes, sort=True):
        observed, lo, hi = toolkit.cluster_bootstrap_mean(cell["y"], cell["game_uuid"], n_boot=n_boot, seed=0)
        predicted = float(cell[predictor].mean())
        rows.append(
            {
                "predictor": predictor, "bins": bins, "bin": int(b) + 1,
                "predicted_from": float(cell[predictor].min()), "predicted_to": float(cell[predictor].max()),
                "predicted": predicted, "observed": observed, "observed_lo": lo, "observed_hi": hi,
                "gap": observed - predicted, "gap_lo": lo - predicted, "gap_hi": hi - predicted,
                "n_positions": len(cell), "n_games": int(cell["game_uuid"].nunique()),
            }
        )  # fmt: skip
    return rows


def evaluate(X: pd.DataFrame, y: pd.Series, groups: pd.Series, oof: pd.DataFrame, ep_sf: pd.Series, tc: pd.Series, tc_order: list[str], n_boot: int = N_BOOT) -> dict[str, tuple[pd.DataFrame, str]]:
    """Every evaluation table (name -> (frame, description)), overall and per exact time control."""
    frame = pd.DataFrame(
        {
            "game_uuid": groups.to_numpy(), "tc_group": tc.to_numpy(), "y": y.to_numpy(),
            "model": oof["ep"].to_numpy(), "model_p_win": oof["p_win"].to_numpy(),
            "lichess_curve": lichess_expected_points(X["cp"]).to_numpy(), "stockfish_wdl": ep_sf.to_numpy(),
            "ratings_only": ratings_only_expected_points(X).to_numpy(),
        }
    )  # fmt: skip
    cells = [(OVERALL, frame)] + [(g, frame[frame["tc_group"] == g]) for g in tc_order]
    metric_rows, reliability_rows = [], []
    for label, part in cells:
        n_games = part["game_uuid"].nunique()
        bins = RELIABILITY_BINS if label == OVERALL or n_games >= DECILE_MIN_GAMES else THIN_RELIABILITY_BINS
        for predictor in PREDICTORS:
            metric_rows.append({"tc_group": label, **_metrics(part, predictor, n_boot)})
            reliability_rows += [{"tc_group": label, **r} for r in _reliability(part, predictor, bins, n_boot)]
    metrics = pd.DataFrame(metric_rows)
    metrics["thin"] = metrics["n_games"] < toolkit.MIN_GROUP_GAMES
    metrics = toolkit.add_elo(metrics, "predicted_mean", ["bias", "bias_lo", "bias_hi"])
    reliability = pd.DataFrame(reliability_rows)
    reliability["thin"] = reliability["n_games"] < RELIABILITY_MIN_BIN_GAMES
    reliability = toolkit.add_elo(reliability, "predicted", ["gap", "gap_lo", "gap_hi"])
    metric_cols = [
        "tc_group", "predictor", "ep_brier", "ep_brier_lo", "ep_brier_hi", "brier_minus_model", "brier_minus_model_lo", "brier_minus_model_hi",
        "ep_mae", "ep_mae_lo", "ep_mae_hi", "win_log_loss", "win_log_loss_lo", "win_log_loss_hi", "win_brier", "win_brier_lo", "win_brier_hi",
        "predicted_mean", "observed_mean", "bias", "bias_lo", "bias_hi", "bias_elo", "bias_lo_elo", "bias_hi_elo", "n_positions", "n_games", "thin",
    ]  # fmt: skip
    reliability_cols = [
        "tc_group", "predictor", "bins", "bin", "predicted_from", "predicted_to", "predicted", "observed", "observed_lo", "observed_hi",
        "gap", "gap_lo", "gap_hi", "gap_elo", "gap_lo_elo", "gap_hi_elo", "n_positions", "n_games", "thin",
    ]  # fmt: skip
    metrics, reliability = metrics[metric_cols], reliability[reliability_cols]
    overall_m, by_tc_m = metrics[metrics["tc_group"] == OVERALL], metrics[metrics["tc_group"] != OVERALL]
    overall_r, by_tc_r = reliability[reliability["tc_group"] == OVERALL], reliability[reliability["tc_group"] != OVERALL]
    by_tc_r = by_tc_r[by_tc_r["predictor"] == "model"]
    # Per control, the model's own numbers next to the best simple baseline's, one row per control.
    summary = by_tc_m.pivot(index="tc_group", columns="predictor", values="ep_brier").reindex(tc_order)
    model_rows = by_tc_m[by_tc_m["predictor"] == "model"].set_index("tc_group").reindex(tc_order)
    lichess_rows = by_tc_m[by_tc_m["predictor"] == "lichess_curve"].set_index("tc_group").reindex(tc_order)
    by_tc = pd.DataFrame(
        {
            "tc_group": tc_order,
            "model_brier": model_rows["ep_brier"].to_numpy(),
            "lichess_brier": summary["lichess_curve"].to_numpy(),
            "stockfish_brier": summary["stockfish_wdl"].to_numpy(),
            "ratings_only_brier": summary["ratings_only"].to_numpy(),
            "lichess_minus_model": lichess_rows["brier_minus_model"].to_numpy(),
            "lichess_minus_model_lo": lichess_rows["brier_minus_model_lo"].to_numpy(),
            "lichess_minus_model_hi": lichess_rows["brier_minus_model_hi"].to_numpy(),
            "share_of_lichess_error_removed": (lichess_rows["brier_minus_model"] / summary["lichess_curve"]).to_numpy(),
            "model_bias": model_rows["bias"].to_numpy(), "model_bias_lo": model_rows["bias_lo"].to_numpy(), "model_bias_hi": model_rows["bias_hi"].to_numpy(),
            "model_bias_elo": model_rows["bias_elo"].to_numpy(), "model_bias_lo_elo": model_rows["bias_lo_elo"].to_numpy(), "model_bias_hi_elo": model_rows["bias_hi_elo"].to_numpy(),
            "model_worst_bin_gap": by_tc_r.assign(abs_gap=by_tc_r["gap"].abs()).groupby("tc_group")["abs_gap"].max().reindex(tc_order).to_numpy(),
            "reliability_bins": by_tc_r.groupby("tc_group")["bins"].first().reindex(tc_order).to_numpy(),
            "n_positions": model_rows["n_positions"].to_numpy(), "n_games": model_rows["n_games"].to_numpy(),
            "thin": model_rows["thin"].to_numpy(),
        }
    )  # fmt: skip
    lichess_all = overall_m.loc[overall_m["predictor"] == "lichess_curve", "ep_brier"].iloc[0]
    # Share of the Lichess curve's squared error that each predictor removes (negative = worse than Lichess).
    overall_m = overall_m.assign(share_of_lichess_error_removed=1 - overall_m["ep_brier"] / lichess_all)
    return {
        "1_overall": (
            overall_m,
            (
                "All positions, out-of-fold: accuracy of the calibrated model and three simpler predictors of the side to move's game score "
                "(Brier = mean squared error, MAE, P(win) log loss and Brier), each minus the model's Brier, and bias = mean observed - mean predicted with Elo equivalent; CIs clustered by game."
            ),
        ),
        "2_reliability": (
            overall_r,
            "All positions, out-of-fold: each predictor's predictions cut into deciles of its own prediction (a value shared by at least TIE_MIN_SHARE_OF_BIN of a decile and straddling an edge is a bin of its own); mean predicted vs mean observed score of the side to move, gap = observed - predicted (Elo equivalent too); CIs clustered by game.",
        ),
        "3_by_tc": (
            by_tc,
            (
                "Per exact time control, one row each: Brier of the model and the baselines, Lichess minus model (CI clustered by game) and the share of the Lichess curve's error it removes, "
                "the model's bias (observed - predicted, Elo equivalent) and its largest decile/quintile gap; thin = fewer than MIN_GROUP_GAMES games."
            ),
        ),
        "3b_reliability_by_tc": (
            by_tc_r,
            "Per exact time control: the model's reliability (deciles, or quintiles below DECILE_MIN_GAMES games), mean predicted vs observed score, gap with Elo equivalent; CIs clustered by game; thin = bin holds positions from fewer than RELIABILITY_MIN_BIN_GAMES games.",
        ),
        "3c_metrics_by_tc": (
            by_tc_m,
            "Per exact time control and predictor: the full metric set of 1_overall (Brier, MAE, P(win) log loss and Brier, bias), CIs clustered by game.",
        ),
    }


def write_evaluation(tables: dict[str, tuple[pd.DataFrame, str]], results: "toolkit.Results") -> None:
    """Tables as CSV and the three charts as SVG under presentation/ (slug `SLUG`)."""
    import matplotlib.pyplot as plt

    from . import charts

    charts.style()
    for name, (frame, description) in tables.items():
        results.table(name, frame, description)

    # 1: Brier per predictor, lower is better.
    overall = tables["1_overall"][0].set_index("predictor").reindex(PREDICTORS).reset_index()
    overall["label"] = overall["predictor"].map(PREDICTOR_LABELS)
    fig, ax = charts.single("How far off is each predictor? (lower is better)", size=(6.5, 2.6))
    charts.estimate(ax, overall, x="label", y="ep_brier", lo="ep_brier_lo", hi="ep_brier_hi", horizontal=True, colour=charts.PLAYER)
    for k, row in overall.iterrows():
        ax.annotate(f"{row['ep_brier']:.3f}", (row["ep_brier"], k), xytext=(8, 4), textcoords="offset points", fontsize=8, color=charts.INK_SECONDARY)
    ax.set_xlabel("Brier score: mean squared gap between predicted and actual game score")
    ax.set_xlim(0, max(0.3, float(overall["ep_brier_hi"].max()) * 1.1))
    ax.set_ylim(len(overall) - 0.4, -0.6)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.24)
    charts.footnote(fig, "Every position of every standard game, both sides; predictions out-of-fold (5 folds by game). Whiskers: 95% CI, games resampled. 0.25 = always guessing 0.5 in a decisive game.")
    results.figure(fig, "1_overall")

    # 2: reliability diagram, every predictor.
    rel = tables["2_reliability"][0]
    fig, ax = charts.single("When a predictor says x, what do players score?", size=(5.6, 4.6))
    ax.plot([0, 1], [0, 1], color=charts.INK_MUTED, linewidth=1, linestyle="--", zorder=1, label="perfect calibration")
    for k, predictor in enumerate(PREDICTORS):
        part = rel[rel["predictor"] == predictor].sort_values("bin")
        colour = charts.SERIES[k]
        if predictor == "model":
            err = np.vstack([part["observed"] - part["observed_lo"], part["observed_hi"] - part["observed"]])
            ax.errorbar(part["predicted"], part["observed"], yerr=err, fmt="o-", color=colour, ecolor=colour, elinewidth=1.5, markersize=5, linewidth=2, label=PREDICTOR_LABELS[predictor], zorder=4)
        else:
            ax.plot(part["predicted"], part["observed"], marker="o", markersize=3.5, linewidth=1.2, color=colour, label=PREDICTOR_LABELS[predictor], zorder=3)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.set_aspect("equal")
    ax.set_xlabel("predicted score of the side to move (mean of a bin)")
    ax.set_ylabel("actual score (mean)")
    ax.legend(loc="upper left", fontsize=7.5)
    charts.footnote(fig, "Each dot is a tenth of all positions, cut by that predictor's own prediction; a value many positions share (Stockfish's exact 0 and 1) is one dot of its own. On the dashed line, a prediction of 0.7 means a score of 0.7.")
    results.figure(fig, "2_reliability")

    # 3: per exact control, the model's edge over the Lichess curve and its bias (calibration in the large).
    summary = tables["3_by_tc"][0]
    shown = list(summary.loc[~summary["thin"], "tc_group"])
    noted = list(summary.loc[summary["thin"], "tc_group"])
    not_shown = " Not shown (too few games): " + ", ".join(noted) + "." if noted else ""
    if not shown:  # every control too thin (only in small test data)
        for name in ("3_by_tc", "3b_reliability_by_tc"):
            fig, ax = charts.single("Too few games in every time control")
            charts.thin_note(ax)
            results.figure(fig, name)
        plt.close("all")
        results.finish()
        return
    part = summary[~summary["thin"]]
    fig, (left, right) = plt.subplots(1, 2, figsize=(8.2, 0.42 * len(shown) + 1.6))
    fig.suptitle("Is the model as good in every time control?", x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    charts.estimate(left, part, x="tc_group", y="lichess_minus_model", lo="lichess_minus_model_lo", hi="lichess_minus_model_hi", order=shown, horizontal=True)
    charts.zero_line(left, horizontal=False)
    left.set_title("Error removed vs the Lichess curve")
    left.set_xlabel("Lichess Brier minus model Brier\n(right of 0 = model better)")
    charts.estimate(right, part, x="tc_group", y="model_bias", lo="model_bias_lo", hi="model_bias_hi", order=shown, horizontal=True)
    charts.zero_line(right, horizontal=False)
    right.set_title("Model bias")
    right.set_xlabel("actual minus predicted score\n(0 = right on average)")
    right.set_yticklabels([])
    for ax in (left, right):
        ax.grid(axis="y", visible=False)
    fig.tight_layout()
    charts.footnote(fig, "Out-of-fold predictions of the one model fitted on all controls, every position. Whiskers: 95% CI, games resampled." + not_shown)
    results.figure(fig, "3_by_tc")

    # 3b: the model's reliability per exact control, drawn as the gap so that small misses are visible.
    by_tc = tables["3b_reliability_by_tc"][0]
    fig, axes = charts.group_grid(shown, panel=(2.4, 2.2), title="Does the model's 70% mean 70% in every time control?")
    for group, ax in axes.items():
        cell = by_tc[(by_tc["tc_group"] == group) & ~by_tc["thin"]].sort_values("bin")
        charts.zero_line(ax)
        ax.fill_between(cell["predicted"], cell["gap_lo"], cell["gap_hi"], color=charts.PLAYER, alpha=0.18, linewidth=0, zorder=2)
        ax.plot(cell["predicted"], cell["gap"], marker="o", markersize=3.5, linewidth=1.6, color=charts.PLAYER, zorder=3)
        ax.set_xlim(0, 1)
        ax.set_xticks([0, 0.5, 1])
        n = int(summary.loc[summary["tc_group"] == group, "n_games"].iloc[0])
        bins = int(by_tc.loc[by_tc["tc_group"] == group, "bins"].iloc[0])
        ax.text(0.97, 0.04, f"{n:,} games, {'deciles' if bins == RELIABILITY_BINS else 'quintiles'}", transform=ax.transAxes, ha="right", va="bottom", fontsize=7, color=charts.INK_MUTED)
    fig.supxlabel("predicted score of the side to move", fontsize=8.5, color=charts.INK_SECONDARY)
    fig.supylabel("actual minus predicted score", fontsize=8.5, color=charts.INK_SECONDARY)
    fig.tight_layout()
    charts.footnote(fig, "Each dot is a decile (quintile below " + f"{DECILE_MIN_GAMES}" + " games) of the model's out-of-fold predictions; 0 = calibrated. Band: 95% CI, games resampled." + not_shown)
    results.figure(fig, "3b_reliability_by_tc")
    plt.close("all")
    results.finish()


def run(analysed: pd.DataFrame, games: pd.DataFrame, my_games: pd.DataFrame, out_path: Path, presentation_dir: Path | None = None, folds: int = 5, n_boot: int = N_BOOT, log=print) -> pd.DataFrame:
    """Cross-validate (evaluation tables and charts into `presentation_dir`), fit on everything, write expected points per move."""
    ratings = pre_game_ratings(my_games)
    X, y, groups = training_frame(analysed, games, ratings)
    if folds:
        oof = out_of_fold(X, y, groups, folds)
        tc, tc_order = control_groups(analysed)
        tables = evaluate(X, y, groups, oof, analysed["ep_sf_before"].reset_index(drop=True), tc, tc_order, n_boot=n_boot)
        overall = tables["1_overall"][0].set_index("predictor")["ep_brier"]
        log("  expected-points Brier (out-of-fold): " + ", ".join(f"{PREDICTOR_LABELS[p]} {overall[p]:.4f}" for p in PREDICTORS))
        if presentation_dir is not None:
            write_evaluation(tables, toolkit.Results(SLUG, root=presentation_dir))

    model = WinModel().fit(X, y)
    before = model.predict(X)
    a = analysed.reset_index(drop=True)
    after_opp = model.predict(after_move_frame(a, ratings))
    second_opp = model.predict(after_move_frame(a, ratings, cp=a["second_cp"]))
    out = pd.DataFrame({"game_uuid": a["game_uuid"], "ply": a["ply"]})
    out["ep_before"] = before["ep"]
    out["ep_after"] = 1 - after_opp["ep"]
    out["ep_loss"] = (out["ep_before"] - out["ep_after"]).clip(lower=0)
    out["ep_criticality"] = (out["ep_before"] - (1 - second_opp["ep"])).clip(lower=0).where(a["second_cp"].notna())
    out["p_win_before"], out["p_draw_before"], out["p_loss_before"] = before["p_win"], before["p_draw"], before["p_loss"]
    out["p_win_after"], out["p_draw_after"], out["p_loss_after"] = after_opp["p_loss"], after_opp["p_draw"], after_opp["p_win"]
    out = out[OUTPUT_COLUMNS].round(4)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False, compression="zstd")
    return out
