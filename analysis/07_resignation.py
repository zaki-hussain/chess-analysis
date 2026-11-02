"""Q7: resignation - how often you resign, from what, and what it costs.

How often you resign your losses compared with your opponents, from which
positions (the calibrated model's win chance and the engine eval at the
moment of resigning), what positions like those actually scored when they
were played out instead, how the opponent's clock changes that, how many
expected points your resignations gave away (and your opponents' handed
you), and where resigning is safe.

The counterfactual is empirical: every position after a move in games the
side under study did not resign or abandon (its "played-out" games) is
binned by the model's expected score from that side's view (or by the engine
eval), and scored by how those games ended for that side (win 1, draw 1/2,
loss 0). Each resignation is valued at the played-out score of its band
within its own time control; the model's own expected score and an
all-positions lower bound are kept as alternative valuations. Everything is
split by exact time control (`data.tc_groups`); cells that are thin per
control (eval band x clock x side) are also pooled by time class. CIs resample
whole games. Per-game decomposition columns come from games_decomposed.parquet.

Writes CSVs to presentation/data/07_resignation/ and SVGs to
presentation/figures/07_resignation/ (no prose; the reading is in
presentation/README.md).

Run: .venv/bin/python analysis/07_resignation.py   (about 40 seconds)
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from chess_analysis import charts
from chess_analysis.toolkit import OUTCOME_SCORE, Results, cluster_bootstrap_mean, excess_to_elo, load_data, repo_root

SLUG = "07_resignation"
SEED = 7
N_BOOT = 500  # resamples for every CI (game-clustered); the third decimal is stable at this count
SIDES = ("player", "opponent")
RESULT_COL = {"player": "my_result", "opponent": "opponent_result"}
LOSS_OUTCOME = {"player": "loss", "opponent": "win"}  # the game outcome (player's view) when that side lost
LOSS_ENDINGS = ["resigned", "timeout", "checkmated", "abandoned"]  # named ways a loss ends; anything else is "other"
ALL = "all games"  # pooled row over every reported control (the thin 0.5+0 and 30+0 are never included)

# Expected score (P(win) + P(draw)/2, the calibrated model, from the side's view) bands: narrow at the bottom,
# where almost every resignation sits, wide above 30% where hardly anyone resigns.
EV_EDGES = [0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.0001]
EV_LABELS = ["0-2%", "2-5%", "5-10%", "10-15%", "15-20%", "20-30%", "30-50%", "50-100%"]
# Engine eval bands from the side's view, in centipawns (labels in pawns): mate or 20+ pawns down, roughly a
# queen+ / a rook / a piece / a pawn or two down, and close to level. Mates count as beyond -20 pawns.
CP_EDGES = [-np.inf, -2000, -1000, -500, -300, -100, np.inf]
CP_LABELS = ["mate or -20", "-20 to -10", "-10 to -5", "-5 to -3", "-3 to -1", "-1 or better"]
# Clock bands of the other side (and the resigner) at the position: under 10 s is where flagging becomes a
# real threat in bullet, 10-30 s is a few seconds a move for a bullet ending, 30+ s is comfortable. Applied to
# both plain seconds left and the buffer beyond one increment (in 1+1 an opponent on 5 s still gets a second
# back every move, in 1+0 they do not).
CLOCK_EDGES = [-np.inf, 10.0, 30.0, np.inf]
CLOCK_LABELS = ["<10 s", "10-30 s", "30+ s"]
CLOCK_MEASURES = ("seconds left", "buffer beyond increment")
MAIN_MEASURE = "buffer beyond increment"  # the measure the charts use: comparable across increments

# Resignation classes by the model's P(win) at the resignation, from the resigner's view.
CLEARLY_LOST_P_WIN = 0.05  # under 1 in 20: dead lost
PLAYABLE_P_WIN = 0.20  # 1 in 5 or better: "still playable"
SECOND_P_WIN = 0.10  # the looser line the old report also showed
CLASS_LABELS = ["clearly lost", "nearly lost", "still playable"]
# "Resigned on the spot after a blunder": the resigner's last own move alone cost a fifth of a point.
BLUNDER_THEN_RESIGN_EP = 0.20
# "Had to go" proxy: still playable, at least a quarter of the base time left (no time trouble) and a quick
# last own move (at or below the resigner's median move time in that game).
HAD_TO_GO_CLOCK_SHARE = 0.25
# "Had been winning": the resigner reached this P(win) at some point (games_decomposed.parquet's threshold).
WINNING_P_WIN = 0.80
# Rule of thumb: resigning is "safe" in a band when positions like it, played out, scored at most this much
# (upper CI end), and so did every worse band. 0.02 / 0.05 = 2 / 5 points per 100 such resignations.
SAFE_COSTS = (0.02, 0.05)

# Thin flags. A share from fewer than 30 resignations (or losses) has a CI of about +-0.17; a played-out
# score from fewer than 30 games rests on a handful of comebacks. Thin cells are kept in the CSVs, flagged,
# and not drawn.
MIN_RESIGNATIONS = 30
MIN_LOSSES = 30
MIN_CELL_GAMES = 30
# The 7.3 chart draws the bands below 50% only (where resignations happen; 50-100% stays in the CSV) on a
# 0-0.8 scale: tall enough for every drawn cell's upper CI end, low enough to separate the bottom bands.
CHART_EV_LABELS = EV_LABELS[:-1]
PLAYED_OUT_YMAX = 0.8
# The heatmap's colour scale tops out at a coin flip: anything near it is clearly not a position to resign.
MAP_VMAX = 0.5


# --- groups ------------------------------------------------------------------------------------------


def class_views(data) -> list[tuple[str, list[str]]]:
    """Per time class (data.tc_class): "all <class>" over its controls when it has two or more, else the single control."""
    classes: dict[str, list[str]] = {}
    for g in data.tc_groups:
        classes.setdefault(data.tc_class[g], []).append(g)
    return [(f"all {c}" if len(m) >= 2 else m[0], m) for c, m in classes.items()]


def views(data, pooled: bool = False, all_games: bool = False) -> list[tuple[str, list[str], bool]]:
    """(label, member controls, is_pooled): each reported control, then optionally class pools and all games."""
    groups = list(data.tc_groups)
    out = [(g, [g], False) for g in groups]
    if pooled:
        out += [(label, members, True) for label, members in class_views(data) if len(members) >= 2]
    if all_games:
        out.append((ALL, groups, True))
    return out


def pool_note(data) -> str:
    """The class pools and their member controls, for footnotes."""
    return "; ".join(f"{label}: {', '.join(m.split(' ', 1)[1] for m in members)}" for label, members in class_views(data) if len(members) >= 2)


# --- small bootstrap helpers (rows are games) ----------------------------------------------------------


def boot_mean(values, seed: int = SEED) -> tuple[float, float, float]:
    """Mean of one value per game with a bootstrap CI."""
    v = np.asarray(values, dtype="float64")
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return np.nan, np.nan, np.nan
    if len(v) < 2:
        return float(v.mean()), np.nan, np.nan
    rng = np.random.default_rng(seed)
    boot = v[rng.integers(0, len(v), size=(N_BOOT, len(v)))].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(v.mean()), float(lo), float(hi)


def boot_mean_diff(a, b, seed: int = SEED) -> tuple[float, float, float]:
    """mean(a) - mean(b) for two disjoint sets of games, each resampled."""
    a, b = (np.asarray(x, dtype="float64") for x in (a, b))
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 2 or len(b) < 2:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    ba = a[rng.integers(0, len(a), size=(N_BOOT, len(a)))].mean(axis=1)
    bb = b[rng.integers(0, len(b), size=(N_BOOT, len(b)))].mean(axis=1)
    lo, hi = np.quantile(ba - bb, [0.025, 0.975])
    return float(a.mean() - b.mean()), float(lo), float(hi)


def boot_rate_diff(y1, c1, y2, c2, seed: int = SEED) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """P(y1|c1), P(y2|c2) and their difference, all on the same resampled games."""
    y1, c1, y2, c2 = (np.asarray(a, dtype="float64") for a in (y1, c1, y2, c2))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(y1), size=(N_BOOT, len(y1)))
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = (y1[idx] * c1[idx]).sum(axis=1) / c1[idx].sum(axis=1)
        r2 = (y2[idx] * c2[idx]).sum(axis=1) / c2[idx].sum(axis=1)
    e1 = (y1 * c1).sum() / c1.sum() if c1.sum() else np.nan
    e2 = (y2 * c2).sum() / c2.sum() if c2.sum() else np.nan

    def q(x):
        x = x[~np.isnan(x)]
        return tuple(np.quantile(x, [0.025, 0.975])) if len(x) else (np.nan, np.nan)

    return (float(e1), *q(r1)), (float(e2), *q(r2)), (float(e1 - e2), *q(r1 - r2))


# --- data preparation -------------------------------------------------------------------------------------


def positions(moves: pd.DataFrame) -> pd.DataFrame:
    """Every position after a move, once from each side's view (long: a `side` column)."""
    m = moves.sort_values(["game_uuid", "ply"]).reset_index(drop=True)
    mine = m["is_player_move"].to_numpy()
    last_ply = m.groupby("game_uuid")["ply"].transform("max").to_numpy()
    ended_on_board = (m["my_result"].isin(["checkmated", "stalemate"]) | m["opponent_result"].isin(["checkmated", "stalemate"])).to_numpy()
    increment = m["increment_seconds"].astype(float).to_numpy()
    mover_clock = m["clock_seconds"].astype(float).to_numpy()
    waiting_clock = m["opponent_clock_seconds"].astype(float).to_numpy()
    score_player = m["outcome"].map(OUTCOME_SCORE).to_numpy(dtype=float)
    frames = []
    for side in SIDES:
        moved = mine if side == "player" else ~mine  # did this side make the move that led here?
        own_clock = np.where(moved, mover_clock, waiting_clock)
        other_clock = np.where(moved, waiting_clock, mover_clock)
        p_win = np.where(moved, m["p_win_after"], m["p_loss_after"])
        frames.append(
            pd.DataFrame(
                {
                    "game_uuid": m["game_uuid"], "ply": m["ply"], "tc_group": m["tc_group"].astype(str), "side": side,
                    "moved_last": moved, "is_last": m["ply"].to_numpy() == last_ply,
                    "board_terminal": (m["ply"].to_numpy() == last_ply) & ended_on_board,
                    "played_out": ~m[RESULT_COL[side]].isin(["resigned", "abandoned"]).to_numpy(),
                    "p_win": p_win, "ev": p_win + 0.5 * m["p_draw_after"].to_numpy(),
                    "cp": np.where(moved, m["cp_after"], -m["cp_after"]),
                    "score": score_player if side == "player" else 1 - score_player,
                    "own_seconds left": own_clock, "own_buffer beyond increment": own_clock - increment,
                    "other_seconds left": other_clock, "other_buffer beyond increment": other_clock - increment,
                }
            )  # fmt: skip
        )
    pos = pd.concat(frames, ignore_index=True)
    pos["ev_bin"] = pd.cut(pos["ev"], EV_EDGES, labels=EV_LABELS, right=False)
    pos["cp_bin"] = pd.cut(pos["cp"], CP_EDGES, labels=CP_LABELS, right=False)
    for measure in CLOCK_MEASURES:
        for who in ("own", "other"):
            pos[f"{who}_bin_{measure}"] = pd.cut(pos[f"{who}_{measure}"], CLOCK_EDGES, labels=CLOCK_LABELS, right=False)
    return pos


def resignations(pos: pd.DataFrame, moves: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per resignation (games with moves), from the resigner's view at the final position."""
    last = pos[pos["is_last"]]
    g = games.set_index("uuid")
    rows = []
    for side in SIDES:
        resigned = g.index[g[RESULT_COL[side]] == "resigned"]
        r = last[(last["side"] == side) & last["game_uuid"].isin(resigned)].set_index("game_uuid")
        own = moves[moves["is_player_move"] == (side == "player")].sort_values(["game_uuid", "ply"])
        own_last = own.groupby("game_uuid").tail(1).set_index("game_uuid")
        own_median = own.groupby("game_uuid")["time_spent_seconds"].median()
        info = g.reindex(r.index)
        r = r.assign(
            last_own_ep_loss=own_last["ep_loss"].reindex(r.index),
            last_own_quick=(own_last["time_spent_seconds"].reindex(r.index) <= own_median.reindex(r.index)),
            clock_share=r["own_seconds left"] / info["base_seconds"].astype(float),
            last_in_session=(info["game_in_session"] == info["session_games"]),
            had_been_winning=(info["my_p_win_max" if side == "player" else "opp_p_win_max"] >= WINNING_P_WIN),
        )
        rows.append(r.reset_index())
    res = pd.concat(rows, ignore_index=True)
    res["class"] = pd.cut(res["p_win"], [0.0, CLEARLY_LOST_P_WIN, PLAYABLE_P_WIN, 1.0001], labels=CLASS_LABELS, right=False)
    res["blunder_then_resign"] = res["last_own_ep_loss"] >= BLUNDER_THEN_RESIGN_EP
    res["had_to_go"] = (res["class"] == "still playable") & (res["clock_share"] >= HAD_TO_GO_CLOCK_SHARE) & res["last_own_quick"].fillna(False).astype(bool)
    return res


# --- played-out scores --------------------------------------------------------------------------------------


def score_cells(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Played-out score per cell of `keys` with game-clustered CIs, positions, games and the model's mean EV."""
    rows = []
    for key, part in frame.groupby(keys, observed=True, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        est, lo, hi = cluster_bootstrap_mean(part["score"], part["game_uuid"], n_boot=N_BOOT, seed=SEED)
        rows.append({**dict(zip(keys, key)), "score": est, "score_lo": lo, "score_hi": hi, "model_ev": float(part["ev"].mean()), "positions": len(part), "games": int(part["game_uuid"].nunique())})
    return pd.DataFrame(rows, columns=[*keys, "score", "score_lo", "score_hi", "model_ev", "positions", "games"])


def played_out_table(pos: pd.DataFrame, res: pd.DataFrame, band: str, extra: list[str], view_list) -> pd.DataFrame:
    """Score by `band` (and `extra` keys) per view and side, with the resignations falling in each cell."""
    labels = EV_LABELS if band == "ev_bin" else CP_LABELS
    out = []
    comp = pos[pos["played_out"] & ~pos["board_terminal"]]
    for label, members, pooled in view_list:
        c = comp[comp["tc_group"].isin(members)]
        r = res[res["tc_group"].isin(members)]
        cells = score_cells(c, ["side", *extra, band])
        counts = r.groupby(["side", *extra, band], observed=True).size().rename("resignations").reset_index()
        cells = cells.merge(counts, on=["side", *extra, band], how="left")
        cells["resignations"] = cells["resignations"].fillna(0).astype(int)
        cells.insert(0, "pooled", pooled)
        cells.insert(0, "tc_group", label)
        out.append(cells)
    table = pd.concat(out, ignore_index=True)
    table[band] = pd.Categorical(table[band].astype(str), categories=labels, ordered=True)
    if "other_clock" in extra:
        table["other_clock"] = pd.Categorical(table["other_clock"].astype(str), categories=CLOCK_LABELS, ordered=True)
    table["score_minus_model"] = table["score"] - table["model_ev"]
    table["thin"] = table["games"] < MIN_CELL_GAMES
    order = {lab: i for i, (lab, _, _) in enumerate(view_list)}
    table = table.assign(_v=table["tc_group"].map(order), _s=table["side"].map({s: i for i, s in enumerate(SIDES)}))
    table = table.sort_values(["_v", "_s", *extra, band], kind="stable").drop(columns=["_v", "_s"])
    cols = ["tc_group", "pooled", "side", *extra, band, "score", "score_lo", "score_hi", "model_ev", "score_minus_model", "positions", "games", "resignations", "thin"]
    return table[cols].reset_index(drop=True)


def clock_table(pos: pd.DataFrame, res: pd.DataFrame, band: str, view_list) -> pd.DataFrame:
    """Played-out score by band x the other side's clock band, for both clock measures."""
    parts = []
    for measure in CLOCK_MEASURES:
        p = pos.assign(other_clock=pos[f"other_bin_{measure}"])
        r = res.assign(other_clock=res[f"other_bin_{measure}"])
        t = played_out_table(p, r, band, ["other_clock"], view_list)
        t.insert(2, "clock_measure", measure)
        parts.append(t)
    return pd.concat(parts, ignore_index=True)


# --- valuation of resignations -------------------------------------------------------------------------------


def valuation(pos: pd.DataFrame, res: pd.DataFrame, group_games: np.ndarray, seed: int) -> dict:
    """Played-out value of every resignation in one control, with bootstrap replicates.

    One game-level bootstrap drives everything: each replicate resamples the
    control's games, recomputes the band scores from the played-out games
    drawn, and values the resignations drawn (both sides on the same draw, so
    the net has a proper CI). A band with no played-out positions in a draw
    falls back to the resignation's own model expected score.
    """
    index = {g: i for i, g in enumerate(group_games)}
    n = len(group_games)
    rng = np.random.default_rng(seed)
    weights = np.stack([np.bincount(rng.integers(0, n, n), minlength=n) for _ in range(N_BOOT)]).astype(float)
    nb = len(EV_LABELS)
    out = {}
    for side in SIDES:
        side_pos = pos[(pos["side"] == side) & ~pos["board_terminal"] & pos["ev_bin"].notna()]
        r = res[res["side"] == side]
        r = r[r["ev_bin"].notna()]
        rb = r["ev_bin"].cat.codes.to_numpy()
        rg = r["game_uuid"].map(index).to_numpy()
        rev = r["ev"].to_numpy(dtype=float)
        for name, comp in (("played_out", side_pos[side_pos["played_out"]]), ("all_positions", side_pos)):
            gi = comp["game_uuid"].map(index).to_numpy()
            bi = comp["ev_bin"].cat.codes.to_numpy()
            sums, counts = np.zeros((n, nb)), np.zeros((n, nb))
            np.add.at(sums, (gi, bi), comp["score"].to_numpy(dtype=float))
            np.add.at(counts, (gi, bi), 1.0)
            with np.errstate(invalid="ignore", divide="ignore"):
                rates = sums.sum(0) / counts.sum(0)
            values = np.where(np.isnan(rates[rb]), rev, rates[rb])
            entry = {"values": pd.Series(values, index=r.index), "total": float(values.sum()), "n_res": len(r)}
            if name == "played_out":
                with np.errstate(invalid="ignore", divide="ignore"):
                    boot_rates = (weights @ sums) / (weights @ counts)
                v = boot_rates[:, rb]
                v = np.where(np.isnan(v), rev[None, :], v)
                w = weights[:, rg]
                entry["boot_total"] = (v * w).sum(axis=1)
                entry["boot_nres"] = w.sum(axis=1)
            out[(side, name)] = entry
    out["n_games"] = n
    return out


def q(x) -> tuple[float, float]:
    lo, hi = np.nanquantile(np.asarray(x, dtype=float), [0.025, 0.975])
    return float(lo), float(hi)


# --- 7.1 how often ---------------------------------------------------------------------------------------------


def q1_share(results: Results, games: pd.DataFrame, data) -> None:
    rows, endings = [], []
    for label, members, pooled in views(data, all_games=True):
        g = games[games["tc_group"].isin(members)]
        y1, c1 = (g["my_result"] == "resigned").to_numpy(), (g["outcome"] == "loss").to_numpy()
        y2, c2 = (g["opponent_result"] == "resigned").to_numpy(), (g["outcome"] == "win").to_numpy()
        (pe, plo, phi), (oe, olo, ohi), (de, dlo, dhi) = boot_rate_diff(y1, c1, y2, c2)
        rows.append({
            "tc_group": label, "pooled": pooled, "player": pe, "player_lo": plo, "player_hi": phi, "opponent": oe, "opponent_lo": olo, "opponent_hi": ohi,
            "diff": de, "diff_lo": dlo, "diff_hi": dhi,
            "player_resigned_per_100_games": 100 * y1.mean(), "opponent_resigned_per_100_games": 100 * y2.mean(),
            "games": len(g), "player_losses": int(c1.sum()), "opponent_losses": int(c2.sum()), "player_resigned": int(y1.sum()), "opponent_resigned": int(y2.sum()),
            "thin": min(c1.sum(), c2.sum()) < MIN_LOSSES,
        })  # fmt: skip
        for side in SIDES:
            lost = g[g["outcome"] == LOSS_OUTCOME[side]]
            how = lost[RESULT_COL[side]].where(lost[RESULT_COL[side]].isin(LOSS_ENDINGS), "other").value_counts()
            for ending in [*LOSS_ENDINGS, "other"]:
                endings.append({"tc_group": label, "pooled": pooled, "side": side, "ending": ending, "share": how.get(ending, 0) / len(lost) if len(lost) else np.nan, "count": int(how.get(ending, 0)), "losses": len(lost), "thin": len(lost) < MIN_LOSSES})
    table = pd.DataFrame(rows)
    results.table("1_resign_share", table, "Share of each side's losses that ended by resignation, you (player) vs opponents in the same games; diff = you minus opponents (CIs: games resampled). Also resignations per 100 games.")
    results.table("1b_loss_endings", pd.DataFrame(endings), "How each side's losses ended: share resigned, lost on time, checkmated, abandoned, other (shares sum to 1).")

    order = [*data.tc_groups, ALL]
    fig, ax = charts.single("What share of your losses do you resign?", size=(8.6, 3.6))
    shown = table.copy()
    for c in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi"]:
        shown.loc[shown["thin"], c] = np.nan
    charts.side_bars(ax, shown, "tc_group", order=order)
    for i, (_, row) in enumerate(table.set_index("tc_group").reindex(order).iterrows()):
        if row["thin"]:
            ax.text(i, 0.02, "too few\nlosses", ha="center", va="bottom", fontsize=7, color=charts.INK_MUTED)
    ax.set_xticks(range(len(order)), [o.replace(" ", "\n", 1) for o in order], fontsize=7.5)
    ax.set_ylabel("share of losses resigned")
    ax.set_ylim(0, 1)
    ax.axvline(len(order) - 1.5, color=charts.GRID, linewidth=1)
    charts.legend(ax, loc="upper left")
    charts.footnote(fig, "Bars: share of each side's losses that ended by resignation (95% CI, games resampled). The rest were lost on time, checkmated or abandoned.")
    results.figure(fig, "1_resign_share")


# --- 7.2 from what positions ------------------------------------------------------------------------------------


def q2_position(results: Results, res: pd.DataFrame, data, valued: pd.Series, games_per_view: dict) -> None:
    wide, detail = [], []
    for label, members, pooled in views(data, all_games=True):
        r_all = res[res["tc_group"].isin(members)]
        rp, ro = (r_all[r_all["side"] == s] for s in SIDES)
        pe = boot_mean((rp["p_win"] >= PLAYABLE_P_WIN).astype(float))
        oe = boot_mean((ro["p_win"] >= PLAYABLE_P_WIN).astype(float))
        de = boot_mean_diff((rp["p_win"] >= PLAYABLE_P_WIN).astype(float), (ro["p_win"] >= PLAYABLE_P_WIN).astype(float))
        wide.append({"tc_group": label, "pooled": pooled, "p_win_threshold": PLAYABLE_P_WIN, "player": pe[0], "player_lo": pe[1], "player_hi": pe[2], "opponent": oe[0], "opponent_lo": oe[1], "opponent_hi": oe[2], "diff": de[0], "diff_lo": de[1], "diff_hi": de[2], "player_resignations": len(rp), "opponent_resignations": len(ro), "thin": min(len(rp), len(ro)) < MIN_RESIGNATIONS})
        for side, r in zip(SIDES, (rp, ro)):
            row = {"tc_group": label, "pooled": pooled, "side": side}
            for name, values in [
                ("p_win_mean", r["p_win"]), ("share_p_win_20", (r["p_win"] >= PLAYABLE_P_WIN).astype(float)), ("share_p_win_10", (r["p_win"] >= SECOND_P_WIN).astype(float)),
                ("share_blunder_then_resign", r["blunder_then_resign"].astype(float)), ("clock_share_of_base", r["clock_share"]), ("share_had_been_winning", r["had_been_winning"].astype(float)),
            ]:  # fmt: skip
                e, lo, hi = boot_mean(values)
                row.update({name: e, f"{name}_lo": lo, f"{name}_hi": hi})
            row.update({
                "ev_median": float(r["ev"].median()), "eval_median_pawns": float(r["cp"].median() / 100), "share_level_or_better": float((r["cp"] >= 0).mean()) if len(r) else np.nan,
                "share_resigner_moved_last": float(r["moved_last"].mean()) if len(r) else np.nan,
                "resignations": len(r), "thin": len(r) < MIN_RESIGNATIONS,
            })  # fmt: skip
            detail.append(row)
    table = pd.DataFrame(wide)
    results.table("2_resign_position", table, "Share of resignations made while the model still gave the resigner P(win) >= 0.20 ('still playable'), you vs opponents; diff = you minus opponents (resignations resampled).")
    results.table("2b_resign_position_detail", pd.DataFrame(detail), "The resignation position per side: mean model P(win), median expected score, median engine eval (pawns, resigner's view), shares with P(win) >= 0.20 / 0.10, level or better on the board, last own move a blunder (>= 0.20 expected points), resigner's clock as a share of the base time, had been winning (P(win) >= 0.80 earlier), resigner moved last.")

    # Classes and 'had to go', valued at the played-out scores.
    rows, htg_rows = [], []
    res_v = res.assign(value=valued)
    for label, members, pooled in views(data, all_games=True):
        r_all = res_v[res_v["tc_group"].isin(members)]
        n_games = sum(games_per_view[m] for m in members)
        for side in SIDES:
            r = r_all[r_all["side"] == side]
            for cls in CLASS_LABELS:
                part = r[r["class"] == cls]
                row = {"tc_group": label, "pooled": pooled, "side": side, "class": cls, "share_of_resignations": len(part) / len(r) if len(r) else np.nan,
                       "played_out_cost": float(part["value"].sum()), "cost_per_100_games": 100 * float(part["value"].sum()) / n_games,
                       "share_of_cost": float(part["value"].sum() / r["value"].sum()) if r["value"].sum() > 0 else np.nan}  # fmt: skip
                for name, values in [("clock_share_of_base", part["clock_share"]), ("share_quick_last_move", part["last_own_quick"].astype(float)), ("share_blunder_then_resign", part["blunder_then_resign"].astype(float))]:
                    e, lo, hi = boot_mean(values)
                    row.update({name: e, f"{name}_lo": lo, f"{name}_hi": hi})
                e, lo, hi = boot_mean(part["last_in_session"].astype(float)) if side == "player" else (np.nan, np.nan, np.nan)
                row.update({"share_last_game_of_session": e, "share_last_game_of_session_lo": lo, "share_last_game_of_session_hi": hi})
                row.update({"resignations": len(part), "games": n_games, "thin": len(part) < MIN_RESIGNATIONS})
                rows.append(row)
        r = r_all[r_all["side"] == "player"]
        htg = r[r["had_to_go"]]
        lost = r[r["class"] != "still playable"]
        a, b = boot_mean(htg["last_in_session"].astype(float)), boot_mean(lost["last_in_session"].astype(float))
        d = boot_mean_diff(htg["last_in_session"].astype(float), lost["last_in_session"].astype(float))
        htg_rows.append({
            "tc_group": label, "pooled": pooled, "p_win_threshold": PLAYABLE_P_WIN, "clock_share_threshold": HAD_TO_GO_CLOCK_SHARE, "had_to_go": len(htg), "share_of_resignations": len(htg) / len(r) if len(r) else np.nan,
            "played_out_cost": float(htg["value"].sum()), "share_of_cost": float(htg["value"].sum() / r["value"].sum()) if r["value"].sum() > 0 else np.nan,
            "last_game_share": a[0], "last_game_share_lo": a[1], "last_game_share_hi": a[2],
            "lost_last_game_share": b[0], "lost_last_game_share_lo": b[1], "lost_last_game_share_hi": b[2],
            "diff": d[0], "diff_lo": d[1], "diff_hi": d[2], "resignations": len(r), "lost_resignations": len(lost), "thin": len(htg) < MIN_RESIGNATIONS,
        })  # fmt: skip
    results.table("2c_resign_class", pd.DataFrame(rows), "Resignations by the model's P(win) at the time (clearly lost < 0.05, nearly lost 0.05-0.20, still playable >= 0.20): share, played-out cost (points; per 100 games of the control), resigner's clock share of base, quick last own move, blunder then resign, last game of your session (you only).")
    results.table("2d_had_to_go", pd.DataFrame(htg_rows), "Your 'had to go' resignations (still playable, >= 25% of the base time left, quick last own move): count, cost, and how often they were the last game of the session vs your clearly/nearly lost resignations (diff, CI from resampling both sets).")

    order = [*data.tc_groups, ALL]
    fig, ax = charts.single("How often do you resign a position that was still playable?", size=(8.6, 3.6))
    shown = table.copy()
    for c in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi"]:
        shown.loc[shown["thin"], c] = np.nan
    charts.side_bars(ax, shown, "tc_group", order=order)
    for i, (_, row) in enumerate(table.set_index("tc_group").reindex(order).iterrows()):
        if row["thin"]:
            ax.text(i, 0.01, "too few\nresignations", ha="center", va="bottom", fontsize=6.5, color=charts.INK_MUTED)
    ax.set_xticks(range(len(order)), [o.replace(" ", "\n", 1) for o in order], fontsize=7.5)
    ax.set_ylabel("share of resignations\nwith P(win) of 20% or more")
    ax.set_ylim(0, 0.6)
    ax.axvline(len(order) - 1.5, color=charts.GRID, linewidth=1)
    charts.legend(ax, loc="upper left")
    charts.footnote(fig, "P(win) is the calibrated model's chance that the resigner would have won from the final position (95% CI, resignations resampled).")
    results.figure(fig, "2_resign_position")


# --- 7.3 played out -------------------------------------------------------------------------------------------


def wide_sides(table: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Player/opponent columns side by side (thin cells blanked) for drawing."""
    t = table.copy()
    for c in ["score", "score_lo", "score_hi"]:
        t.loc[t["thin"], c] = np.nan
    w = t.pivot_table(index=keys, columns="side", values=["score", "score_lo", "score_hi"], observed=False, dropna=False)
    out = pd.DataFrame(index=w.index)
    for side in SIDES:
        out[side] = w[("score", side)] if ("score", side) in w else np.nan
        out[f"{side}_lo"] = w[("score_lo", side)] if ("score_lo", side) in w else np.nan
        out[f"{side}_hi"] = w[("score_hi", side)] if ("score_hi", side) in w else np.nan
    return out.reset_index()


def label_first_column(axes: dict, text: str) -> None:
    first = {}
    for ax in axes.values():
        first.setdefault(round(ax.get_position().y0, 3), ax)
    for ax in first.values():
        ax.set_ylabel(text)


def q3_played_out(results: Results, pos: pd.DataFrame, res: pd.DataFrame, data) -> None:
    view_list = views(data)
    ev_table = played_out_table(pos, res, "ev_bin", [], view_list)
    results.table("3_played_out_by_ev", ev_table, "Played-out score (how games the side did not resign or abandon ended for it) of positions by the model's expected score band from that side's view; model_ev = the model's mean in the cell; resignations = that side's resignations in the band. CIs clustered by game.")
    cp_table = played_out_table(pos, res, "cp_bin", [], view_list)
    results.table("3b_played_out_by_eval", cp_table, "The same played-out score by engine eval band (pawns, the side's view; mates count as -20 or worse).")

    fig, axes = charts.group_grid(data.tc_groups, panel=(3.0, 2.6), sharey=True, title="What did positions like the ones you resign score when played out?")
    x = np.arange(len(CHART_EV_LABELS))
    for group, ax in axes.items():
        t = ev_table[(ev_table["tc_group"] == group) & ev_table["ev_bin"].astype(str).isin(CHART_EV_LABELS)].copy()
        t["ev_bin"] = pd.Categorical(t["ev_bin"].astype(str), categories=CHART_EV_LABELS, ordered=True)
        model = t[t["side"] == "player"].set_index("ev_bin")["model_ev"].reindex(CHART_EV_LABELS)
        ax.plot(x, model.to_numpy(dtype=float), color=charts.INK_MUTED, linestyle="--", linewidth=1, label="model's expected score")
        w = wide_sides(t, ["ev_bin"])
        if w[["player", "opponent"]].notna().sum().sum() == 0:
            charts.thin_note(ax)
            continue
        charts.side_lines(ax, w, "ev_bin", order=CHART_EV_LABELS)
        ax.set_xticks(x, CHART_EV_LABELS, rotation=45, ha="right", rotation_mode="anchor", fontsize=7)
        ax.set_ylim(0, PLAYED_OUT_YMAX)
    for ax in axes.values():
        ax.set_xlabel("model's expected score", fontsize=7.5)
    label_first_column(axes, "played-out score")
    charts.legend(fig, loc="upper right", bbox_to_anchor=(0.99, 1.0))
    fig.tight_layout()
    charts.footnote(fig, "Positions from games the side did not resign or abandon, by the model's expected score from that side's view; y = how those games ended for that side (win 1, draw 1/2). Bands under 50% only; bands with under 30 games are not drawn.")
    results.figure(fig, "3_played_out_by_ev")


# --- 7.4 the opponent's clock ---------------------------------------------------------------------------------


def q4_clock(results: Results, pos: pd.DataFrame, res: pd.DataFrame, data) -> pd.DataFrame:
    view_list = views(data, pooled=True)
    cp_clock = clock_table(pos, res, "cp_bin", view_list)
    results.table("4_clock_by_eval", cp_clock, "Played-out score by engine eval band x the other side's clock (seconds left, and buffer beyond one increment), per control and pooled by time class ('all bullet', 'all blitz'), both sides; resignations = that side's resignations in the cell.")
    ev_clock = clock_table(pos, res, "ev_bin", view_list)
    results.table("4b_clock_by_ev", ev_clock, "The same split by the model's expected score band: the model already uses both clocks, so differences between clock bands here are what it misses.")
    rows = []
    for label, members, pooled in views(data, pooled=True, all_games=True):
        r_all = res[res["tc_group"].isin(members)]
        for side in SIDES:
            r = r_all[r_all["side"] == side]
            for measure in CLOCK_MEASURES:
                for who, name in (("other", "other side"), ("own", "resigner")):
                    counts = r[f"{who}_bin_{measure}"].value_counts()
                    for band in CLOCK_LABELS:
                        rows.append({"tc_group": label, "pooled": pooled, "side": side, "clock_of": name, "clock_measure": measure, "clock": band, "share": counts.get(band, 0) / len(r) if len(r) else np.nan, "count": int(counts.get(band, 0)), "resignations": len(r), "thin": len(r) < MIN_RESIGNATIONS})
    results.table("4c_resign_clock", pd.DataFrame(rows), "Where on the clock the resignations happened: share of each side's resignations by the other side's clock and by the resigner's own clock, both measures.")

    t = cp_clock[(cp_clock["clock_measure"] == MAIN_MEASURE) & (cp_clock["side"] == "player")]
    fig, axes = charts.group_grid(data.tc_groups, panel=(3.0, 2.6), sharey=True, title="Does the opponent's clock change what a bad position is worth?")
    x = np.arange(len(CP_LABELS))
    for group, ax in axes.items():
        part = t[t["tc_group"] == group]
        drawn = False
        for k, band in enumerate(CLOCK_LABELS):
            cell = part[part["other_clock"] == band].set_index("cp_bin").reindex(CP_LABELS)
            est = cell["score"].where(~cell["thin"].fillna(True).astype(bool))
            if est.notna().sum() == 0:
                continue
            lo = cell["score_lo"].where(est.notna())
            hi = cell["score_hi"].where(est.notna())
            colour = charts.SERIES[k]
            ax.plot(x, est.to_numpy(dtype=float), color=colour, marker="o", markersize=4, linewidth=1.5, label=f"opponent's buffer {band}")
            ax.fill_between(x, lo.to_numpy(dtype=float), hi.to_numpy(dtype=float), color=colour, alpha=0.15, linewidth=0)
            drawn = True
        if not drawn:
            charts.thin_note(ax)
            continue
        ax.set_xticks(x, CP_LABELS, rotation=45, ha="right", rotation_mode="anchor", fontsize=7)
        ax.set_ylim(0, 1)
    for ax in axes.values():
        ax.set_xlabel("engine eval, your view (pawns)", fontsize=7.5)
    label_first_column(axes, "your played-out score")
    charts.legend(fig, loc="upper right", bbox_to_anchor=(0.99, 1.0))
    fig.tight_layout()
    charts.footnote(fig, "Your positions in games you did not resign or abandon, by engine eval and the opponent's clock beyond one increment; y = how those games ended for you (win 1, draw 1/2). Cells with under 30 games are not drawn.")
    results.figure(fig, "4_clock_by_eval")
    return cp_clock


# --- 7.5 cost ---------------------------------------------------------------------------------------------------


def value_all(pos: pd.DataFrame, res: pd.DataFrame, games: pd.DataFrame, data) -> tuple[dict, pd.Series]:
    """Values every resignation per control: the bootstrap results and the played-out value per resignation (indexed like `res`)."""
    per_group, values = {}, []
    for k, group in enumerate(data.tc_groups):
        gids = np.sort(games.loc[games["tc_group"] == group, "uuid"].to_numpy())
        p = pos[pos["tc_group"] == group]
        r = res[res["tc_group"] == group]
        per_group[group] = valuation(p, r, gids, seed=SEED + k)
        values.append(per_group[group][("player", "played_out")]["values"])
        values.append(per_group[group][("opponent", "played_out")]["values"])
    return per_group, pd.concat(values).reindex(res.index)


def q5_cost(results: Results, per_group: dict, res: pd.DataFrame, games: pd.DataFrame, data) -> None:
    rows, net_rows = [], []
    for label, members, pooled in views(data, pooled=True, all_games=True):
        g = games[games["tc_group"].isin(members)]
        n_games = len(g)
        score = float(g["my_score"].mean())
        boots = {}
        for side in SIDES:
            v = [per_group[m][(side, "played_out")] for m in members]
            a = [per_group[m][(side, "all_positions")] for m in members]
            total = sum(e["total"] for e in v)
            n_res = sum(e["n_res"] for e in v)
            boot_total = np.sum([e["boot_total"] for e in v], axis=0)
            boot_nres = np.sum([e["boot_nres"] for e in v], axis=0)
            boots[side] = boot_total
            r = res[res["tc_group"].isin(members) & (res["side"] == side)]
            m_est, m_lo, m_hi = cluster_bootstrap_mean(r["ev"], r["game_uuid"], n_boot=N_BOOT, seed=SEED)
            with np.errstate(invalid="ignore", divide="ignore"):
                per_res_lo, per_res_hi = q(boot_total / boot_nres)
            tot_lo, tot_hi = q(boot_total)
            per100, per100_lo, per100_hi = 100 * total / n_games, 100 * tot_lo / n_games, 100 * tot_hi / n_games
            # Elo: the rating gap worth the same score. Your resignations: from your score up to score + cost.
            # The opponents' resignations: from score - gift (without them) up to your score.
            base = score if side == "player" else score - total / n_games
            base_lo = score if side == "player" else score - tot_lo / n_games
            base_hi = score if side == "player" else score - tot_hi / n_games
            rows.append({
                "tc_group": label, "pooled": pooled, "side": side,
                "played_out_per_resignation": total / n_res if n_res else np.nan, "played_out_per_resignation_lo": per_res_lo, "played_out_per_resignation_hi": per_res_hi,
                "played_out_total": total, "played_out_total_lo": tot_lo, "played_out_total_hi": tot_hi,
                "per_100_games": per100, "per_100_games_lo": per100_lo, "per_100_games_hi": per100_hi,
                "your_score": score,
                "elo": float(excess_to_elo(base, per100 / 100)), "elo_lo": float(excess_to_elo(base_lo, per100_lo / 100)), "elo_hi": float(excess_to_elo(base_hi, per100_hi / 100)),
                "model_per_resignation": m_est, "model_per_resignation_lo": m_lo, "model_per_resignation_hi": m_hi, "model_total": float(r["ev"].sum()),
                "all_positions_total": sum(e["total"] for e in a), "all_positions_per_100_games": 100 * sum(e["total"] for e in a) / n_games,
                "resignations": n_res, "games": n_games, "thin": n_res < MIN_RESIGNATIONS,
            })  # fmt: skip
        net = (boots["opponent"] - boots["player"]) / n_games
        n_est = (rows[-1]["played_out_total"] - rows[-2]["played_out_total"]) / n_games
        n_lo, n_hi = q(net)
        net_rows.append({
            "tc_group": label, "pooled": pooled, "net_per_100_games": 100 * n_est, "net_per_100_games_lo": 100 * n_lo, "net_per_100_games_hi": 100 * n_hi, "your_score": score,
            "elo": float(excess_to_elo(score - n_est, n_est)), "elo_lo": float(excess_to_elo(score - n_lo, n_lo)), "elo_hi": float(excess_to_elo(score - n_hi, n_hi)),
            "player_resignations": rows[-2]["resignations"], "opponent_resignations": rows[-1]["resignations"], "games": n_games,
            "thin": min(rows[-2]["resignations"], rows[-1]["resignations"]) < MIN_RESIGNATIONS,
        })  # fmt: skip
    table = pd.DataFrame(rows)
    results.table(
        "5_cost", table,
        "Expected points given away by resigning. side=player: your resignations (what you gave away); side=opponent: theirs (what they handed you). Each resignation valued at the played-out score of its expected-score band in its control (CI: one game bootstrap over band scores and resignations); per 100 games of the control and as an Elo-equivalent (the rating gap worth the same score change from your actual score). Model valuation and an all-positions lower bound (resigned games' positions counted as losses) alongside.",
    )  # fmt: skip
    net = pd.DataFrame(net_rows)
    results.table("5b_net", net, "Net effect of resignations on your score: opponents' resignations handed to you minus yours given away, per 100 games and as Elo (same bootstrap draws for both sides).")

    import matplotlib.pyplot as plt

    order = [*data.tc_groups, ALL]
    fig, (left, right, net_ax) = plt.subplots(1, 3, figsize=(12.5, 4.1), sharey=True, gridspec_kw={"width_ratios": [1, 1, 0.8]})
    thin = table[table["side"] == "player"].set_index("tc_group").reindex(order)["thin"].to_numpy() | table[table["side"] == "opponent"].set_index("tc_group").reindex(order)["thin"].to_numpy()
    for ax, value, title in ((left, "per_100_games", "Points per 100 games"), (right, "elo", "Elo-equivalent")):
        w = pd.DataFrame({"tc_group": order})
        for side in SIDES:
            part = table[table["side"] == side].set_index("tc_group").reindex(order)
            ok = ~part["thin"].astype(bool)
            for suffix in ("", "_lo", "_hi"):
                w[f"{side}{suffix}"] = part[f"{value}{suffix}"].where(ok).to_numpy()
        charts.side_bars(ax, w, "tc_group", order=order, horizontal=True)
        ax.set_title(title)
    shown_net = net.set_index("tc_group").reindex(order)
    for c in ("net_per_100_games", "net_per_100_games_lo", "net_per_100_games_hi"):
        shown_net[c] = shown_net[c].where(~shown_net["thin"].astype(bool))
    charts.estimate(net_ax, shown_net.reset_index(), "tc_group", y="net_per_100_games", lo="net_per_100_games_lo", hi="net_per_100_games_hi", order=order, colour=charts.PLAYER, horizontal=True)
    net_ax.set_title("Net: handed to you minus given away")
    for ax in (left, right, net_ax):
        charts.zero_line(ax, horizontal=False)
        for i, t in enumerate(thin):
            if t:
                ax.text(0.02, i, "too few resignations", transform=ax.get_yaxis_transform(), va="center", fontsize=7, color=charts.INK_MUTED)
        ax.axhline(len(order) - 1.5, color=charts.GRID, linewidth=1)
    left.set_xlabel("expected points per 100 games")
    right.set_xlabel("Elo")
    net_ax.set_xlabel("expected points per 100 games")
    if not left.yaxis_inverted():
        left.invert_yaxis()
    fig.suptitle("How much has resigning cost you?", x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    fig.tight_layout()
    fig.subplots_adjust(top=0.82)
    fig.legend(handles=left.get_legend_handles_labels()[0][:2], labels=["your resignations (points given away)", "opponents' resignations (points handed to you)"], loc="upper right", ncol=2, fontsize=8)
    charts.footnote(fig, "Each resignation valued at what positions with the same model expected score scored when played out, in the same control (95% CI, games resampled). Elo: the rating gap worth the same change in your score. Net below 0: resigning costs you more than your opponents' resignations give back.")
    results.figure(fig, "5_cost")


# --- 7.6 when is it safe ----------------------------------------------------------------------------------------


def safe_band(cells: pd.DataFrame, band: str, labels: list[str], cost: float) -> str:
    """Highest band such that it and every worse band (with enough games) scored at most `cost` (upper CI)."""
    top = "none"
    for lab in labels:  # both band lists run from worst to best
        cell = cells[cells[band].astype(str) == lab]
        if cell.empty or bool(cell["thin"].iloc[0]):
            continue  # uninformative: neither confirms nor breaks the run
        if cell["score_hi"].iloc[0] <= cost:
            top = lab
        else:
            break
    return top


def q6_safe(results: Results, pos: pd.DataFrame, res: pd.DataFrame, cp_clock: pd.DataFrame, data) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize

    pooled_views = [label for label, _ in class_views(data)]
    mine = cp_clock[(cp_clock["side"] == "player")]
    table = mine[mine["tc_group"].isin(pooled_views)].reset_index(drop=True)
    results.table("6_safe_map", table, "Your played-out score by engine eval band x opponent's clock, pooled by time class ('all bullet', 'all blitz'; rapid has one control), both clock measures: the per-control cells are mostly thin outside 1+0 and 3+2 (4_clock_by_eval has them all).")

    rows = []
    view_list = views(data, pooled=True)
    ev_any = played_out_table(pos, res, "ev_bin", [], view_list)
    cp_any = played_out_table(pos, res, "cp_bin", [], view_list)
    ev_clock = clock_table(pos[pos["side"] == "player"], res[res["side"] == "player"], "ev_bin", view_list)
    for label, _, pooled in view_list:
        for scale, band, labels, any_t, clock_t in (("expected score", "ev_bin", EV_LABELS, ev_any, ev_clock), ("engine eval", "cp_bin", CP_LABELS, cp_any, mine)):
            for measure, clock in [("any", "any"), *[(m, c) for m in CLOCK_MEASURES for c in CLOCK_LABELS]]:
                if clock == "any":
                    cells = any_t[(any_t["tc_group"] == label) & (any_t["side"] == "player")]
                else:
                    cells = clock_t[(clock_t["tc_group"] == label) & (clock_t["clock_measure"] == measure) & (clock_t["other_clock"].astype(str) == clock)]
                for cost in SAFE_COSTS:
                    rows.append({
                        "tc_group": label, "pooled": pooled, "scale": scale, "clock_measure": measure, "opponent_clock": clock, "cost_cap": cost,
                        "safe_up_to": safe_band(cells, band, labels, cost), "games_min": int(cells["games"].min()) if len(cells) else 0, "cells_not_thin": int((~cells["thin"]).sum()),
                    })  # fmt: skip
    results.table("6b_safe_threshold", pd.DataFrame(rows), "Rule of thumb for your positions: the best band (expected score, or engine eval from worst to best) up to which every band with enough games scored at most 0.02 / 0.05 when played out (upper CI end), per control, pooled by time class, and by the opponent's clock. 'none': even the worst band scored more.")

    t = table[table["clock_measure"] == MAIN_MEASURE]
    fig, axs = plt.subplots(1, len(pooled_views), figsize=(3.3 * len(pooled_views) + 0.8, 3.6), sharey=True)
    axs = np.atleast_1d(axs)
    cmap = plt.get_cmap(charts.SEQUENTIAL)
    norm = Normalize(vmin=0, vmax=MAP_VMAX)
    for ax, label in zip(axs, pooled_views):
        part = t[t["tc_group"] == label]
        grid = np.full((len(CP_LABELS), len(CLOCK_LABELS)), np.nan)
        thin = np.ones_like(grid, dtype=bool)
        for i, cp in enumerate(CP_LABELS):
            for j, clock in enumerate(CLOCK_LABELS):
                cell = part[(part["cp_bin"].astype(str) == cp) & (part["other_clock"].astype(str) == clock)]
                if len(cell):
                    grid[i, j] = cell["score"].iloc[0]
                    thin[i, j] = bool(cell["thin"].iloc[0])
        shown = np.where(thin, np.nan, grid)
        ax.imshow(np.ma.masked_invalid(shown), cmap=cmap, norm=norm, aspect="auto")
        for i in range(grid.shape[0]):
            for j in range(grid.shape[1]):
                if thin[i, j]:
                    ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, facecolor=charts.GRID, edgecolor=charts.SURFACE))
                    ax.text(j, i, "few", ha="center", va="center", fontsize=7, color=charts.INK_MUTED)
                else:
                    v = shown[i, j]
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8, color="white" if v > MAP_VMAX * 0.6 else charts.INK)
        ax.set_xticks(range(len(CLOCK_LABELS)), CLOCK_LABELS)
        ax.set_yticks(range(len(CP_LABELS)), CP_LABELS)
        ax.set_xlabel("opponent's clock beyond increment", fontsize=7.5)
        ax.set_title(label)
        ax.grid(False)
        for spine in ax.spines.values():
            spine.set_visible(False)
    axs[0].set_ylabel("engine eval, your view (pawns)")
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    fig.subplots_adjust(top=0.84, bottom=0.2, right=0.88, wspace=0.12)
    cbar = fig.colorbar(sm, cax=fig.add_axes((0.9, 0.2, 0.015, 0.64)))
    cbar.set_label("your played-out score", fontsize=8)
    cbar.outline.set_visible(False)
    fig.suptitle("When is resigning safe? What your positions scored when you played on", x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    charts.footnote(fig, f"Pooled by time class ({pool_note(data)}): per control most cells are thin. Light = little to gain by playing on; dark = resigning gives points away. 'few' = under 30 games.")
    results.figure(fig, "6_safe_map")


# --- main -----------------------------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet", columns=["uuid", "my_p_win_max", "opp_p_win_max"])
    games = data.games.merge(decomposed, on="uuid", how="left")
    games = games[games["tc_group"].isin(data.tc_groups)].copy()
    games["tc_group"] = games["tc_group"].astype(str)
    moves = data.moves[data.moves["tc_group"].isin(data.tc_groups)]
    results = Results(SLUG)

    pos = positions(moves)
    res = resignations(pos, moves, games)
    games_per_view = games["tc_group"].value_counts().to_dict()

    no_moves = games[~games["uuid"].isin(pos["game_uuid"])]
    notes = pd.DataFrame(
        [{"tc_group": g, "games": int(games_per_view.get(g, 0)), "player_resigned": int((games.loc[games["tc_group"] == g, "my_result"] == "resigned").sum()),
          "opponent_resigned": int((games.loc[games["tc_group"] == g, "opponent_result"] == "resigned").sum()),
          "player_resignations_with_moves": int(((res["tc_group"] == g) & (res["side"] == "player")).sum()),
          "opponent_resignations_with_moves": int(((res["tc_group"] == g) & (res["side"] == "opponent")).sum()),
          "games_without_moves": int((no_moves["tc_group"] == g).sum()), "reported": True} for g in data.tc_groups]
        + [{"tc_group": g, "games": n, "reported": False} for g, n in data.tc_noted.items()]
    )  # fmt: skip
    for c in notes.columns.drop(["tc_group", "reported"]):
        notes[c] = notes[c].astype("Int64")
    results.table("0_games", notes, "Games and resignations per control; controls with reported=False are too thin to analyse (listed only). Games that ended before the first move have no resignation position.")

    per_group, valued = value_all(pos, res, games, data)
    q1_share(results, games, data)
    q2_position(results, res, data, valued, games_per_view)
    q3_played_out(results, pos, res, data)
    cp_clock = q4_clock(results, pos, res, data)
    q5_cost(results, per_group, res, games, data)
    q6_safe(results, pos, res, cp_clock, data)
    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
