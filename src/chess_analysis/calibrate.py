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
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error
from sklearn.model_selection import GroupKFold
from threadpoolctl import threadpool_limits

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


def cross_validate(X: pd.DataFrame, y: pd.Series, groups: pd.Series, ep_sf: pd.Series, folds: int = 5) -> dict:
    """Out-of-fold quality of the model against the Lichess curve and Stockfish WDL baselines."""
    oof = pd.DataFrame(index=X.index, columns=["ep", "p_win", "p_draw", "p_loss"], dtype="float64")
    for train, test in GroupKFold(folds).split(X, y, groups):
        model = WinModel().fit(X.iloc[train], y.iloc[train])
        oof.iloc[test] = model.predict(X.iloc[test]).to_numpy()
    lichess = lichess_expected_points(X["cp"])
    metrics = {
        "rows": int(len(X)),
        "folds": folds,
        "expected_points_mae": {"model": float(mean_absolute_error(y, oof["ep"])), "lichess_curve": float(mean_absolute_error(y, lichess)), "stockfish_wdl": float(mean_absolute_error(y, ep_sf))},
        "expected_points_brier": {"model": float(np.mean((y - oof["ep"]) ** 2)), "lichess_curve": float(np.mean((y - lichess) ** 2)), "stockfish_wdl": float(np.mean((y - ep_sf) ** 2))},
        "win_log_loss": {"model": float(log_loss(y == 1.0, oof["p_win"].clip(1e-6, 1 - 1e-6))), "lichess_curve": float(log_loss(y == 1.0, lichess.clip(1e-6, 1 - 1e-6)))},
        "win_brier": {"model": float(brier_score_loss(y == 1.0, oof["p_win"])), "lichess_curve": float(brier_score_loss(y == 1.0, lichess))},
    }
    # Reliability: mean predicted vs observed expected points in deciles of the model's prediction.
    deciles = pd.qcut(oof["ep"], 10, labels=False, duplicates="drop")
    table = pd.DataFrame({"predicted": oof["ep"], "observed": y, "lichess": lichess, "stockfish": ep_sf}).groupby(deciles).mean().round(3)
    metrics["reliability_by_decile"] = table.to_dict("records")
    return metrics


def write_report(metrics: dict, path: Path) -> None:
    m = metrics
    lines = [
        "# Win-probability calibration",
        "",
        f"Fitted on {m['rows']:,} positions (every move of every standard game, both sides), "
        f"{m['folds']}-fold cross-validation grouped by game. Lower is better.",
        "",
        "| Metric | Model | Lichess cp curve | Stockfish WDL |",
        "|---|---|---|---|",
        f"| Expected points MAE | {m['expected_points_mae']['model']:.4f} | {m['expected_points_mae']['lichess_curve']:.4f} | {m['expected_points_mae']['stockfish_wdl']:.4f} |",
        f"| Expected points Brier | {m['expected_points_brier']['model']:.4f} | {m['expected_points_brier']['lichess_curve']:.4f} | {m['expected_points_brier']['stockfish_wdl']:.4f} |",
        f"| P(win) log loss | {m['win_log_loss']['model']:.4f} | {m['win_log_loss']['lichess_curve']:.4f} | |",
        f"| P(win) Brier | {m['win_brier']['model']:.4f} | {m['win_brier']['lichess_curve']:.4f} | |",
        "",
        "## Reliability by decile of the model's prediction",
        "",
        "| Decile | Model predicted | Observed | Lichess curve | Stockfish WDL |",
        "|---|---|---|---|---|",
    ]
    for i, row in enumerate(m["reliability_by_decile"]):
        lines.append(f"| {i + 1} | {row['predicted']:.3f} | {row['observed']:.3f} | {row['lichess']:.3f} | {row['stockfish']:.3f} |")
    lines += ["", "Observed = mean game score of the side to move. A well-calibrated model has predicted ≈ observed in every row."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(analysed: pd.DataFrame, games: pd.DataFrame, my_games: pd.DataFrame, out_path: Path, report_path: Path | None = None, folds: int = 5, log=print) -> pd.DataFrame:
    """Cross-validate, fit on everything, and write expected points per move."""
    ratings = pre_game_ratings(my_games)
    X, y, groups = training_frame(analysed, games, ratings)
    metrics = cross_validate(X, y, groups, analysed["ep_sf_before"].reset_index(drop=True), folds) if folds else None
    if metrics:
        log(f"  expected-points MAE: model {metrics['expected_points_mae']['model']:.4f}, Lichess curve {metrics['expected_points_mae']['lichess_curve']:.4f}, Stockfish WDL {metrics['expected_points_mae']['stockfish_wdl']:.4f}")
        if report_path:
            write_report(metrics, report_path)
            report_path.with_suffix(".json").write_text(json.dumps(metrics, indent=2))

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
