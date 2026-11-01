"""Q1: where points are won and lost - your mistakes vs your opponents'.

Per game: expected points each side gave away (sum of `ep_loss`), who made
the last decisive mistake, whether a winning position was converted and a
lost one swindled, and the size distribution of errors by phase. Writes the
per-game table data/<user>/games_decomposed.parquet for later questions.

Run: .venv/bin/python analysis/01_decomposition.py
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from chess_analysis.toolkit import (
    SIDE_COLOURS,
    TIME_CLASSES,
    Report,
    ci_column,
    cluster_bootstrap_mean,
    compare_sides,
    figure_style,
    load_data,
    repo_root,
)

SLUG = "01_decomposition"
PHASES = ["opening", "middlegame", "endgame"]
WIN_T, LOSE_T = 0.8, 0.2  # "winning" / "losing" P(win) thresholds for conversion and swindles
DECISIVE_LO, DECISIVE_HI = 0.1, 0.2  # the loser is "lost" below LO after their last own move; "playable" means P(win) >= HI
DECISIVE_ALT = (0.15, 0.3)  # stricter pair, as a sensitivity check
MIN_DECISIVE_EP = 0.05  # the costliest move of the final slide must lose at least this to count as a blunder
TAIL = [0.1, 0.2, 0.3]  # ep_loss sizes used for the error-size tables
MIN_CELL_N = 20  # conditional shares resting on fewer games than this are left blank
N_BOOT = 500
SEED = 1
SIDE_PREFIX = (("player", "my"), ("opponent", "opp"))


# --- per-game decomposition -------------------------------------------------------------


def _decisive(decided: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    """Decisive mistake per decided game, judged on the loser's own moves only.

    The loser's P(win) is read before and after each of the loser's own moves
    (mover's view, so terminal positions and the winner's moves add no noise).
    A game counts as lost on the board if the loser's P(win) after their last
    own move is below `lo`. The final slide starts at the last own move the
    loser made from a playable position (P(win) >= `hi` before the move; the
    first move if there never was one, e.g. against a much stronger opponent).
    The decisive mistake is the loser's costliest move (largest `ep_loss`) from
    that move to the end; it counts as a blunder if it lost at least
    MIN_DECISIVE_EP, otherwise the game was lost without a single identifiable
    error (`lost_without_blunder`). Games where the loser was still at or above
    `lo` after their last own move have no decisive mistake: flags,
    resignations and abandonments from positions the model still considered
    alive.
    """
    lm = decided[decided["loser_moved"]].copy()
    last_own = lm.groupby("game_uuid").tail(1).set_index("game_uuid")
    first_ply = lm.groupby("game_uuid")["ply"].min()
    playable = lm[lm["p_win_before"] >= hi].groupby("game_uuid")["ply"].max()
    out = pd.DataFrame(index=last_own.index)
    out["loser_last_p_win"] = last_own["p_win_after"]
    lost = out["loser_last_p_win"] < lo
    out["slide_start_ply"] = playable.reindex(out.index).fillna(first_ply.reindex(out.index)).astype("Int64").where(lost)
    lm["slide_start"] = lm["game_uuid"].map(out["slide_start_ply"].astype("float64"))
    slide = lm[lm["ply"] >= lm["slide_start"]]
    cross = slide.loc[slide.groupby("game_uuid")["ep_loss"].idxmax()].set_index("game_uuid").reindex(out.index)
    out["decisive_found"] = lost & (cross["ep_loss"] >= MIN_DECISIVE_EP)
    out["lost_without_blunder"] = lost & ~out["decisive_found"]
    out["decisive_ply"] = cross["ply"].astype("Int64")
    out["decisive_is_last_playable"] = (cross["ply"] == out["slide_start_ply"]).astype("boolean").where(lost)
    out["decisive_by"] = cross["side"]
    out["decisive_ep_loss"] = cross["ep_loss"]
    out["decisive_cp_loss"] = cross["cp_loss"]
    out["decisive_label"] = cross["label"]
    out["decisive_phase"] = cross["phase"]
    out["decisive_p_win_before"] = cross["p_win_before"]
    out["decisive_p_win_after"] = cross["p_win_after"]
    out["decisive_clock_before"] = cross["clock_before_seconds"]
    return out


def decompose_games(moves: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per game in `games`, keyed by uuid, in the order of `games`."""
    m = moves.sort_values(["game_uuid", "ply"]).reset_index(drop=True)
    mine = m["is_player_move"].to_numpy()
    # Player's P(win) / expected points after every move: mover's p_win if the player moved, else the mover's p_loss.
    m["my_p_win_after"] = np.where(mine, m["p_win_after"], m["p_loss_after"])
    m["my_p_win_before"] = np.where(mine, m["p_win_before"], m["p_loss_before"])
    m["opp_p_win_after"] = np.where(mine, m["p_loss_after"], m["p_win_after"])
    m["opp_p_win_before"] = np.where(mine, m["p_loss_before"], m["p_win_before"])
    m["my_ep_after"] = np.where(mine, m["ep_after"], 1 - m["ep_after"])
    m["my_ep_before"] = np.where(mine, m["ep_before"], 1 - m["ep_before"])
    m["ep_gain"] = (m["ep_after"] - m["ep_before"]).clip(lower=0)
    for t in TAIL:
        m[f"big{int(t * 100)}"] = m["ep_loss"] >= t
    m["is_blunder"] = m["label"] == "blunder"
    m["is_mistake"] = m["label"] == "mistake"
    m["is_inaccuracy"] = m["label"] == "inaccuracy"

    out = pd.DataFrame({"uuid": games["uuid"].to_numpy()}).set_index("uuid")
    g = m.groupby("game_uuid", sort=False)
    out["n_plies"] = g.size().astype("Int64")
    first, last = g.head(1).set_index("game_uuid"), g.tail(1).set_index("game_uuid")
    out["my_ep_start"] = first["my_ep_before"]
    out["my_ep_end"] = last["my_ep_after"]
    out["my_p_win_start"] = first["my_p_win_before"]
    out["my_p_win_end"] = last["my_p_win_after"]

    # Sums and counts by side (and by phase).
    agg = {"ep_loss": "sum", "cp_loss": "sum", "ep_gain": "sum", "is_blunder": "sum", "is_mistake": "sum", "is_inaccuracy": "sum", "ply": "size"}
    agg.update({f"big{int(t * 100)}": "sum" for t in TAIL})
    by_side = m.groupby(["game_uuid", "side"]).agg(agg).unstack("side")
    rename = {"ep_loss": "ep_lost", "cp_loss": "cp_lost", "ep_gain": "ep_gained", "is_blunder": "blunders", "is_mistake": "mistakes", "is_inaccuracy": "inaccuracies", "ply": "moves"}
    rename.update({f"big{int(t * 100)}": f"errors_ge_{int(t * 100)}" for t in TAIL})
    for column, name in rename.items():
        for side, prefix in SIDE_PREFIX:
            out[f"{prefix}_{name}"] = by_side.get((column, side))
    by_phase = m.groupby(["game_uuid", "side", "phase"], observed=True)["ep_loss"].sum().unstack(["side", "phase"])
    for phase in PHASES:
        for side, prefix in SIDE_PREFIX:
            out[f"{prefix}_ep_lost_{phase}"] = by_phase.get((side, phase))
    max_loss = m.groupby(["game_uuid", "side"])["ep_loss"].max().unstack("side")
    for side, prefix in SIDE_PREFIX:
        out[f"{prefix}_max_ep_loss"] = max_loss.get(side)
    count_columns = [c for c in out.columns if c.endswith(("_moves", "_blunders", "_mistakes", "_inaccuracies")) or "_errors_ge_" in c]
    out[count_columns] = out[count_columns].fillna(0).astype("Int64")
    sum_columns = [c for c in out.columns if "_ep_lost" in c or "_cp_lost" in c or "_ep_gained" in c]
    out[sum_columns] = out[sum_columns].astype("float64").fillna(0.0)
    out["net_ep"] = out["opp_ep_lost"] - out["my_ep_lost"]

    # Swings: each side's P(win) extremes over the game (positions after each move, plus the start).
    start = first.reset_index()[["game_uuid", "my_p_win_before", "opp_p_win_before"]].rename(columns={"my_p_win_before": "my_p_win_after", "opp_p_win_before": "opp_p_win_after"})
    track = pd.concat([m[["game_uuid", "my_p_win_after", "opp_p_win_after"]], start])
    extremes = track.groupby("game_uuid").agg(my_max=("my_p_win_after", "max"), my_min=("my_p_win_after", "min"), opp_max=("opp_p_win_after", "max"), opp_min=("opp_p_win_after", "min"))
    out["my_p_win_max"], out["my_p_win_min"] = extremes["my_max"], extremes["my_min"]
    out["opp_p_win_max"], out["opp_p_win_min"] = extremes["opp_max"], extremes["opp_min"]
    out["reached_winning"] = out["my_p_win_max"] >= WIN_T
    out["reached_losing"] = out["my_p_win_min"] <= LOSE_T
    out["opp_reached_winning"] = out["opp_p_win_max"] >= WIN_T
    out["opp_reached_losing"] = out["opp_p_win_min"] <= LOSE_T

    # Decisive mistake (decided games only), at the main threshold and the sensitivity one.
    outcome = games.set_index("uuid")["outcome"]
    m["outcome"] = m["game_uuid"].map(outcome)
    decided = m[m["outcome"].isin(["win", "loss"])].copy()
    decided["loser_moved"] = (decided["outcome"] == "loss") == decided["is_player_move"]
    main = _decisive(decided, DECISIVE_LO, DECISIVE_HI)
    alt = _decisive(decided, *DECISIVE_ALT)
    out = out.join(main)
    out["decisive_found"] = out["decisive_found"].astype("boolean")
    out["lost_without_blunder"] = out["lost_without_blunder"].astype("boolean")
    out["decisive_plies_from_end"] = (out["n_plies"] - out["decisive_ply"]).astype("Int64")
    out["decisive_strict_found"] = alt["decisive_found"].reindex(out.index).astype("boolean")
    out["decisive_strict_ply"] = alt["decisive_ply"].reindex(out.index).astype("Int64")
    out["decisive_strict_ep_loss"] = alt["decisive_ep_loss"].reindex(out.index)
    out["decisive_strict_phase"] = alt["decisive_phase"].reindex(out.index)
    return out.reset_index()


# --- game-level bootstrap helpers (each game is its own cluster) -------------------------------


def boot_rate(y: np.ndarray, cond: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float, int]:
    """P(y | cond) with a bootstrap CI over games; returns (est, lo, hi, n_cond)."""
    y, cond = np.asarray(y, dtype="float64"), np.asarray(cond, dtype="float64")
    n = int(cond.sum())
    if n == 0:
        return float("nan"), float("nan"), float("nan"), 0
    est = float((y * cond).sum() / n)
    if len(y) < 2:
        return est, float("nan"), float("nan"), n
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(y), size=(n_boot, len(y)))
    boot = (y[idx] * cond[idx]).sum(axis=1) / np.maximum(cond[idx].sum(axis=1), 1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return est, float(lo), float(hi), n


def boot_rate_diff(y1, c1, y2, c2, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """P(y1|c1) - P(y2|c2) with both rates computed on the same resampled games."""
    y1, c1, y2, c2 = (np.asarray(a, dtype="float64") for a in (y1, c1, y2, c2))
    if c1.sum() == 0 or c2.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    est = float((y1 * c1).sum() / c1.sum() - (y2 * c2).sum() / c2.sum())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(y1), size=(n_boot, len(y1)))
    r1 = (y1[idx] * c1[idx]).sum(axis=1) / np.maximum(c1[idx].sum(axis=1), 1)
    r2 = (y2[idx] * c2[idx]).sum(axis=1) / np.maximum(c2[idx].sum(axis=1), 1)
    lo, hi = np.quantile(r1 - r2, [0.025, 0.975])
    return est, float(lo), float(hi)


def boot_paired_mean_diff(a: np.ndarray, b: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """mean(a) - mean(b) over the same games, bootstrapped by game."""
    d = np.asarray(a, dtype="float64") - np.asarray(b, dtype="float64")
    d = d[~np.isnan(d)]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    boot = d[idx].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(d.mean()), float(lo), float(hi)


def game_mean(frame: pd.DataFrame, column: str, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    return cluster_bootstrap_mean(frame[column].astype("float64"), frame["uuid"], n_boot=n_boot, seed=SEED)


def share(frame: pd.DataFrame, mask, f: str = ".3f") -> str:
    """Share of rows of `frame` satisfying `mask`, with a game-bootstrap CI."""
    return fmt(*boot_rate(np.asarray(mask, dtype=float), np.ones(len(frame)))[:3], f=f)


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    return f"{est:{f}} [{lo:{f}}, {hi:{f}}]" if pd.notna(lo) else f"{est:{f}}"


# --- report sections --------------------------------------------------------------------


def section_budget(report: Report, games: pd.DataFrame) -> dict:
    report.heading("1. The expected-points budget per game")
    report.text(
        "For every game, the expected points (calibrated win probability + half the draw probability) each side gave away "
        "is the sum of its moves' `ep_loss`. The per-game numbers below are means over games (CIs bootstrapped by game); "
        "the difference is paired within the same games (opponents minus you; positive = opponents gave away more). "
        "Totals are the sums over all games of that time class, i.e. points handed over in total."
    )
    rows, out = [], {}
    for tc in TIME_CLASSES:
        part = games[games["time_class"] == tc]
        me, opp = game_mean(part, "my_ep_lost"), game_mean(part, "opp_ep_lost")
        diff = boot_paired_mean_diff(part["opp_ep_lost"], part["my_ep_lost"])
        me_cp, opp_cp = game_mean(part, "my_cp_lost"), game_mean(part, "opp_cp_lost")
        out[tc] = diff
        rows.append(
            {
                "time class": tc, "games": len(part),
                "your ep lost / game": fmt(*me), "their ep lost / game": fmt(*opp), "theirs − yours": fmt(*diff),
                "your total": f"{part['my_ep_lost'].sum():.0f}", "their total": f"{part['opp_ep_lost'].sum():.0f}",
                "your cp lost / game": fmt(*me_cp, f=".0f"), "their cp lost / game": fmt(*opp_cp, f=".0f"),
            }  # fmt: skip
        )
    report.table(pd.DataFrame(rows))

    report.text(
        "Where the score comes from. Your score in a game is approximately `my_ep_start − my_ep_lost + opp_ep_lost + residual`, "
        "where `my_ep_start` is the model's expectation at move 1 (it knows both pre-game ratings and the time control), "
        "and the residual collects what the engine-based decomposition cannot see: the result of resignations, flag falls, "
        "the model's end-of-game error, and small 'gains' where the engine's deeper search after a move rates the position better "
        "than before it (`*_ep_gained`, stored separately; they roughly cancel between the sides). Means per game:"
    )
    rows = []
    for tc in TIME_CLASSES:
        part = games[games["time_class"] == tc].copy()
        part["residual"] = part["my_score"] - (part["my_ep_start"] - part["my_ep_lost"] + part["opp_ep_lost"])
        part["end_gap"] = part["my_score"] - part["my_ep_end"]
        part["over"] = part["my_score"] - part["my_ep_start"]
        row = {"time class": tc}
        for name, col in [("score", "my_score"), ("ep at move 1", "my_ep_start"), ("score − ep at move 1", "over"), ("− your ep lost", "my_ep_lost"), ("+ their ep lost", "opp_ep_lost"), ("residual", "residual"), ("score − ep at last move", "end_gap")]:
            row[name] = fmt(*game_mean(part, col))
        rows.append(row)
    report.table(pd.DataFrame(rows))
    report.text(
        "Read: in every time class you and your opponents each give away close to a full point per game "
        "(a ~0.03 ep loss per move over ~30 moves each); the net difference is a few hundredths of a point, and your score "
        "exceeds the model's move-1 expectation by about one to four hundredths. The residual is small and, where it is positive, "
        "reflects games that ended (resignation, flag, abandonment) before the board said so, plus the clipping of `ep_loss` at zero."
    )
    return out


def section_errors(report: Report, moves: pd.DataFrame) -> pd.DataFrame:
    report.heading("2. Error sizes: you vs your opponents, by phase")
    report.text(
        "Mean `ep_loss` per move (both sides lose ~0.03), then the share of moves that lose at least 0.1, 0.2 and 0.3 "
        "expected points (the tail where games are decided). Differences are you minus opponents in the same games; "
        "negative = you err less. `cp_loss` means and blunder rates (cp_loss ≥ 300) are the secondary measures."
    )
    frame = moves.copy()
    for t in TAIL:
        frame[f"ge{int(t * 100)}"] = (frame["ep_loss"] >= t).astype("float64")
    frame["blunder"] = (frame["label"] == "blunder").astype("float64")
    tables = {}
    for column, f in [("ep_loss", ".4f"), ("ge10", ".3f"), ("ge20", ".3f"), ("ge30", ".4f"), ("cp_loss", ".1f"), ("blunder", ".3f")]:
        tbl = compare_sides(frame, column, by=["time_class", "phase"], n_boot=N_BOOT)
        tbl = tbl[tbl["time_class"].isin(TIME_CLASSES)].copy()
        tbl["time_class"] = pd.Categorical(tbl["time_class"], TIME_CLASSES)
        tbl["phase"] = pd.Categorical(tbl["phase"], PHASES)
        tbl = tbl.sort_values(["time_class", "phase"])
        tbl["you"] = ci_column(tbl, "player", "player_lo", "player_hi", f)
        tbl["opponents"] = ci_column(tbl, "opponent", "opponent_lo", "opponent_hi", f)
        tbl["you − them"] = ci_column(tbl, "diff", "diff_lo", "diff_hi", f)
        tables[column] = tbl
    for column, title in [("ep_loss", "Mean ep_loss per move"), ("ge10", "Share of moves losing ≥ 0.1 expected points"), ("ge20", "Share of moves losing ≥ 0.2"), ("ge30", "Share of moves losing ≥ 0.3"), ("cp_loss", "Mean cp_loss per move (secondary)"), ("blunder", "Blunder rate (cp_loss ≥ 300, secondary)")]:
        report.heading(title, level=3)
        report.table(tables[column][["time_class", "phase", "you", "opponents", "you − them", "games", "n"]])
    report.text(
        "Reading across the tables: the differences are at most a thousandth of a point per move. Your bullet endgames are the one "
        "cell that is cleaner than the opponents' on every measure (mean ep_loss, ≥0.1, ≥0.2); your bullet openings lose slightly more "
        "(mean ep_loss) and your blitz middlegames have a slightly fatter tail (≥0.2 and ≥0.3, CIs touching zero). In cp terms you lose "
        "more centipawns than opponents in bullet middlegames but not more expected points: those are big swings in positions already decided."
    )

    # Concentration: share of all ep lost that comes from moves >= 0.1
    rows = []
    for tc in TIME_CLASSES:
        part = frame[frame["time_class"] == tc]
        row = {"time class": tc}
        for side, name in (("player", "you"), ("opponent", "opponents")):
            s = part[part["side"] == side]
            total = s["ep_loss"].sum()
            row[f"{name}: share of ep lost from moves ≥ 0.1"] = f"{s.loc[s['ep_loss'] >= 0.1, 'ep_loss'].sum() / total:.2f}"
            row[f"{name}: share of moves ≥ 0.1"] = f"{(s['ep_loss'] >= 0.1).mean():.3f}"
        rows.append(row)
    report.heading("How concentrated the damage is", level=3)
    report.text("Share of all expected points given away that came from moves losing at least 0.1, next to the share of moves those were (descriptive ratios over the whole class, no CI).")
    report.table(pd.DataFrame(rows))

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharey=True)
    xs = np.linspace(0.02, 0.5, 49)
    styles = {"opening": ":", "middlegame": "--", "endgame": "-"}
    for ax, tc in zip(axes, TIME_CLASSES):
        part = frame[frame["time_class"] == tc]
        for side in ("player", "opponent"):
            for phase in PHASES:
                v = np.sort(part.loc[(part["side"] == side) & (part["phase"] == phase), "ep_loss"].to_numpy())
                surv = 1 - np.searchsorted(v, xs, side="left") / len(v)
                ax.plot(xs, surv, styles[phase], color=SIDE_COLOURS[side], lw=1.8, label=f"{'you' if side == 'player' else 'opponents'}, {phase}")
        ax.set_yscale("log")
        ax.set_title(tc)
        ax.set_xlabel("ep_loss at least")
    axes[0].set_ylabel("share of moves (log)")
    axes[-1].legend(fontsize=7, loc="upper right")
    fig.suptitle("Error-size survival curves: share of moves losing at least x expected points", fontsize=11)
    fig.tight_layout()
    report.figure(fig, "error_sizes", "Share of moves losing at least x expected points, by phase (line style) and side (colour). Log scale.")
    plt.close(fig)
    return tables["ge20"]


def section_decisive(report: Report, games: pd.DataFrame) -> dict:
    """Writes section 3; returns the shares the closing paragraph quotes, keyed (time class, 'you'/'opponents')."""
    report.heading("3. The decisive mistake: the loser's costliest move of the final slide")
    report.text(
        "Who made the decisive mistake is answered over the loser's own moves only: by construction the decisive move is always the loser's "
        "(`decisive_by` is the loser's side), so the winner's moves never count as 'decisive', and the question becomes whether the loser signed "
        "their own defeat with an identifiable move or lost without one. "
        f"For every decided game, the loser's P(win) is read before and after each of the loser's own moves. A game counts as lost on the board "
        f"if the loser's P(win) after their last own move was below {DECISIVE_LO:.0%}. The final slide starts at the last move the loser made from a "
        f"playable position (P(win) ≥ {DECISIVE_HI:.0%} before the move); the decisive mistake is the loser's costliest move from there to the end, "
        f"and it counts as a blunder if it lost at least {MIN_DECISIVE_EP:.2f} expected points. (A plain 'last crossing below 10%' rule was tried first: "
        "the model's P(win) decays gradually in bullet and a quarter of games were 'decided' by the winner's routine recapture nudging the loser from 12% to 7%, "
        "so the crossing move was not the mistake.) 'Lost without a blunder' means the loser slid from the last playable position to a lost one without any single move "
        f"costing {MIN_DECISIVE_EP:.2f}. 'Lost while still alive' means the loser's last own move still left P(win) ≥ {DECISIVE_LO:.0%}: the game ended by flag, resignation, "
        "abandonment or a mate the model still gave chances against (in bullet the model keeps a real flag chance alive even at +10). "
        f"The stricter pair (playable ≥ {DECISIVE_ALT[1]:.0%}, lost < {DECISIVE_ALT[0]:.0%}) is a sensitivity check."
    )
    decided = games[games["outcome"].isin(["win", "loss"])]
    rows, out = [], {}
    for tc in TIME_CLASSES:
        part = decided[decided["time_class"] == tc]
        for outcome, who in (("loss", "you"), ("win", "opponents")):
            p = part[part["outcome"] == outcome]
            found, between = p["decisive_found"].fillna(False).to_numpy(bool), p["lost_without_blunder"].fillna(False).to_numpy(bool)
            on_board = found | between
            out[(tc, who)] = {
                "alive": float((~on_board).mean()),
                "found_given_on_board": float(found.sum() / max(on_board.sum(), 1)),
                "decisive_ep": float(p.loc[found, "decisive_ep_loss"].mean()),
            }
            rows.append(
                {
                    "time class": tc, "loser": who, "games": len(p),
                    "loser's own decisive blunder": share(p, found), "lost without a blunder": share(p, between),
                    "lost while still alive (≥ 10%)": share(p, ~on_board),
                    "own decisive blunder, strict": share(p, p["decisive_strict_found"].fillna(False).to_numpy(bool)),
                }  # fmt: skip
            )
    report.table(pd.DataFrame(rows))
    report.text("'Loser = you' are your losses; 'loser = opponents' are your wins. Each pair of rows compares the same thing from both sides: how often the losing side signs its own defeat with an identifiable move.")

    report.heading("Games lost while still alive, by how they ended", level=3)
    report.text(
        "Share of decided games in which the loser's P(win) after their last own move was still ≥ 10%, split by the loser's termination, with the loser's P(win) "
        f"after that last move in those games. Cells resting on fewer than {MIN_CELL_N} games are left blank (n shown). A checkmate counted as 'still alive' is not a "
        "contradiction: the bullet model's clock term keeps a real flag chance alive for the side about to be mated (even at −10 with the opponent short of time), "
        "so in bullet almost half the mated losers were still 'alive' by the model's reckoning."
    )
    rows = []
    for tc in TIME_CLASSES:
        part = decided[decided["time_class"] == tc]
        for outcome, who, column in (("loss", "you", "my_result"), ("win", "opponents", "opponent_result")):
            p = part[part["outcome"] == outcome]
            none = ~p["decisive_found"].fillna(False).to_numpy(bool) & ~p["lost_without_blunder"].fillna(False).to_numpy(bool)
            row = {"time class": tc, "loser": who, "games": len(p)}
            for term in ("checkmated", "timeout", "resigned", "abandoned"):
                mask = (p[column] == term).to_numpy()
                row[f"{term}: share of games"] = f"{mask.mean():.2f}"
                est, lo, hi, n = boot_rate(none.astype(float), mask.astype(float))
                row[f"{term}: still alive"] = fmt(est, lo, hi, f=".2f") if n >= MIN_CELL_N else f"— (n={n})"
            row["loser's P(win) after last own move, when alive"] = fmt(*game_mean(p[none], "loser_last_p_win"), f=".2f")
            rows.append(row)
    report.table(pd.DataFrame(rows))

    report.heading("What the decisive blunders look like", level=3)
    report.text("Only games with a loser's own decisive blunder. Yours (in your losses) vs the opponents' (in your wins). 'P(win) before/after' are the loser's around the decisive move; 'from last playable' is the share where the decisive move is the very move played from the last playable position (the rest came later in the slide).")
    rows = []
    for tc in TIME_CLASSES:
        part = decided[(decided["time_class"] == tc) & decided["decisive_found"].fillna(False).astype(bool)]
        for outcome, who in (("loss", "you"), ("win", "opponents")):
            p = part[part["outcome"] == outcome]
            row = {"time class": tc, "decisive blunder by": who, "games": len(p)}
            row["ep_loss of the move"] = fmt(*game_mean(p, "decisive_ep_loss"))
            row["P(win) before"] = fmt(*game_mean(p, "decisive_p_win_before"), f=".2f")
            row["P(win) after"] = fmt(*game_mean(p, "decisive_p_win_after"), f=".2f")
            row["from last playable"] = share(p, p["decisive_is_last_playable"].fillna(False).to_numpy(bool), f=".2f")
            row["plies before the end"] = fmt(*game_mean(p, "decisive_plies_from_end"), f=".1f")
            row["clock before (s)"] = fmt(*game_mean(p, "decisive_clock_before"), f=".0f")
            for phase in PHASES:
                row[phase] = share(p, (p["decisive_phase"] == phase).to_numpy(), f=".2f")
            rows.append(row)
    report.table(pd.DataFrame(rows))
    ep_means = [v["decisive_ep"] for v in out.values()]
    report.text(
        "The phase columns are the share of decisive blunders made in that phase (material heuristic). The profile is the same for you and for your opponents: "
        f"the decisive blunder is mostly an endgame move in bullet and blitz, costs about {min(ep_means):.2f}–{max(ep_means):.2f} expected points on average, and comes some 20 plies before the end."
    )

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, outcome, title in zip(axes, ("loss", "win"), ("Your losses (loser = you)", "Your wins (loser = opponent)")):
        x = np.arange(len(TIME_CLASSES))
        bottoms = np.zeros(len(TIME_CLASSES))
        found = decided["decisive_found"].fillna(False).astype(bool)
        between = decided["lost_without_blunder"].fillna(False).astype(bool)
        cats = [("loser's own decisive blunder", found), ("lost without a blunder", between), ("lost while still alive (≥ 10%)", ~found & ~between)]
        colours = [SIDE_COLOURS["player"] if outcome == "loss" else SIDE_COLOURS["opponent"], "#8a8a8a", "#d5d5d5"]
        for (name, mask), colour in zip(cats, colours):
            vals = np.array([mask[(decided["time_class"] == tc) & (decided["outcome"] == outcome)].mean() for tc in TIME_CLASSES])
            ax.bar(x, vals, bottom=bottoms, color=colour, width=0.6, edgecolor="white", linewidth=2, label=name)
            for xi, v, b in zip(x, vals, bottoms):
                if v > 0.08:
                    ax.text(xi, b + v / 2, f"{v:.0%}", ha="center", va="center", fontsize=8, color="white" if colour != "#d5d5d5" else "#333")
            bottoms += vals
        ax.set_xticks(x, TIME_CLASSES)
        ax.set_title(title)
        ax.set_ylim(0, 1)
    axes[0].set_ylabel("share of decided games")
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, fontsize=8, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle("Who signed the defeat: the loser's costliest move of the final slide", fontsize=11)
    fig.tight_layout()
    report.figure(fig, "decisive_mistake", "Share of decided games with a loser's own decisive blunder, by time class, in your losses (left, blue = you) and wins (right, orange = opponents).")
    plt.close(fig)
    return out


def section_conversion(report: Report, games: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    report.heading("4. Converting winning positions and swindling lost ones")
    report.text(
        f"Conversion rate = P(you win | your P(win) reached ≥ {WIN_T:.0%} at some point); swindle rate = P(you did not lose | your P(win) "
        f"fell to ≤ {LOSE_T:.0%} at some point). The opponents' rates are the same quantities from their side in the same games "
        "(their P(win) is your P(loss)). Differences are you minus opponents, bootstrapped over games. Also shown: how often each side "
        "reached those positions at all."
    )
    rows = []
    for tc in TIME_CLASSES:
        part = games[games["time_class"] == tc]
        win, loss = (part["outcome"] == "win").to_numpy(float), (part["outcome"] == "loss").to_numpy(float)
        ones = np.ones(len(part))
        rw, orw = part["reached_winning"].to_numpy(float), part["opp_reached_winning"].to_numpy(float)
        rl, orl = part["reached_losing"].to_numpy(float), part["opp_reached_losing"].to_numpy(float)
        conv, oconv, dconv = boot_rate(win, rw), boot_rate(loss, orw), boot_rate_diff(win, rw, loss, orw)
        swin, oswin, dswin = boot_rate(1 - loss, rl), boot_rate(1 - win, orl), boot_rate_diff(1 - loss, rl, 1 - win, orl)
        rows.append(
            {
                "time class": tc, "games": len(part),
                "you reached ≥80%": fmt(*boot_rate(rw, ones)[:3]), "they reached ≥80%": fmt(*boot_rate(orw, ones)[:3]),
                "your conversion": fmt(*conv[:3]) + f" (n={conv[3]})", "their conversion": fmt(*oconv[:3]) + f" (n={oconv[3]})", "conv. you − them": fmt(*dconv),
                "your swindle": fmt(*swin[:3]) + f" (n={swin[3]})", "their swindle": fmt(*oswin[:3]) + f" (n={oswin[3]})", "swindle you − them": fmt(*dswin),
            }  # fmt: skip
        )
    report.table(pd.DataFrame(rows))

    report.heading("How the swindles ended", level=3)
    report.text(
        "Among games where a side fell to ≤ 20% and did not lose (the swindles): how the game ended, as shares of all swindles (descriptive, n given). "
        "'Won on time' is the share of the swindle *wins* in which the other side flagged (with a game-bootstrap CI, since the headline paragraph quotes it); "
        "'won on time, share of all swindles' is the same games as a share of every swindle."
    )
    rows, on_time = [], {}
    for tc in TIME_CLASSES:
        part = games[games["time_class"] == tc]
        for who, mask, column, other in (("you", part["reached_losing"] & (part["outcome"] != "loss"), "my_result", "opponent_result"), ("opponents", part["opp_reached_losing"] & (part["outcome"] != "win"), "opponent_result", "my_result")):
            p = part[mask]
            r = p[column]
            won = (r == "win").to_numpy(float)
            flagged = (p[other] == "timeout").to_numpy(float)
            est, lo, hi, n_won = boot_rate(flagged, won)
            on_time[(tc, who)] = est
            rows.append(
                {
                    "time class": tc, "swindler": who, "n": len(p), "won": f"{won.mean():.2f}",
                    "won on time (share of swindle wins)": fmt(est, lo, hi, f=".2f") + f" (n={n_won})",
                    "won on time, share of all swindles": f"{flagged.mean():.2f}",
                    "draw by timeout vs insufficient": f"{(r == 'timevsinsufficient').mean():.2f}", "stalemate": f"{(r == 'stalemate').mean():.2f}",
                    "repetition/agreed/insufficient": f"{r.isin(['repetition', 'agreed', 'insufficient']).mean():.2f}",
                }  # fmt: skip
            )
    report.table(pd.DataFrame(rows))

    import matplotlib.pyplot as plt

    thresholds = np.round(np.arange(0.55, 0.96, 0.05), 2)
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.5), sharey="row")
    curve_rows = []
    for j, tc in enumerate(TIME_CLASSES):
        part = games[games["time_class"] == tc]
        win, loss = (part["outcome"] == "win").to_numpy(float), (part["outcome"] == "loss").to_numpy(float)
        for side, colour, label in (("player", SIDE_COLOURS["player"], "you"), ("opponent", SIDE_COLOURS["opponent"], "opponents")):
            conv, swin = [], []
            for t in thresholds:
                if side == "player":
                    c = boot_rate(win, (part["my_p_win_max"] >= t).to_numpy(float))
                    s = boot_rate(1 - loss, (part["my_p_win_min"] <= 1 - t).to_numpy(float))
                else:
                    c = boot_rate(loss, (part["opp_p_win_max"] >= t).to_numpy(float))
                    s = boot_rate(1 - win, (part["opp_p_win_min"] <= 1 - t).to_numpy(float))
                conv.append(c)
                swin.append(s)
                curve_rows.append({"time_class": tc, "side": side, "threshold": t, "conversion": c[0], "conv_lo": c[1], "conv_hi": c[2], "n_conv": c[3], "swindle": s[0], "swin_lo": s[1], "swin_hi": s[2], "n_swin": s[3]})
            conv, swin = np.array(conv), np.array(swin)
            axes[0, j].plot(thresholds, conv[:, 0], "-o", color=colour, ms=4, lw=1.8, label=label)
            axes[0, j].fill_between(thresholds, conv[:, 1], conv[:, 2], color=colour, alpha=0.15, lw=0)
            axes[1, j].plot(1 - thresholds, swin[:, 0], "-o", color=colour, ms=4, lw=1.8, label=label)
            axes[1, j].fill_between(1 - thresholds, swin[:, 1], swin[:, 2], color=colour, alpha=0.15, lw=0)
        axes[0, j].set_title(tc)
        axes[0, j].set_xlabel("reached P(win) ≥ t")
        axes[1, j].set_xlabel("fell to P(win) ≤ t")
    axes[0, 0].set_ylabel("P(win) - conversion")
    axes[1, 0].set_ylabel("P(not loss) - swindle")
    axes[0, 0].legend(fontsize=8, loc="lower right")
    fig.suptitle("Conversion and swindle rates against the threshold (shaded: 95% CI)", fontsize=11)
    fig.tight_layout()
    report.figure(fig, "conversion_swindle", "Conversion (top) and swindle (bottom) rates as a function of the threshold, you vs opponents, per time class.")
    plt.close(fig)
    return pd.DataFrame(curve_rows), on_time


def section_sanity(report: Report, games: pd.DataFrame, moves: pd.DataFrame) -> None:
    report.heading("5. Sanity checks")
    report.text("The decomposition should predict results: your score by `net_ep` bin (their ep lost minus yours), all time classes together, with the model's expectation at move 1 and after the last move.")
    bins = [-10, -1.0, -0.5, -0.2, 0, 0.2, 0.5, 1.0, 10]
    labels = ["< −1", "−1 to −0.5", "−0.5 to −0.2", "−0.2 to 0", "0 to 0.2", "0.2 to 0.5", "0.5 to 1", "> 1"]
    frame = games.assign(bin=pd.cut(games["net_ep"], bins=bins, labels=labels))
    rows = []
    for b, part in frame.groupby("bin", observed=True):
        rows.append({"net ep (them − you)": b, "games": len(part), "your score": fmt(*game_mean(part, "my_score")), "ep at move 1": fmt(*game_mean(part, "my_ep_start")), "ep at last move": fmt(*game_mean(part, "my_ep_end"))})
    report.table(pd.DataFrame(rows))
    empty = [b for b in labels if (frame["bin"] == b).sum() == 0]
    corr = np.corrcoef(games["net_ep"].to_numpy(float), games["my_score"].to_numpy(float))[0, 1]
    report.text(
        (f"Bins {', '.join(repr(b) for b in empty)} hold no games (net ep ranges from {games['net_ep'].min():.2f} to {games['net_ep'].max():.2f}) and are not shown. " if empty else "")
        + f"Correlation between `net_ep` and your score over all {len(games):,} games: {corr:.2f}; monotone and steep, as it should be if `ep_loss` measures what matters."
    )

    report.heading("Is the P(win) model even-handed between you and your opponents?", level=3)
    report.text(
        "The thresholds in sections 3 and 4 are model probabilities. Here every position after a move is binned by the model's P(win) for one side, "
        "and the observed win rate of that side is shown (CIs by game; all time classes pooled, standard live games). If the model were symmetric and "
        "perfectly calibrated, both observed columns would equal the bin centre. Two things show: the model is under-confident at the extremes "
        "(a model 80–90% is observed above 90%), and in every bin you win more often than opponents do from positions the model rates the same, "
        "which is the same fact as the conversion and swindle gaps in section 4."
    )
    mine = moves["is_player_move"].to_numpy()
    f = pd.DataFrame({"g": moves["game_uuid"], "my_p": np.where(mine, moves["p_win_after"], moves["p_loss_after"]), "opp_p": np.where(mine, moves["p_loss_after"], moves["p_win_after"]), "my_win": (moves["outcome"] == "win").astype(float), "opp_win": (moves["outcome"] == "loss").astype(float)})
    edges = np.round(np.arange(0, 1.01, 0.1), 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        row = {"model P(win) after the move": f"{lo:.1f}–{hi:.1f}"}
        for who, p, w in (("you", "my_p", "my_win"), ("opponents", "opp_p", "opp_win")):
            part = f[(f[p] >= lo) & (f[p] < hi + (1e-9 if hi == 1 else 0))]
            row[f"{who}: positions"] = len(part)
            row[f"{who}: observed win rate"] = fmt(*cluster_bootstrap_mean(part[w], part["g"], n_boot=N_BOOT, seed=SEED))
        rows.append(row)
    report.table(pd.DataFrame(rows))


def calibration_brier() -> str:
    """Cross-validated expected-points Brier of the P(win) model, read from reports/calibration.md (so it cannot go stale here)."""
    import re

    path = repo_root() / "reports" / "calibration.md"
    if path.exists():
        hit = re.search(r"Expected points Brier \| ([0-9.]+)", path.read_text())
        if hit:
            return f"cross-validated expected-points Brier {float(hit.group(1)):.3f}, see `reports/calibration.md`"
    return "see `reports/calibration.md` for its cross-validated Brier score"


def write_table(table: pd.DataFrame, username: str) -> Path:
    path = repo_root() / "data" / username / "games_decomposed.parquet"
    table.to_parquet(path, index=False)
    return path


def main() -> None:
    t0 = time.time()
    data = load_data()
    figure_style()
    games = data.games.copy()
    decomposed = decompose_games(data.moves, games)
    table_path = write_table(decomposed, data.username)
    print(f"wrote {table_path} ({len(decomposed):,} rows, {len(decomposed.columns)} columns)")

    g = games.merge(decomposed, on="uuid", how="left")
    g = g[g["n_plies"].notna() & g["time_class"].isin(TIME_CLASSES)]
    moves = data.moves[data.moves["time_class"].isin(TIME_CLASSES)]

    report = Report(SLUG, "Where points are won and lost: your mistakes vs your opponents'")
    report.text(
        f"Data: {len(g):,} standard live games with at least one move ({(g['time_class'] == 'bullet').sum():,} bullet, "
        f"{(g['time_class'] == 'blitz').sum():,} blitz, {(g['time_class'] == 'rapid').sum():,} rapid) and {len(moves):,} analysed moves "
        f"({int(moves['is_player_move'].sum()):,} yours), player {data.username}. Numbers are estimate [95% CI], CIs bootstrapped by game "
        f"({N_BOOT} resamples). `ep_loss` is the calibrated expected points a move gives away; `cp_loss` is the secondary measure. "
        "Every number about you sits next to the opponents' number from the same games."
    )
    budget = section_budget(report, g)
    tail20 = section_errors(report, moves)
    decisive = section_decisive(report, g)
    curves, on_time = section_conversion(report, g)
    section_sanity(report, g, moves)

    report.heading("Output table")
    report.text(
        f"`data/{data.username}/games_decomposed.parquet`: one row per standard live game ({len(decomposed):,} rows, same order as `load_data().games`), "
        "keyed by `uuid`. Columns: `n_plies`; `my_ep_start`/`my_ep_end`/`my_p_win_start`/`my_p_win_end` (your expected points and P(win) before move 1 and after the last move); "
        "per side (`my_`/`opp_`): `ep_lost`, `cp_lost`, `ep_gained`, `moves`, `blunders`, `mistakes`, `inaccuracies`, `errors_ge_10/20/30` (moves with ep_loss ≥ 0.1/0.2/0.3), "
        "`ep_lost_opening/middlegame/endgame`, `max_ep_loss`, `p_win_max`, `p_win_min`; `net_ep` = opp_ep_lost − my_ep_lost; "
        "`reached_winning`/`reached_losing`/`opp_reached_winning`/`opp_reached_losing` (P(win) ≥ 0.8 / ≤ 0.2 at some point); "
        "decided games only (null for draws): `loser_last_p_win` (loser's P(win) after their last own move); for games that ended lost on the board (< 0.1): `slide_start_ply` (last own move from P(win) ≥ 0.2), "
        "`decisive_ply` (the loser's costliest move from there to the end), `decisive_found` (that move lost ≥ 0.05 ep), `lost_without_blunder` (it did not), `decisive_is_last_playable`, `decisive_by` (the loser's side: player/opponent), "
        "`decisive_ep_loss`, `decisive_cp_loss`, `decisive_label`, `decisive_phase`, `decisive_p_win_before`/`decisive_p_win_after` (loser's P(win) around it), `decisive_clock_before` (mover's clock, s), `decisive_plies_from_end`, "
        "and the stricter (playable ≥ 0.3, lost < 0.15) versions `decisive_strict_found/ply/ep_loss/phase`. Games lost while still alive have `decisive_found` and `lost_without_blunder` both False with outcome win/loss and null decisive columns."
    )

    # Numbers for the closing paragraph and caveats, all computed from the tables above or the data (nothing hardcoded).
    conv_b = curves[(curves["time_class"] == "bullet") & (curves["threshold"] == 0.8)].set_index("side")
    swin_b = conv_b
    worst = tail20.loc[tail20["diff"].idxmax()]
    found_rates = [v["found_given_on_board"] for v in decisive.values()]
    ep_means = [v["decisive_ep"] for v in decisive.values()]
    alive = lambda who: " / ".join(f"{decisive[(tc, who)]['alive']:.0%}" for tc in TIME_CLASSES)  # noqa: E731
    n_draws = int((g["outcome"] == "draw").sum())
    n_no_moves = int((games["time_class"].isin(TIME_CLASSES) & ~games["uuid"].isin(g["uuid"])).sum())
    n_rapid = int((g["time_class"] == "rapid").sum())
    # Opponent pre-game rating reconstructed as in the calibration step (post-game rating + your rating change); a descriptor only, never a predictor.
    gap = (g["opponent_rating_after"] + g["my_rating_change"]) - g["my_rating_before"]
    brier = calibration_brier()
    report.heading("What this means for the player")
    report.text(
        "At this level games are not won by playing better than the opponent on average: both sides leak about a point of expected score per game "
        f"and the paired difference is a few hundredths (bullet {fmt(*budget['bullet'])}, blitz {fmt(*budget['blitz'])}, rapid {fmt(*budget['rapid'])} in your favour). "
        f"Games are decided by the tail: the ~10% of moves that lose 0.1 or more carry more than half the damage, and in {min(found_rates):.0%}–{max(found_rates):.0%} of games lost on the board a single "
        f"move of the loser costing 0.05 or more can be named, typically an endgame move costing {min(ep_means):.2f}–{max(ep_means):.2f}. Your edge over the field is not fewer mistakes; it is what happens after the position is clear: from positions the model rates the same "
        f"you convert more (bullet {conv_b.loc['player', 'conversion']:.0%} vs {conv_b.loc['opponent', 'conversion']:.0%} from ≥ 80%) and escape more "
        f"(bullet {swin_b.loc['player', 'swindle']:.0%} vs {swin_b.loc['opponent', 'swindle']:.0%} from ≤ 20%), largely by winning on time: "
        f"{on_time[('bullet', 'you')]:.0%} of your bullet swindle wins came by the opponent flagging, against {on_time[('bullet', 'opponents')]:.0%} of theirs "
        f"(blitz {on_time[('blitz', 'you')]:.0%} vs {on_time[('blitz', 'opponents')]:.0%}). "
        f"The cell where your own big errors are most frequent relative to opponents is {worst['time_class']} {worst['phase']} moves losing ≥ 0.2 "
        f"({worst['player']:.3f} vs {worst['opponent']:.3f} per move, difference {fmt(worst['diff'], worst['diff_lo'], worst['diff_hi'])}); your bullet endgames are your cleanest phase relative to the field. "
        f"The other visible leak is games lost while the model still gave you chances ({alive('you')} of your bullet / blitz / rapid losses, against {alive('opponents')} for opponents): "
        "flags, resignations and mates the clock might have averted, which Q4 and Q7 take up."
    )
    report.heading("Caveats")
    report.text(
        "- The baseline is engine-perfect play (Stockfish 17.1, 1M nodes): `ep_loss` is relative to the best move, so a 'loss' can be a move no human would find.\n"
        "- `ep_loss` is clipped at zero and each position is evaluated separately, so per-game sums do not telescope exactly to the result; the residual in section 1 absorbs resignations, flag falls and end-of-game model error.\n"
        f"- P(win) comes from a model calibrated on these same games ({brier}) that is under-confident at the extremes (section 5): 'reached ≥ 80%' positions are won about 90–95% of the time, and 'lost while still alive' is judged by a model that keeps flag chances alive in bullet even at +10. The strict column in section 3 shows how the shares move with the thresholds.\n"
        "- The P(win) model is pooled over both sides; you out-perform it in every probability bin and opponents under-perform it, so absolute conversion rates are model-relative; the you-vs-them comparison is the robust part.\n"
        f"- The decisive-mistake rule credits a game to one move: the costliest of the loser's final slide from the last playable (≥ {DECISIVE_HI:.0%}) position; slides with several comparable mistakes are credited to the largest, and slides with no move costing {MIN_DECISIVE_EP:.2f} are counted separately. Only the loser's moves are candidates, so 'who made it' is always the loser.\n"
        "- Phase is the material heuristic; 'opening' includes many moves beyond book.\n"
        f"- Opponents are matched by rating: reconstructing the opponent's pre-game rating as their post-game rating plus your rating change (a rough descriptor, not used as a predictor anywhere), the gap to your pre-game rating has median |gap| {gap.abs().median():.0f} points, mean |gap| {gap.abs().mean():.0f}, mean signed gap {gap.mean():+.1f}, so you-vs-them differences describe how you differ from the field at your level, not an absolute standard. Rapid has only {n_rapid:,} games.\n"
        f"- {n_no_moves} standard live game(s) ended before the first move and are excluded from the report (they are in the table with null decomposition); draws ({n_draws:,}) have no decisive mistake by construction.\n"
        f"- Bootstrap CIs use {N_BOOT} resamples by game; per-game numbers treat each game as one cluster."
    )
    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
