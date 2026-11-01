"""Q7: expected value of not resigning.

How many expected points the player's resignations gave away (and how many
the opponents' resignations handed over), judged two ways: the calibrated
model's P(win) + P(draw)/2 at the resignation position, and the robust
empirical version: how positions the model rated the same actually ended in
games that were played out (not resigned/abandoned by the side under study),
by time class and clock. Then a split of resignations into 'clearly lost',
'nearly lost' and 'still playable', a 'had to go' proxy, and a rule of thumb.

Run: .venv/bin/python analysis/07_resignation.py
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from chess_analysis.toolkit import (
    TIME_CLASSES,
    Report,
    cluster_bootstrap_mean,
    figure_style,
    load_data,
    repo_root,
)

SLUG = "07_resignation"
N_BOOT = 500
SEED = 7
EV_BINS = [0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0001]
EV_LABELS = ["<0.02", "0.02–0.05", "0.05–0.10", "0.10–0.15", "0.15–0.20", "0.20–0.30", "0.30–0.50", "≥0.50"]
CP_BINS = [-100000, -2000, -1000, -500, -300, -100, 100000]
CP_LABELS = ["mate / ≤ −2000", "−2000..−1000", "−1000..−500", "−500..−300", "−300..−100", "> −100"]
CLOCK_BINS = [0, 10, 30, 1e9]
CLOCK_LABELS = ["<10 s", "10–30 s", "≥30 s"]
MIN_GAMES = 20  # cells with fewer games are blanked
ELO_SLOPE = np.log(10) / 1600  # d(expected score)/d(rating) at equal ratings, per Elo point
SIDES = {"player": ("my_result", "opp_"), "opponent": ("opponent_result", "my_")}
SIDE_NAME = {"player": "you", "opponent": "opponents"}
SIDE_POSS = {"player": "your", "opponent": "opponents'"}


# --- small helpers -------------------------------------------------------------


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    if np.isnan(est):
        return ""
    if np.isnan(lo):
        return f"{est:{f}}"
    return f"{est:{f}} [{lo:{f}}, {hi:{f}}]"


def boot_mean(values: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """Mean over games (one value per game) with a bootstrap CI."""
    v = np.asarray(values, dtype="float64")
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return float("nan"), float("nan"), float("nan")
    if len(v) < 2:
        return float(v.mean()), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(v), size=(n_boot, len(v)))
    boot = v[idx].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(v.mean()), float(lo), float(hi)


def boot_rate(y, cond, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float, int]:
    """P(y | cond) over games with a bootstrap CI; (est, lo, hi, n_cond). Copied from analysis/01_decomposition.py."""
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
    """P(y1|c1) - P(y2|c2), both rates on the same resampled games. Copied from analysis/01_decomposition.py."""
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


def boot_mean_diff(a: np.ndarray, b: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """mean(a) - mean(b) for two independent samples of games, bootstrapped separately."""
    a, b = (np.asarray(x, dtype="float64") for x in (a, b))
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    ba = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    bb = b[rng.integers(0, len(b), size=(n_boot, len(b)))].mean(axis=1)
    lo, hi = np.quantile(ba - bb, [0.025, 0.975])
    return float(a.mean() - b.mean()), float(lo), float(hi)


def calibration_brier() -> str:
    """Cross-validated expected-points Brier of the P(win) model, read from reports/calibration.md."""
    import re

    path = repo_root() / "reports" / "calibration.md"
    if path.exists():
        hit = re.search(r"Expected points Brier \| ([0-9.]+)", path.read_text())
        if hit:
            return f"cross-validated expected-points Brier {float(hit.group(1)):.3f}, see `reports/calibration.md`"
    return "see `reports/calibration.md` for its cross-validated Brier score"


# --- data preparation ------------------------------------------------------------


def side_view(moves: pd.DataFrame) -> pd.DataFrame:
    """Every position after a move, seen from each side: P(win), P(draw), EV, cp, clocks and the eventual score."""
    m = moves.sort_values(["game_uuid", "ply"]).copy()
    mine = m["is_player_move"].to_numpy()
    m["score_player"] = m["outcome"].map({"win": 1.0, "draw": 0.5, "loss": 0.0})
    m["score_opponent"] = 1.0 - m["score_player"]
    m["p_win_player"] = np.where(mine, m["p_win_after"], m["p_loss_after"])
    m["p_win_opponent"] = np.where(mine, m["p_loss_after"], m["p_win_after"])
    m["ev_player"] = m["p_win_player"] + 0.5 * m["p_draw_after"]
    m["ev_opponent"] = m["p_win_opponent"] + 0.5 * m["p_draw_after"]
    m["cp_player"] = np.where(mine, m["cp_after"], -m["cp_after"])
    m["cp_opponent"] = -m["cp_player"]
    m["clock_player"] = np.where(mine, m["clock_seconds"], m["opponent_clock_seconds"])
    m["clock_opponent"] = np.where(mine, m["opponent_clock_seconds"], m["clock_seconds"])
    # The final position of a game that ended on the board (mate, stalemate): nobody can decide anything there.
    is_last = m["ply"].to_numpy() == m.groupby("game_uuid")["ply"].transform("max").to_numpy()
    ended_on_board = m["my_result"].isin(["checkmated", "stalemate"]) | m["opponent_result"].isin(["checkmated", "stalemate"])
    m["board_terminal"] = is_last & ended_on_board.to_numpy()
    for side in SIDES:
        m[f"ev_bin_{side}"] = pd.cut(m[f"ev_{side}"], EV_BINS, labels=EV_LABELS, right=False)
        m[f"cp_bin_{side}"] = pd.cut(m[f"cp_{side}"], CP_BINS, labels=CP_LABELS, right=False)
    return m


def last_positions(view: pd.DataFrame) -> pd.DataFrame:
    """The position after the last move of every game with moves, indexed by game uuid."""
    return view.groupby("game_uuid").tail(1).set_index("game_uuid")


def last_own_moves(view: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Each side's last own move per game (ep_loss, time spent), indexed by game uuid."""
    out = {}
    for side, flag in (("player", True), ("opponent", False)):
        own = view[view["is_player_move"] == flag]
        last = own.groupby("game_uuid").tail(1).set_index("game_uuid")
        med = own.groupby("game_uuid")["time_spent_seconds"].median()
        last = last.assign(own_median_time=med.reindex(last.index))
        out[side] = last[["ep_loss", "cp_loss", "time_spent_seconds", "own_median_time", "label", "clock_fraction"]]
    return out


def resignations(games: pd.DataFrame, last: pd.DataFrame, own: dict[str, pd.DataFrame], side: str) -> pd.DataFrame:
    """One row per game resigned by `side`, with the resignation position from the resigner's view."""
    result_col = SIDES[side][0]
    g = games[games[result_col] == "resigned"].set_index("uuid")
    g = g[g.index.isin(last.index)]  # games with at least one move
    pos = last.reindex(g.index)
    other = "opponent" if side == "player" else "player"
    out = pd.DataFrame(
        {
            "time_class": g["time_class"],
            "year": g["year"],
            "p_win": pos[f"p_win_{side}"],
            "p_draw": pos["p_draw_after"],
            "ev": pos[f"ev_{side}"],
            "ev_bin": pos[f"ev_bin_{side}"],
            "cp": pos[f"cp_{side}"],
            "cp_bin": pos[f"cp_bin_{side}"],
            "clock_own": pos[f"clock_{side}"],
            "clock_other": pos[f"clock_{other}"],
            "clock_own_frac": pos[f"clock_{side}"] / g["base_seconds"].astype(float),
            "resigner_moved_last": pos["is_player_move"] == (side == "player"),
            "ply": pos["ply"],
            "last_in_session": g["game_in_session"] == g["session_games"],
            "session_games": g["session_games"],
        }
    )
    own_last = own[side].reindex(g.index)
    out["last_own_ep_loss"] = own_last["ep_loss"]
    out["last_own_time"] = own_last["time_spent_seconds"]
    out["last_own_quick"] = own_last["time_spent_seconds"] <= own_last["own_median_time"]
    out["clock_other_bin"] = pd.cut(out["clock_other"], CLOCK_BINS, labels=CLOCK_LABELS, right=False)
    out["clock_own_bin"] = pd.cut(out["clock_own"], CLOCK_BINS, labels=CLOCK_LABELS, right=False)
    return out


def played_out(view: pd.DataFrame, side: str) -> pd.DataFrame:
    """Positions from games the side under study did not resign or abandon (its 'played-out' games)."""
    result_col = SIDES[side][0]
    return view[~view[result_col].isin(["resigned", "abandoned"]) & ~view["board_terminal"]]


# --- empirical bin rates with joint bootstrap ---------------------------------------


def bin_rates(frame: pd.DataFrame, bin_col: str, score_col: str, extra: list[str] | None = None) -> pd.DataFrame:
    """Observed eventual score of `score_col` per bin (and extra keys) with game-clustered CIs."""
    keys = [*(extra or []), bin_col]
    rows = []
    for key, part in frame.groupby(keys, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        est, lo, hi = cluster_bootstrap_mean(part[score_col], part["game_uuid"], n_boot=N_BOOT, seed=SEED)
        rows.append({**dict(zip(keys, key)), "score": est, "lo": lo, "hi": hi, "positions": len(part), "games": part["game_uuid"].nunique()})
    return pd.DataFrame(rows)


def empirical_total(res: pd.DataFrame, comp: pd.DataFrame, bin_col: str, score_col: str, res_bin_col: str = "ev_bin", n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """Mean and total empirical EV of the resignations in `res`, each valued at the played-out score of its bin.

    Joint bootstrap: resample the comparison games (uncertainty in the bin
    rates) and the resigned games (uncertainty in their composition).
    """
    labels = list(res[res_bin_col].cat.categories)
    per_game = comp.groupby(["game_uuid", bin_col], observed=False)[score_col].agg(["sum", "count"]).unstack(bin_col).fillna(0.0)
    sums = per_game["sum"].reindex(columns=labels, fill_value=0.0).to_numpy()
    counts = per_game["count"].reindex(columns=labels, fill_value=0.0).to_numpy()
    rates = sums.sum(axis=0) / np.maximum(counts.sum(axis=0), 1)
    res_codes = res[res_bin_col].cat.codes.to_numpy()
    res_codes = res_codes[res_codes >= 0]
    n_res = len(res_codes)
    if n_res == 0:
        return {"mean": np.nan, "lo": np.nan, "hi": np.nan, "total": np.nan, "total_lo": np.nan, "total_hi": np.nan, "n": 0, "rates": rates}
    est_mean = float(rates[res_codes].mean())
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    for b in range(n_boot):
        gi = rng.integers(0, len(sums), size=len(sums))
        r = sums[gi].sum(axis=0) / np.maximum(counts[gi].sum(axis=0), 1)
        ri = rng.integers(0, n_res, size=n_res)
        boot[b] = r[res_codes[ri]].mean()
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"mean": est_mean, "lo": float(lo), "hi": float(hi), "total": est_mean * n_res, "total_lo": float(lo) * n_res, "total_hi": float(hi) * n_res, "n": n_res, "rates": rates}


def rule_threshold(rates: pd.DataFrame, cost: float) -> str:
    """Highest EV bin such that it and every bin below it have played-out score (upper CI) <= cost; 'none' if the lowest bin fails."""
    top = "none"
    for lab in EV_LABELS:
        cell = rates[rates["ev_bin"] == lab]
        if cell.empty or cell["games"].iloc[0] < MIN_GAMES:
            continue  # uninformative cell: neither confirms nor breaks the run
        if cell["hi"].iloc[0] <= cost:
            top = lab
        else:
            break
    return top


# --- report sections ---------------------------------------------------------------


def section_frequency(report: Report, games: pd.DataFrame, res: dict[str, pd.DataFrame]) -> dict:
    report.heading("1. How often each side resigns, and from what")
    report.text(
        "Resignations as a share of each side's losses, by time class, with game-bootstrap CIs; then the resignation position from the resigner's view: "
        "the calibrated model's P(win), P(draw) and expected value EV = P(win) + ½P(draw) at the position after the last move of the game (flipped to the resigner's "
        "view when the opponent moved last), and how often the resigner still had model P(win) ≥ 0.2 ('still playable') or ≥ 0.1. 'Opponents' are the same "
        "quantities for the opponents' resignations in the same pool of games. Model probabilities are model-relative (see section 3 for the empirical check)."
    )
    rows, out = [], {}
    for tc in TIME_CLASSES:
        g = games[games["time_class"] == tc]
        row = {"time class": tc, "games": len(g)}
        for side, (col, _) in SIDES.items():
            lost = (g["outcome"] == ("loss" if side == "player" else "win")).to_numpy()
            resigned = (g[col] == "resigned").to_numpy()
            est, lo, hi, n = boot_rate(resigned, lost)
            row[f"{SIDE_NAME[side]}: resigned / losses"] = f"{fmt(est, lo, hi)} (n={n})"
            r = res[side][res[side]["time_class"] == tc]
            row[f"{SIDE_NAME[side]}: median EV"] = f"{r['ev'].median():.3f}"
            est, lo, hi = boot_mean((r["p_win"] >= 0.2).to_numpy())
            row[f"{SIDE_NAME[side]}: P(win) ≥ 0.2"] = fmt(est, lo, hi, ".2f")
            est, lo, hi = boot_mean((r["p_win"] >= 0.1).to_numpy())
            row[f"{SIDE_NAME[side]}: P(win) ≥ 0.1"] = fmt(est, lo, hi, ".2f")
            out[(tc, side, "playable_share")] = (r["p_win"] >= 0.2).mean()
        yl = (g["outcome"] == "loss").to_numpy()
        ol = (g["outcome"] == "win").to_numpy()
        d = boot_rate_diff((g["my_result"] == "resigned").to_numpy(), yl, (g["opponent_result"] == "resigned").to_numpy(), ol)
        row["resign share, you − them"] = fmt(*d)
        out[(tc, "diff")] = d
        for side, (col, _) in SIDES.items():
            g_lost = g[g["outcome"] == ("loss" if side == "player" else "win")]
            out[(tc, side, "resign_share")] = float((g_lost[col] == "resigned").mean())
        rows.append(row)
    table = pd.DataFrame(rows)
    report.table(table[["time class", "games", "you: resigned / losses", "opponents: resigned / losses", "resign share, you − them"]])
    report.text("Where the resignations happened (resigner's view, model probabilities):")
    report.table(table[["time class", "you: median EV", "opponents: median EV", "you: P(win) ≥ 0.2", "opponents: P(win) ≥ 0.2", "you: P(win) ≥ 0.1", "opponents: P(win) ≥ 0.1"]])
    rp, ro = res["player"], res["opponent"]
    report.text(
        f"You moved last in {rp['resigner_moved_last'].mean():.0%} of your resignations (i.e. you resigned during the opponent's turn, usually straight after your own move); "
        f"opponents moved last in {ro['resigner_moved_last'].mean():.0%} of theirs. Median engine eval at your resignations: {rp['cp'].median():.0f} cp from your side "
        f"(opponents: {ro['cp'].median():.0f}); {(rp['cp'] > -300).mean():.1%} of yours were better than −300 cp (opponents {(ro['cp'] > -300).mean():.1%}) and "
        f"{(rp['cp'] >= 0).sum()} of yours ({(rp['cp'] >= 0).mean():.1%}) were level or better on the board (opponents {(ro['cp'] >= 0).sum()})."
    )
    return out


def section_model_ev(report: Report, res: dict[str, pd.DataFrame]) -> pd.DataFrame:
    report.heading("2. Expected points given away by resigning, model view")
    report.text(
        "For every resigned game, EV at the resignation position (resigner's view) is what a perfectly played-out continuation from that position would score "
        "according to the calibrated P(win) model. Mean per resigned game with a bootstrap CI over resigned games, and the total (mean × number of resignations). "
        "Your total is what you gave away; the opponents' total is what they handed to you. 'Per 100 games' spreads the cost over every game of the time class "
        "(resigned or not), and the Elo column converts that into the rating difference that would produce the same change in expected score "
        f"(slope ln(10)/1600 ≈ {ELO_SLOPE:.5f} points per Elo at equal ratings; indicative only). Caveat: the model is under-confident at the extremes and was "
        "fitted on all games including the resigned ones, so section 3's played-out comparison is the robust estimate."
    )
    rows = []
    for tc in TIME_CLASSES:
        for side in SIDES:
            r = res[side][res[side]["time_class"] == tc]
            est, lo, hi = boot_mean(r["ev"].to_numpy())
            rows.append({"time class": tc, "side": SIDE_NAME[side], "resignations": len(r), "mean EV": est, "lo": lo, "hi": hi, "total": est * len(r), "total_lo": lo * len(r), "total_hi": hi * len(r)})
    table = pd.DataFrame(rows)
    return table


def section_empirical(report: Report, view: pd.DataFrame, res: dict[str, pd.DataFrame], model_table: pd.DataFrame, n_games: dict[str, int]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    report.heading("3. The empirical check: how played-out games from the same positions actually ended")
    report.text(
        "The model's P(win) is model-relative (Q1: under-confident at the extremes, and you out-perform it in every bin). The robust counterfactual is empirical: "
        "take every position after a move in games the side under study did **not** resign or abandon (its 'played-out' games), bin it by the model EV from that "
        "side's view, and look at how those games actually ended for that side (score = win 1, draw ½, loss 0; CIs clustered by game). The table shows those rates "
        "for your positions and for the opponents' positions, with the number of your / their resignations falling in each bin. A resignation is then valued at "
        "the played-out score of its bin (same side, same time class)."
    )
    comps = {side: played_out(view, side) for side in SIDES}
    rates = {side: bin_rates(comps[side], f"ev_bin_{side}", f"score_{side}", extra=["time_class"]).rename(columns={f"ev_bin_{side}": "ev_bin"}) for side in SIDES}
    for side in SIDES:
        rates[side]["side"] = side
    rate_table = pd.concat(rates.values(), ignore_index=True)

    for tc in TIME_CLASSES:
        rows = []
        for lab in EV_LABELS:
            row = {"model EV bin": lab}
            for side in SIDES:
                rt = rates[side][(rates[side]["time_class"] == tc) & (rates[side]["ev_bin"] == lab)]
                r = res[side][(res[side]["time_class"] == tc) & (res[side]["ev_bin"] == lab)]
                if len(rt) and rt["games"].iloc[0] >= MIN_GAMES:
                    row[f"{SIDE_NAME[side]}: played-out score"] = fmt(rt["score"].iloc[0], rt["lo"].iloc[0], rt["hi"].iloc[0])
                    row[f"{SIDE_NAME[side]}: positions (games)"] = f"{int(rt['positions'].iloc[0]):,} ({int(rt['games'].iloc[0])})"
                else:
                    row[f"{SIDE_NAME[side]}: played-out score"] = "–"
                    row[f"{SIDE_NAME[side]}: positions (games)"] = f"{int(rt['positions'].iloc[0]):,} ({int(rt['games'].iloc[0])})" if len(rt) else "0"
                row[f"{SIDE_NAME[side]}: resignations"] = len(r)
            rows.append(row)
        report.text(f"**{tc}**")
        report.table(pd.DataFrame(rows))

    # Empirical valuation of every resignation.
    rows, emp = [], {}
    for tc in TIME_CLASSES:
        for side in SIDES:
            r = res[side][res[side]["time_class"] == tc]
            c = comps[side][comps[side]["time_class"] == tc]
            e = empirical_total(r, c, f"ev_bin_{side}", f"score_{side}")
            e_all = empirical_total(r, view[(view["time_class"] == tc) & ~view["board_terminal"]], f"ev_bin_{side}", f"score_{side}")
            e["all_mean"], e["all_total"] = e_all["mean"], e_all["total"]
            emp[(tc, side)] = e
            mt = model_table[(model_table["time class"] == tc) & (model_table["side"] == SIDE_NAME[side])].iloc[0]
            per100 = 100 * e["total"] / n_games[tc]
            rows.append(
                {
                    "time class": tc,
                    "side": SIDE_NAME[side],
                    "resignations": e["n"],
                    "model EV / resignation": fmt(mt["mean EV"], mt["lo"], mt["hi"]),
                    "model total": fmt(mt["total"], mt["total_lo"], mt["total_hi"], ".1f"),
                    "played-out EV / resignation": fmt(e["mean"], e["lo"], e["hi"]),
                    "played-out total": fmt(e["total"], e["total_lo"], e["total_hi"], ".1f"),
                    "all-positions total": f"{e['all_total']:.1f}",
                    "per 100 games (played-out)": fmt(per100, 100 * e["total_lo"] / n_games[tc], 100 * e["total_hi"] / n_games[tc], ".2f"),
                    "≈ Elo": f"{per100 / 100 / ELO_SLOPE:.0f}",
                }
            )
    summary = pd.DataFrame(rows)
    report.text(
        "Both valuations side by side. The model column is section 2; the played-out column values each resignation at the observed score of its EV bin "
        "(joint bootstrap over comparison games and resigned games). 'All-positions' repeats the empirical valuation with the comparison set extended to every game, "
        "resigned ones included (their positions count as losses): a lower bound that needs no model calibration."
    )
    report.table(summary)
    you_tot = sum(emp[(tc, "player")]["total"] for tc in TIME_CLASSES)
    them_tot = sum(emp[(tc, "opponent")]["total"] for tc in TIME_CLASSES)
    you_model = model_table[model_table["side"] == "you"]["total"].sum()
    them_model = model_table[model_table["side"] == "opponents"]["total"].sum()
    n_you = sum(emp[(tc, "player")]["n"] for tc in TIME_CLASSES)
    n_them = sum(emp[(tc, "opponent")]["n"] for tc in TIME_CLASSES)
    report.text(
        f"Across all three time classes your {n_you} resignations gave away about **{you_tot:.0f} expected points** on the played-out valuation ({you_model:.0f} on the "
        f"model valuation), i.e. {you_tot / n_you:.3f} per resignation; the opponents' {n_them} resignations handed you about {them_tot:.0f} ({them_model:.0f} model), "
        f"{them_tot / n_them:.3f} per resignation. Net, resignations are worth {them_tot - you_tot:+.0f} points to you over {sum(n_games.values()):,} games."
    )
    return rate_table, summary, {"emp": emp, "comps": comps, "rates": rates}


def figure_bins(report: Report, rate_table: pd.DataFrame, res: dict[str, pd.DataFrame]) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(12, 6.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    x = np.arange(len(EV_LABELS))
    centres = [(EV_BINS[i] + min(EV_BINS[i + 1], 1.0)) / 2 for i in range(len(EV_LABELS))]
    colours = {"player": "#1f6f8b", "opponent": "#c46a2d"}
    for j, tc in enumerate(TIME_CLASSES):
        ax = axes[0, j]
        ax.plot(x, centres, color="grey", ls="--", lw=1, label="model EV (bin centre)")
        for side in SIDES:
            rt = rate_table[(rate_table["time_class"] == tc) & (rate_table["side"] == side)].set_index("ev_bin").reindex(EV_LABELS)
            ok = rt["games"] >= MIN_GAMES
            ax.errorbar(x[ok.to_numpy()] + (0.08 if side == "opponent" else -0.08), rt["score"][ok], yerr=[(rt["score"] - rt["lo"])[ok], (rt["hi"] - rt["score"])[ok]], fmt="o", ms=4, color=colours[side], label=f"{SIDE_NAME[side]}: played-out score")
        ax.set_title(tc)
        ax.set_ylim(0, 1)
        if j == 0:
            ax.set_ylabel("observed score from the position")
            ax.legend(fontsize=8, loc="upper left")
        ax2 = axes[1, j]
        for k, side in enumerate(SIDES):
            counts = res[side][res[side]["time_class"] == tc]["ev_bin"].value_counts().reindex(EV_LABELS).fillna(0)
            ax2.bar(x + (0.2 if k else -0.2), counts.to_numpy(), width=0.4, color=colours[side], label=f"{SIDE_NAME[side]}: resignations")
        ax2.set_xticks(x)
        ax2.set_xticklabels(EV_LABELS, rotation=45, ha="right", fontsize=8)
        if j == 0:
            ax2.set_ylabel("resignations")
            ax2.legend(fontsize=8)
    fig.suptitle("Model EV at a position vs how played-out games from such positions ended, and where the resignations sit", fontsize=11, fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "played_out_vs_model", "Top: observed score (win 1, draw ½) of positions in played-out games, by the model's EV bin from the side's view, you vs opponents, with game-clustered 95% CIs; dashed = the model's own value. Bottom: number of resignations in each bin (yours and the opponents').")
    plt.close(fig)


def section_clock(report: Report, res: dict[str, pd.DataFrame], comps: dict[str, pd.DataFrame]) -> dict:
    report.heading("4. The clock: when a lost position is still worth playing on")
    report.text(
        "The same played-out scores, split by the **other side's** clock at the position (the player who is ahead has to finish the game before flagging). "
        "Bullet and blitz only (rapid positions are almost never under 30 s; rapid rows are the ≥30 s column of section 3). Resignation counts per cell show "
        "where each side actually resigned. Cells with fewer than "
        f"{MIN_GAMES} games are blank."
    )
    out = {}
    for tc in ("bullet", "blitz"):
        for side in SIDES:
            other = "opponent" if side == "player" else "player"
            c = comps[side][comps[side]["time_class"] == tc].copy()
            c["other_clock_bin"] = pd.cut(c[f"clock_{other}"], CLOCK_BINS, labels=CLOCK_LABELS, right=False)
            rt = bin_rates(c, f"ev_bin_{side}", f"score_{side}", extra=["other_clock_bin"]).rename(columns={f"ev_bin_{side}": "ev_bin"})
            r = res[side][res[side]["time_class"] == tc]
            rows = []
            for lab in EV_LABELS[:6]:
                row = {"model EV bin": lab}
                for cb in CLOCK_LABELS:
                    cell = rt[(rt["ev_bin"] == lab) & (rt["other_clock_bin"] == cb)]
                    n_res = int(((r["ev_bin"] == lab) & (r["clock_other_bin"] == cb)).sum())
                    if len(cell) and cell["games"].iloc[0] >= MIN_GAMES:
                        row[f"other side {cb}: score"] = fmt(cell["score"].iloc[0], cell["lo"].iloc[0], cell["hi"].iloc[0])
                    else:
                        row[f"other side {cb}: score"] = "–"
                    row[f"other side {cb}: resignations"] = n_res
                rows.append(row)
            report.text(f"**{tc}, {SIDE_POSS[side]} positions and resignations**")
            report.table(pd.DataFrame(rows))
            out[(tc, side)] = rt
    # Resignations by the resigner's own clock and the other side's clock, counts.
    rows = []
    for tc in ("bullet", "blitz"):
        for side in SIDES:
            r = res[side][res[side]["time_class"] == tc]
            row = {"time class": tc, "side": SIDE_NAME[side], "resignations": len(r)}
            for cb in CLOCK_LABELS:
                row[f"other side {cb}"] = f"{(r['clock_other_bin'] == cb).mean():.0%}"
            for cb in CLOCK_LABELS:
                row[f"resigner {cb}"] = f"{(r['clock_own_bin'] == cb).mean():.0%}"
            rows.append(row)
    report.text("Share of resignations by the other side's clock and by the resigner's own clock at the moment of resigning:")
    report.table(pd.DataFrame(rows))
    return out


def section_categories(report: Report, res: dict[str, pd.DataFrame], emp: dict, rates: dict[str, pd.DataFrame]) -> pd.DataFrame:
    report.heading("5. 'Had to go' vs 'gave up': what kind of resignations cost the points")
    report.text(
        "Each resignation is classed by the model P(win) from the resigner's view: **clearly lost** (< 0.05), **nearly lost** (0.05–0.2) and **still playable** (≥ 0.2). "
        "Within each class: the resigner's clock as a fraction of the base time, whether the resigner's last own move was quick (at or below their own median move time "
        "in that game), whether that last own move was a blunder (ep_loss ≥ 0.2: 'resigned on the spot after a blunder'), and, for your games only, whether it was the "
        "last game of your session (the direct 'had to go' marker; opponents' sessions are unknown). The cost column is the played-out valuation (section 3) summed over "
        "the resignations of the class. A 'had to go' resignation is defined as still playable + resigner's clock ≥ 25% of base + quick last own move."
    )
    for side in SIDES:
        r = res[side].copy()
        r["class"] = pd.cut(r["p_win"], [0, 0.05, 0.2, 1.01], labels=["clearly lost (<0.05)", "nearly lost (0.05–0.2)", "still playable (≥0.2)"], right=False)
        r["blunder_then_resign"] = r["last_own_ep_loss"] >= 0.2
        r["had_to_go"] = (r["class"] == "still playable (≥0.2)") & (r["clock_own_frac"] >= 0.25) & r["last_own_quick"].fillna(False)
        res[side] = r
    rows = []
    for side in SIDES:
        r = res[side]
        for cls in r["class"].cat.categories:
            part = r[r["class"] == cls]
            row = {"side": SIDE_NAME[side], "class": cls, "resignations": len(part), "share": f"{len(part) / len(r):.0%}"}
            # empirical cost: value each at its bin rate (per time class)
            cost = 0.0
            for tc in TIME_CLASSES:
                p = part[part["time_class"] == tc]
                rates_tc = emp[(tc, side)]["rates"]
                codes = p["ev_bin"].cat.codes.to_numpy()
                cost += float(rates_tc[codes[codes >= 0]].sum())
            row["played-out cost (points)"] = f"{cost:.1f}"
            est, lo, hi = boot_mean(part["clock_own_frac"].to_numpy())
            row["clock left (frac of base)"] = fmt(est, lo, hi, ".2f")
            est, lo, hi = boot_mean(part["last_own_quick"].astype(float).to_numpy())
            row["quick last move"] = fmt(est, lo, hi, ".2f")
            est, lo, hi = boot_mean(part["blunder_then_resign"].astype(float).to_numpy())
            row["blunder then resign"] = fmt(est, lo, hi, ".2f")
            if side == "player":
                est, lo, hi = boot_mean(part["last_in_session"].astype(float).to_numpy())
                row["last game of session"] = fmt(est, lo, hi, ".2f")
            else:
                row["last game of session"] = "n/a"
            rows.append(row)
    table = pd.DataFrame(rows)
    report.table(table)
    rp = res["player"]
    htg = rp[rp["had_to_go"]]
    others_playable = rp[(rp["class"] == "still playable (≥0.2)") & ~rp["had_to_go"]]
    est_a, lo_a, hi_a = boot_mean(htg["last_in_session"].astype(float).to_numpy())
    est_b, lo_b, hi_b = boot_mean(rp[rp["class"] != "still playable (≥0.2)"]["last_in_session"].astype(float).to_numpy())
    d = boot_mean_diff(htg["last_in_session"].astype(float).to_numpy(), rp[rp["class"] != "still playable (≥0.2)"]["last_in_session"].astype(float).to_numpy())
    base_rate = float(rp["last_in_session"].mean())
    cost_htg = sum(float(emp[(tc, "player")]["rates"][htg[htg["time_class"] == tc]["ev_bin"].cat.codes.to_numpy()].sum()) for tc in TIME_CLASSES)
    cost_playable = sum(float(emp[(tc, "player")]["rates"][rp[(rp["class"] == "still playable (≥0.2)") & (rp["time_class"] == tc)]["ev_bin"].cat.codes.to_numpy()].sum()) for tc in TIME_CLASSES)
    cost_all = sum(emp[(tc, "player")]["total"] for tc in TIME_CLASSES)
    report.text(
        f"**Your 'had to go' resignations** (still playable, ≥ 25% of the clock, quick last move): {len(htg)} of {len(rp)} ({len(htg) / len(rp):.0%}), worth "
        f"{cost_htg:.1f} points on the played-out valuation; {est_a:.0%} [{lo_a:.0%}, {hi_a:.0%}] of them were the last game of a session, against "
        f"{est_b:.0%} [{lo_b:.0%}, {hi_b:.0%}] of your clearly/nearly lost resignations (difference {d[0]:+.2f} [{d[1]:+.2f}, {d[2]:+.2f}]) and {base_rate:.0%} of all your resignations. "
        f"The other {len(others_playable)} still-playable resignations (low clock or a slow last move: a real decision to stop) cost {cost_playable - cost_htg:.1f} points. "
        f"All still-playable resignations together are {cost_playable / cost_all:.0%} of the {cost_all:.0f} points your resignations cost; the clearly lost ones are "
        f"{sum(float(emp[(tc, 'player')]['rates'][rp[(rp['class'] == 'clearly lost (<0.05)') & (rp['time_class'] == tc)]['ev_bin'].cat.codes.to_numpy()].sum()) for tc in TIME_CLASSES) / cost_all:.0%}."
    )
    # blunder-then-resign detail
    rows = []
    for side in SIDES:
        r = res[side]
        for tc in TIME_CLASSES:
            p = r[r["time_class"] == tc]
            est, lo, hi = boot_mean(p["blunder_then_resign"].astype(float).to_numpy())
            b = p[p["blunder_then_resign"]]
            rows.append(
                {
                    "time class": tc,
                    "side": SIDE_NAME[side],
                    "resignations": len(p),
                    "last own move lost ≥ 0.2 ep": fmt(est, lo, hi, ".2f"),
                    "their median EV at resignation": f"{b['ev'].median():.3f}" if len(b) >= MIN_GAMES else "–",
                    "others' median EV": f"{p[~p['blunder_then_resign']]['ev'].median():.3f}",
                    "median ply": f"{p['ply'].median():.0f}",
                }
            )
    report.text(
        "'Resigned on the spot after a blunder' (the resigner's last own move cost ≥ 0.2 expected points, i.e. the position was decided by that move and the resignation "
        "followed at once or after the reply), by time class:"
    )
    report.table(pd.DataFrame(rows))
    return table


def section_cp(report: Report, res: dict[str, pd.DataFrame], comps: dict[str, pd.DataFrame]) -> dict:
    report.heading("6. Rule of thumb in engine terms")
    report.text(
        "The model EV is not visible at the board; the engine eval (or material) is closer to what a player feels. Played-out score by the engine eval from the "
        "resigner's side (cp after the last move, mates counted as ≤ −2000), per time class, you vs opponents, with the number of resignations per cell. "
        f"Cells with fewer than {MIN_GAMES} games are blank. For bullet, the score is also split by the other side's clock."
    )
    cp_rates = {}
    for tc in TIME_CLASSES:
        rows = []
        rt = {side: bin_rates(comps[side][comps[side]["time_class"] == tc], f"cp_bin_{side}", f"score_{side}").rename(columns={f"cp_bin_{side}": "cp_bin"}) for side in SIDES}
        cp_rates[tc] = rt["player"].set_index("cp_bin")
        for lab in CP_LABELS:
            row = {"eval (resigner's side)": lab}
            for side in SIDES:
                cell = rt[side][rt[side]["cp_bin"] == lab]
                r = res[side][(res[side]["time_class"] == tc) & (res[side]["cp_bin"] == lab)]
                row[f"{SIDE_NAME[side]}: played-out score"] = fmt(cell["score"].iloc[0], cell["lo"].iloc[0], cell["hi"].iloc[0]) if len(cell) and cell["games"].iloc[0] >= MIN_GAMES else "–"
                row[f"{SIDE_NAME[side]}: positions (games)"] = f"{int(cell['positions'].iloc[0]):,} ({int(cell['games'].iloc[0])})" if len(cell) else "0"
                row[f"{SIDE_NAME[side]}: resignations"] = len(r)
            rows.append(row)
        report.text(f"**{tc}**")
        report.table(pd.DataFrame(rows))
    # bullet split by other side's clock, your positions
    c = comps["player"][comps["player"]["time_class"] == "bullet"].copy()
    c["other_clock_bin"] = pd.cut(c["clock_opponent"], CLOCK_BINS, labels=CLOCK_LABELS, right=False)
    rt = bin_rates(c, "cp_bin_player", "score_player", extra=["other_clock_bin"]).rename(columns={"cp_bin_player": "cp_bin"})
    rows = []
    for lab in CP_LABELS:
        row = {"eval (your side)": lab}
        for cb in CLOCK_LABELS:
            cell = rt[(rt["cp_bin"] == lab) & (rt["other_clock_bin"] == cb)]
            row[f"opponent {cb}"] = fmt(cell["score"].iloc[0], cell["lo"].iloc[0], cell["hi"].iloc[0]) if len(cell) and cell["games"].iloc[0] >= MIN_GAMES else "–"
            row[f"opponent {cb}: games"] = int(cell["games"].iloc[0]) if len(cell) else 0
        rows.append(row)
    report.text("**bullet, your positions, by the opponent's clock** (played-out score):")
    report.table(pd.DataFrame(rows))
    cp_rates["bullet_by_clock"] = rt.set_index(["other_clock_bin", "cp_bin"])
    return cp_rates


def figure_cp(report: Report, comps: dict[str, pd.DataFrame], res: dict[str, pd.DataFrame]) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharey=True)
    x = np.arange(len(CP_LABELS))
    colours = {"player": "#1f6f8b", "opponent": "#c46a2d"}
    for j, tc in enumerate(TIME_CLASSES):
        ax = axes[j]
        for k, side in enumerate(SIDES):
            rt = bin_rates(comps[side][comps[side]["time_class"] == tc], f"cp_bin_{side}", f"score_{side}").rename(columns={f"cp_bin_{side}": "cp_bin"}).set_index("cp_bin").reindex(CP_LABELS)
            ok = (rt["games"] >= MIN_GAMES).to_numpy()
            ax.errorbar(x[ok] + (0.1 if k else -0.1), rt["score"][ok], yerr=[(rt["score"] - rt["lo"])[ok], (rt["hi"] - rt["score"])[ok]], fmt="o-", ms=4, lw=1, color=colours[side], label=f"{SIDE_NAME[side]}: played-out score")
        if tc == "bullet":
            c = comps["player"][comps["player"]["time_class"] == tc].copy()
            c = c[c["clock_opponent"] < 10]
            rt = bin_rates(c, "cp_bin_player", "score_player").rename(columns={"cp_bin_player": "cp_bin"}).set_index("cp_bin").reindex(CP_LABELS)
            ok = (rt["games"] >= MIN_GAMES).to_numpy()
            ax.plot(x[ok], rt["score"][ok], "s--", ms=4, lw=1, color="#4a9d5b", label="you, opponent < 10 s")
        ax.set_title(tc)
        ax.set_xticks(x)
        ax.set_xticklabels(CP_LABELS, rotation=45, ha="right", fontsize=8)
        ax.set_ylim(0, 1)
        if j == 0:
            ax.set_ylabel("observed score from the position")
        ax.legend(fontsize=8, loc="upper left")
    fig.suptitle("Played-out score by engine eval from the side under study's view", fontsize=11, fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "played_out_by_cp", "Observed score of positions in played-out games by the engine eval from the side under study's view, you vs opponents, per time class (game-clustered 95% CIs); bullet also shows your positions when the opponent was under 10 s.")
    plt.close(fig)


def rules_of_thumb(report: Report, rate_table: pd.DataFrame, clock_rates: dict, emp: dict) -> dict:
    report.heading("7. Rule of thumb")
    report.text(
        "Reading the played-out tables for your own positions: the highest model-EV bin in which positions were still worth less than 0.02 and 0.05 points "
        "(upper CI bound), i.e. where resigning costs at most that much, per time class and (bullet/blitz) by the opponent's clock. 'none' means no bin with enough "
        "games was that hopeless."
    )
    rows, out = [], {}
    for tc in TIME_CLASSES:
        rt = rate_table[(rate_table["time_class"] == tc) & (rate_table["side"] == "player")]
        row = {"time class": tc, "opponent's clock": "any", "resign costs ≤ 0.02 below EV": rule_threshold(rt, 0.02), "≤ 0.05 below EV": rule_threshold(rt, 0.05)}
        rows.append(row)
        out[(tc, "any")] = row
        if tc in ("bullet", "blitz"):
            crt = clock_rates[(tc, "player")]
            for cb in CLOCK_LABELS:
                part = crt[crt["other_clock_bin"] == cb]
                row = {"time class": tc, "opponent's clock": cb, "resign costs ≤ 0.02 below EV": rule_threshold(part, 0.02), "≤ 0.05 below EV": rule_threshold(part, 0.05)}
                rows.append(row)
                out[(tc, cb)] = row
    report.table(pd.DataFrame(rows))
    return out


def main() -> None:
    t0 = time.time()
    figure_style()
    data = load_data()
    games = data.games.merge(pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet"), on="uuid", how="left")
    view = side_view(data.moves)
    last = last_positions(view)
    own = last_own_moves(view)
    res = {side: resignations(games, last, own, side) for side in SIDES}
    n_games = {tc: int((games["time_class"] == tc).sum()) for tc in TIME_CLASSES}
    no_moves = int((~games["uuid"].isin(last.index)).sum())
    n_res_all = {side: int((games[SIDES[side][0]] == "resigned").sum()) for side in SIDES}

    report = Report(SLUG, "Expected value of not resigning")
    report.text(
        f"Data: {len(games):,} standard live games ({n_games['bullet']:,} bullet, {n_games['blitz']:,} blitz, {n_games['rapid']:,} rapid) and {len(view):,} analysed moves, "
        f"player {data.username}. You resigned {n_res_all['player']} of them and your opponents resigned {n_res_all['opponent']}; {no_moves} game(s) ended before the first move "
        f"and are excluded from position-based numbers (that leaves {len(res['player'])} of your resignations and {len(res['opponent'])} of theirs). The resignation position is "
        f"the position after the last move of the game; P(win), P(draw) and EV = P(win) + ½P(draw) are the calibrated model's values from the resigner's view "
        f"({calibration_brier()}). Numbers are estimate [95% CI], bootstrapped over games ({N_BOOT} resamples; position-level rates clustered by game). "
        f"Every number about you is paired with the opponents' number from the same games. Per-game Q1 columns are joined from `games_decomposed.parquet`."
    )

    freq = section_frequency(report, games, res)
    model_table = section_model_ev(report, res)
    mt = model_table.copy()
    mt["mean EV / resignation"] = mt.apply(lambda r: fmt(r["mean EV"], r["lo"], r["hi"]), axis=1)
    mt["total points"] = mt.apply(lambda r: fmt(r["total"], r["total_lo"], r["total_hi"], ".1f"), axis=1)
    mt["per 100 games"] = mt.apply(lambda r: f"{100 * r['total'] / n_games[r['time class']]:.2f}", axis=1)
    mt["≈ Elo"] = mt.apply(lambda r: f"{r['total'] / n_games[r['time class']] / ELO_SLOPE:.0f}", axis=1)
    report.table(mt[["time class", "side", "resignations", "mean EV / resignation", "total points", "per 100 games", "≈ Elo"]])

    rate_table, summary, aux = section_empirical(report, view, res, model_table, n_games)
    figure_bins(report, rate_table, res)
    clock_rates = section_clock(report, res, aux["comps"])
    cats = section_categories(report, res, aux["emp"], aux["rates"])
    cp_rates = section_cp(report, res, aux["comps"])
    figure_cp(report, aux["comps"], res)
    rules = rules_of_thumb(report, rate_table, clock_rates, aux["emp"])

    # --- closing sections, computed from the numbers above
    emp = aux["emp"]
    you_tot = sum(emp[(tc, "player")]["total"] for tc in TIME_CLASSES)
    them_tot = sum(emp[(tc, "opponent")]["total"] for tc in TIME_CLASSES)
    n_all = sum(n_games.values())
    per100_you = 100 * you_tot / n_all
    rp = res["player"]
    playable_share = (rp["p_win"] >= 0.2).mean()
    playable_cost = sum(float(emp[(tc, "player")]["rates"][rp[(rp["p_win"] >= 0.2) & (rp["time_class"] == tc)]["ev_bin"].cat.codes.to_numpy()].sum()) for tc in TIME_CLASSES)
    lost_share = (rp["p_win"] < 0.05).mean()
    lost_cost = sum(float(emp[(tc, "player")]["rates"][rp[(rp["p_win"] < 0.05) & (rp["time_class"] == tc)]["ev_bin"].cat.codes.to_numpy()].sum()) for tc in TIME_CLASSES)
    bullet_row = summary[(summary["time class"] == "bullet") & (summary["side"] == "you")].iloc[0]
    blitz_row = summary[(summary["time class"] == "blitz") & (summary["side"] == "you")].iloc[0]
    rapid_row = summary[(summary["time class"] == "rapid") & (summary["side"] == "you")].iloc[0]
    rule_b = rules[("bullet", "<10 s")]
    rule_b_any = rules[("bullet", "any")]
    rule_bl = rules[("blitz", "any")]
    rule_r = rules[("rapid", "any")]
    htg = rp[rp["had_to_go"]]
    cost_htg = sum(float(emp[(tc, "player")]["rates"][htg[htg["time_class"] == tc]["ev_bin"].cat.codes.to_numpy()].sum()) for tc in TIME_CLASSES)
    opp_share = {tc: freq[(tc, "opponent", "playable_share")] for tc in TIME_CLASSES}
    you_share = {tc: freq[(tc, "player", "playable_share")] for tc in TIME_CLASSES}

    report.heading("What this means for the player")
    net = them_tot - you_tot
    you_model = model_table[model_table["side"] == "you"]["total"].sum()
    per100 = {tc: 100 * emp[(tc, "player")]["total"] / n_games[tc] for tc in TIME_CLASSES}
    worst_tc = max(TIME_CLASSES, key=lambda tc: per100[tc])
    tc_costs = "; ".join(f"{tc} {per100[tc]:.1f} per 100 games (≈ {per100[tc] / 100 / ELO_SLOPE:.0f} Elo)" for tc in TIME_CLASSES)
    sig = [tc for tc in TIME_CLASSES if freq[(tc, "diff")][1] > 0 or freq[(tc, "diff")][2] < 0]
    if sig:
        share_txt = "; ".join(
            f"in {tc} you resign {'more' if freq[(tc, 'diff')][0] > 0 else 'less'} of your losses than opponents ({freq[(tc, 'player', 'resign_share')]:.2f} vs {freq[(tc, 'opponent', 'resign_share')]:.2f}, "
            f"difference {fmt(*freq[(tc, 'diff')])})"
            for tc in sig
        )
        share_txt += "; elsewhere the shares are equal within the CI"
    else:
        share_txt = "you resign about the same share of your losses as opponents in every time class"
    cpr = cp_rates
    worst_bin = CP_LABELS[0]
    worst_scores = " / ".join(f"{cpr[tc].loc[worst_bin, 'score']:.3f}" if worst_bin in cpr[tc].index and cpr[tc].loc[worst_bin, "games"] >= MIN_GAMES else "–" for tc in TIME_CLASSES)
    rook_bin = CP_LABELS[2]
    rook_scores = " / ".join(f"{cpr[tc].loc[rook_bin, 'score']:.2f}" if rook_bin in cpr[tc].index and cpr[tc].loc[rook_bin, "games"] >= MIN_GAMES else "–" for tc in TIME_CLASSES)
    bc = cpr["bullet_by_clock"]
    mate_short = bc.loc[("<10 s", worst_bin), "score"] if ("<10 s", worst_bin) in bc.index else float("nan")
    mate_long = bc.loc[("≥30 s", worst_bin), "score"] if ("≥30 s", worst_bin) in bc.index else float("nan")
    report.text(
        f"Resigning has cost you about {you_tot:.0f} expected points over {len(rp)} resignations ({you_tot / len(rp):.2f} per resignation, {per100_you:.1f} per 100 games, "
        f"roughly {per100_you / 100 / ELO_SLOPE:.0f} Elo of expected score) on the played-out valuation (lower bound {sum(emp[(tc, 'player')]['all_total'] for tc in TIME_CLASSES):.0f} "
        f"from all positions, model view {you_model:.0f}); your opponents' resignations handed you about {them_tot:.0f}, so the exchange is a net "
        f"{'cost' if net < 0 else 'gain'} of {abs(net):.0f} points to you. The cost is uneven: {tc_costs}. {worst_tc.capitalize()} is where the habit costs most: "
        f"{share_txt}, and {you_share[worst_tc]:.0%} of your {worst_tc} resignations came from positions the model still rated ≥ 0.2 P(win), against "
        f"{opp_share[worst_tc]:.0%} of the opponents'. Over all time classes the {playable_share:.0%} of your resignations from such still-playable positions carry "
        f"{playable_cost / you_tot:.0%} of the cost ({playable_cost:.0f} points) and the {lost_share:.0%} from clearly lost positions (P(win) < 0.05) carry {lost_cost / you_tot:.0%}: "
        f"resigning dead positions is harmless, the question is only where the line is. From the played-out tables (sections 4, 6 and 7): in **bullet** the opponent's clock "
        f"decides; with the opponent under 10 s even positions with mate against you or ≤ −2000 scored {mate_short:.2f} when played out (vs {mate_long:.2f} with the opponent "
        f"at ≥ 30 s), so never resign a bullet game while the opponent is short of time; with the opponent at ≥ 30 s, resigning costs ≤ 0.05 only below model EV "
        f"{rules[('bullet', '≥30 s')]['≤ 0.05 below EV']}. In **blitz** resigning costs ≤ 0.05 only below model EV {rule_bl['≤ 0.05 below EV']}, in **rapid** below "
        f"{rule_r['≤ 0.05 below EV']} (bins of P(win) + ½P(draw)). In engine terms no band is free: positions with mate against or ≤ −2000 scored {worst_scores} "
        f"(bullet / blitz / rapid) when you played them out, and −1000..−500 (roughly a rook down) scored {rook_scores}; so the practical rule is to resign only with mate or a "
        f"hopeless material deficit on the board **and** an opponent with plenty of time, and to treat 'down a piece or a rook' as a position to play on in every time class. "
        f"The {len(htg)} 'had to go' resignations (playable, plenty of clock, quick last move; {cost_htg:.0f} points) are the group you can act on without changing how you "
        f"judge positions: if they were real interruptions there is nothing to fix; if they were impatience, that is where the points are."
    )

    report.heading("Caveats")
    report.text(
        "- The model P(win) is the pooled calibrated model (" + calibration_brier() + "): under-confident at the extremes, model-relative, and fitted on all games including the "
        "resigned ones (positions in resigned games enter its training with the resigner's loss), so the section 2 totals are a model view only; section 3's played-out "
        "comparison is the robust estimate and is what the conclusions use.\n"
        "- The played-out comparison set is positions in games the side under study did not resign or abandon. Those games are themselves selected (a player who did not resign "
        "from a hopeless position may have had a reason: the opponent's clock, a trick), so the bin rates are an upper-ish bound on what resigned positions would have scored; "
        "the model view is the lower-ish bound. Positions are weighted per position, not per game: long slides count more; CIs are clustered by game.\n"
        "- Opponents who are ahead may play differently against someone who does not resign (the plan's caveat); the comparison set cannot separate that.\n"
        "- The resignation position is the position after the last recorded move; the time between that move and the resignation is not recorded (wall-clock game length "
        "minus clock time used is dominated by lag and is not usable), so 'quick last move' is the resigner's last own move, not the resignation itself.\n"
        "- 'Had to go' is a proxy (still playable, ≥ 25% of the clock left, quick last own move); the last-game-of-session marker only exists for you, and a session that ends "
        "because you resigned and quit in frustration looks the same as one ended by an interruption.\n"
        f"- Cells with fewer than {MIN_GAMES} games are blanked; rapid has {n_games['rapid']} games and {len(res['player'][res['player']['time_class'] == 'rapid'])} of your "
        "resignations, so its bin rates are wide. Bootstrap CIs use "
        f"{N_BOOT} resamples.\n"
        "- The Elo conversion uses the Elo slope at equal ratings (ln 10 / 1600 points per rating point) applied to the per-game cost; it is an order-of-magnitude translation, "
        "not a rating prediction.\n"
        f"- {no_moves} game(s) with no moves are excluded; abandoned games are treated as not played out but are not counted as resignations."
    )
    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
