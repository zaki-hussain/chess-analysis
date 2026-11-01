"""Q10. Endgame technique: converting tablebase wins, holding tablebase draws,
and which endgame types go wrong.

Two layers:
1. Tablebase range (<= 5 pieces): exact Syzygy outcome before and after every
   move (`tb_wdl_before/after`, `tb_error`, `tb_dtz_*`). Game-level
   conversion/hold rates from the position that entered tablebase range, and
   per-move error rates by material type, clock and DTZ.
2. Beyond 5 pieces: `ep_loss` (and `cp_loss`) in the endgame phase by a
   coarse material class, player vs opponents in the same games.

Run from the repo root: .venv/bin/python analysis/10_endgames.py
"""

from __future__ import annotations

import re
import time

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
)

SLUG = "10_endgames"
SEED = 10
N_BOOT = 1000  # cheap: the fast path resamples per-game sums
N_BOOT_CELLS = 500

CLOCK_BINS = [0, 5, 15, 60, 10_000]
CLOCK_LABELS = ["<5s", "5-15s", "15-60s", ">=60s"]
DTZ_BINS = [1, 3, 6, 11, 21, 1000]
DTZ_LABELS = ["1-2", "3-5", "6-10", "11-20", "21+"]

# Coarse material classes for the <= 5 piece layer (mover's view does not matter:
# the class describes the whole board).
TB_CLASS_ORDER = [
    "pawn ending", "rook ending", "rook vs pawns/bare king", "queen ending",
    "queen vs pawns/bare king", "queen vs rook/minor", "rook vs minor",
    "minor vs minor", "minor vs pawns/bare king",
]  # fmt: skip
# Coarse material classes for the > 5 piece endgame layer (piece *types* on the board).
BIG_CLASS_ORDER = ["pawn ending", "rook ending", "minor piece ending", "rook + minor", "queen ending", "queen + others"]


# --- helpers -----------------------------------------------------------------


def piece_counts(fen: pd.Series) -> pd.DataFrame:
    """Vectorised counts of every piece letter in the board part of a FEN."""
    board = fen.str.split(" ").str[0]
    out = pd.DataFrame(index=fen.index)
    for letter in "QRBNPqrbnp":
        out[letter] = board.str.count(letter)
    out["total"] = out[list("QRBNPqrbnp")].sum(axis=1) + 2
    return out


def tb_material_class(pc: pd.DataFrame) -> pd.Series:
    """Coarse class of a <= 5 piece position from the piece sets of both sides."""
    wq, bq = pc["Q"] > 0, pc["q"] > 0
    wr, br = pc["R"] > 0, pc["r"] > 0
    wm, bm = (pc["B"] + pc["N"]) > 0, (pc["b"] + pc["n"]) > 0
    conditions = [
        ~(wq | bq | wr | br | wm | bm),
        wq & bq,
        (wq & (br | bm)) | (bq & (wr | wm)),
        wq | bq,
        wr & br,
        (wr & bm) | (br & wm),
        wr | br,
        wm & bm,
    ]
    choices = [
        "pawn ending", "queen ending", "queen vs rook/minor", "queen vs pawns/bare king",
        "rook ending", "rook vs minor", "rook vs pawns/bare king", "minor vs minor",
    ]  # fmt: skip
    return pd.Series(np.select(conditions, choices, default="minor vs pawns/bare king"), index=pc.index)


def big_material_class(pc: pd.DataFrame) -> pd.Series:
    """Coarse class of a > 5 piece endgame position from the piece types present."""
    has_q = (pc["Q"] + pc["q"]) > 0
    has_r = (pc["R"] + pc["r"]) > 0
    has_m = (pc["B"] + pc["N"] + pc["b"] + pc["n"]) > 0
    conditions = [~(has_q | has_r | has_m), has_q & (has_r | has_m), has_q, has_r & has_m, has_r]
    choices = ["pawn ending", "queen + others", "queen ending", "rook + minor", "rook ending"]
    return pd.Series(np.select(conditions, choices, default="minor piece ending"), index=pc.index)


def canonical_material(pc: pd.DataFrame, color: pd.Series) -> pd.Series:
    """'KRPvKR'-style string from the mover's view."""
    white = pd.Series([""] * len(pc), index=pc.index, dtype=object)
    black = pd.Series([""] * len(pc), index=pc.index, dtype=object)
    for letter in "QRBNP":
        white = white + pc[letter].astype(int).map(lambda k, letter=letter: letter * k)
        black = black + pc[letter.lower()].astype(int).map(lambda k, letter=letter: letter * k)
    is_white = (color == "white").to_numpy()
    mover = np.where(is_white, white.to_numpy(), black.to_numpy())
    other = np.where(is_white, black.to_numpy(), white.to_numpy())
    return pd.Series(["K" + a + "vK" + b for a, b in zip(mover, other)], index=pc.index)


def termination_reason(text: pd.Series) -> pd.Series:
    """'won on time' -> 'time', 'drawn by stalemate' -> 'stalemate', ..."""
    patterns = [
        ("on time", "timeout"), ("timeout vs insufficient", "timeout vs insufficient"), ("checkmate", "checkmate"),
        ("resignation", "resignation"), ("stalemate", "stalemate"), ("repetition", "repetition"),
        ("insufficient", "insufficient material"), ("agreement", "agreement"), ("50-move", "50-move rule"),
        ("abandon", "abandoned"),
    ]  # fmt: skip
    out = pd.Series("other", index=text.index)
    for needle, label in reversed(patterns):  # earlier patterns win
        out = out.where(~text.str.contains(re.escape(needle), case=False, na=False), label)
    return out


def rate_ci(flags: pd.Series, games: pd.Series, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    return cluster_bootstrap_mean(flags.astype("float64"), games, n_boot=n_boot, seed=seed)


def rate_difference(a: pd.Series, a_games: pd.Series, b: pd.Series, b_games: pd.Series, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """a - b for two rates measured on *different* games (independent game bootstraps)."""
    diff = float(a.mean() - b.mean()) if len(a) and len(b) else float("nan")
    boots = []
    for values, groups, s in ((a, a_games, seed), (b, b_games, seed + 1)):
        frame = pd.DataFrame({"v": values.astype("float64"), "g": groups.to_numpy()}).dropna()
        agg = frame.groupby("g")["v"].agg(["sum", "count"])
        if len(agg) < 2:
            return diff, float("nan"), float("nan")
        rng = np.random.default_rng(s)
        idx = rng.integers(0, len(agg), size=(n_boot, len(agg)))
        boots.append(agg["sum"].to_numpy()[idx].sum(axis=1) / agg["count"].to_numpy()[idx].sum(axis=1))
    lo, hi = np.quantile(boots[0] - boots[1], [0.025, 0.975])
    return diff, float(lo), float(hi)


def rate_table(frame: pd.DataFrame, flag: str, by: list[str], n_boot: int = N_BOOT_CELLS) -> pd.DataFrame:
    """Player vs opponent rate of `flag` per `by` cell, each with a game-clustered CI; cells are move rows."""
    rows = []
    for key, part in frame.groupby(by, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        mine, theirs = part[part["is_player_move"]], part[~part["is_player_move"]]
        p = rate_ci(mine[flag], mine["game_uuid"], n_boot=n_boot)
        o = rate_ci(theirs[flag], theirs["game_uuid"], n_boot=n_boot)
        rows.append(
            {
                **dict(zip(by, key)),
                "player": p[0], "player_lo": p[1], "player_hi": p[2], "player_n": len(mine), "player_errors": int(mine[flag].sum()),
                "opponent": o[0], "opponent_lo": o[1], "opponent_hi": o[2], "opponent_n": len(theirs), "opponent_errors": int(theirs[flag].sum()),
                "games": part["game_uuid"].nunique(),
            }  # fmt: skip
        )
    return pd.DataFrame(rows)


def pct(frame: pd.DataFrame, mean: str, lo: str, hi: str) -> pd.Series:
    scaled = frame[[mean, lo, hi]] * 100
    scaled.columns = ["m", "l", "h"]
    return ci_column(scaled, "m", "l", "h", fmt=".1f")


def dot_ci(ax, table: pd.DataFrame, labels: list[str], scale: float = 100, xlabel: str = "") -> None:
    """Horizontal dot-and-interval chart, player vs opponent, one row per label."""
    y = np.arange(len(labels))
    for offset, side in ((0.15, "player"), (-0.15, "opponent")):
        est = table[side].to_numpy() * scale
        lo = table[f"{side}_lo"].to_numpy() * scale
        hi = table[f"{side}_hi"].to_numpy() * scale
        ax.errorbar(est, y + offset, xerr=[est - lo, hi - est], fmt="o", ms=6, lw=1.5, capsize=2, color=SIDE_COLOURS[side], label="you" if side == "player" else "opponents")
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel(xlabel)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2)


# --- main ----------------------------------------------------------------------


def main() -> None:
    started = time.time()
    figure_style()
    import matplotlib.pyplot as plt

    data = load_data()
    moves = data.moves
    pc = piece_counts(moves["fen_before"])
    moves = moves.assign(n_pieces=pc["total"], tb_class=tb_material_class(pc), big_class=big_material_class(pc))
    moves["termination_reason"] = termination_reason(moves["termination"])
    moves["opponent_name"] = moves["opponent"]
    moves["time_class"] = pd.Categorical(moves["time_class"], TIME_CLASSES)

    tb = moves[moves["tb_wdl_before"].notna()].copy()
    tb["tb_wdl_before"] = tb["tb_wdl_before"].astype(int)
    tb["tb_wdl_after"] = tb["tb_wdl_after"].astype(int)
    tb["tb_error"] = tb["tb_error"].astype(bool)
    tb["abs_dtz"] = tb["tb_dtz_before"].astype(float).abs()
    tb["material"] = canonical_material(pc.loc[tb.index], tb["color"])
    tb["clock_bin"] = pd.cut(tb["clock_before_seconds"], CLOCK_BINS, labels=CLOCK_LABELS, right=False)
    # Error kinds from the mover's view.
    tb["err_win_draw"] = (tb["tb_wdl_before"] == 2) & (tb["tb_wdl_after"] == 0)
    tb["err_win_loss"] = (tb["tb_wdl_before"] == 2) & (tb["tb_wdl_after"] == -2)
    tb["err_draw_loss"] = (tb["tb_wdl_before"] == 0) & (tb["tb_wdl_after"] == -2)
    # Only won or drawn positions can be thrown away: that is the denominator for error rates.
    at_risk = tb[tb["tb_wdl_before"] >= 0].copy()
    won = tb[tb["tb_wdl_before"] == 2].copy()
    drawn = tb[tb["tb_wdl_before"] == 0].copy()

    big = moves[(moves["phase"] == "endgame") & (moves["n_pieces"] > 5)].copy()

    report = Report(SLUG, "Q10. Endgame technique: converting wins, holding draws, and which endgames go wrong")
    report.text(
        f"Data: {len(data.games):,} standard live games, {len(moves):,} analysed moves. "
        f"**Tablebase layer:** {len(tb):,} moves in {tb['game_uuid'].nunique():,} games reached a position with at most 5 pieces, "
        f"where Syzygy gives the exact result before and after every move ({int(tb['tb_error'].sum())} tablebase errors in total, "
        f"{int(tb.loc[tb['is_player_move'], 'tb_error'].sum())} by you and {int(tb.loc[~tb['is_player_move'], 'tb_error'].sum())} by opponents). "
        f"**Beyond 5 pieces:** {len(big):,} endgame-phase moves in {big['game_uuid'].nunique():,} games, judged by calibrated expected points lost (`ep_loss`) "
        f"and centipawn loss. Every estimate is shown as estimate [95% CI], bootstrapped by game; every number about you is paired with the opponents' number from the same games."
    )

    # --- 1. Game-level conversion and hold ------------------------------------------------
    report.heading("1. Did you convert tablebase wins and hold tablebase draws?")
    first = tb.sort_values(["game_uuid", "ply"]).groupby("game_uuid").first().reset_index()
    # Once a game is in tablebase range it stays there (captures only remove pieces), so the
    # position that entered the range fixes the theoretical result for the rest of the game.
    first["wdl_player"] = np.where(first["is_player_move"], first["tb_wdl_before"], -first["tb_wdl_before"])
    first["player_won"] = first["outcome"] == "win"
    first["player_lost"] = first["outcome"] == "loss"
    first["player_not_lost"] = first["outcome"] != "loss"
    first["player_not_won"] = first["outcome"] != "win"
    first["time_class"] = pd.Categorical(first["time_class"], TIME_CLASSES)

    report.text(
        "For each game, the first position with at most 5 pieces fixes the theoretical result for the rest of the game "
        "(pieces can only leave the board). A **conversion** is a tablebase win that ended as a win on the scoreboard; "
        "a **hold** is a tablebase draw that did not end as a loss. Your conversion rate is compared with the opponents' conversion rate "
        "in the games where *they* had the tablebase win against you, and likewise for holds. "
        f"Entry states from your side: won {int((first['wdl_player'] == 2).sum())} games, drawn {int((first['wdl_player'] == 0).sum())}, lost {int((first['wdl_player'] == -2).sum())}."
    )

    def conversion_rows(frame: pd.DataFrame, label: str) -> dict:
        p_win = frame[frame["wdl_player"] == 2]
        o_win = frame[frame["wdl_player"] == -2]
        p_draw = frame[frame["wdl_player"] == 0]
        p = rate_ci(p_win["player_won"], p_win["game_uuid"])
        o = rate_ci(o_win["player_lost"], o_win["game_uuid"])
        d = rate_difference(p_win["player_won"], p_win["game_uuid"], o_win["player_lost"], o_win["game_uuid"])
        hp = rate_ci(p_draw["player_not_lost"], p_draw["game_uuid"])
        ho = rate_ci(p_draw["player_not_won"], p_draw["game_uuid"])
        return {
            "time_class": label,
            "conv_player": p[0], "conv_player_lo": p[1], "conv_player_hi": p[2], "n_won": len(p_win),
            "conv_opp": o[0], "conv_opp_lo": o[1], "conv_opp_hi": o[2], "n_opp_won": len(o_win),
            "conv_diff": d[0], "conv_diff_lo": d[1], "conv_diff_hi": d[2],
            "hold_player": hp[0], "hold_player_lo": hp[1], "hold_player_hi": hp[2],
            "hold_opp": ho[0], "hold_opp_lo": ho[1], "hold_opp_hi": ho[2], "n_drawn": len(p_draw),
        }  # fmt: skip

    conv = pd.DataFrame([conversion_rows(first, "all")] + [conversion_rows(first[first["time_class"] == tc], tc) for tc in TIME_CLASSES])
    conv_out = pd.DataFrame(
        {
            "time class": conv["time_class"],
            "your conversion % (n)": pct(conv, "conv_player", "conv_player_lo", "conv_player_hi") + " (" + conv["n_won"].astype(str) + ")",
            "opponents' conversion % (n)": pct(conv, "conv_opp", "conv_opp_lo", "conv_opp_hi") + " (" + conv["n_opp_won"].astype(str) + ")",
            "difference (pts)": pct(conv, "conv_diff", "conv_diff_lo", "conv_diff_hi"),
            "your hold % (n drawn)": pct(conv, "hold_player", "hold_player_lo", "hold_player_hi") + " (" + conv["n_drawn"].astype(str) + ")",
            "opponents' hold %": pct(conv, "hold_opp", "hold_opp_lo", "hold_opp_hi"),
        }
    )
    report.table(conv_out)
    report.text(
        "Hold rates use the same drawn games for both sides (a draw you failed to hold is a draw they converted), so they are not independent of each other: "
        "both are shown because a drawn tablebase position that ends decisively is somebody's failure and somebody's swindle."
    )

    fig, ax = plt.subplots(figsize=(7, 3.6))
    plot = conv[conv["time_class"] != "all"].rename(columns={"conv_player": "player", "conv_player_lo": "player_lo", "conv_player_hi": "player_hi", "conv_opp": "opponent", "conv_opp_lo": "opponent_lo", "conv_opp_hi": "opponent_hi"})
    dot_ci(ax, plot, TIME_CLASSES, xlabel="games won after entering a tablebase-won position, %")
    ax.set_title("Converting tablebase wins, by time class")
    ax.set_xlim(40, 100)
    report.figure(fig, "conversion", "Share of tablebase-won endings (<= 5 pieces) that ended as a win, you vs opponents, with 95% game-bootstrap CIs.")
    plt.close(fig)

    # How the failed conversions happened.
    report.heading("How the failed conversions and failed holds ended", level=3)
    tb_sorted = tb.sort_values(["game_uuid", "ply"])
    my_err_games = set(tb_sorted.loc[tb_sorted["is_player_move"] & tb_sorted["tb_error"], "game_uuid"])
    their_err_games = set(tb_sorted.loc[~tb_sorted["is_player_move"] & tb_sorted["tb_error"], "game_uuid"])

    def failure_breakdown(frame: pd.DataFrame, side: str) -> pd.DataFrame:
        err_games = my_err_games if side == "player" else their_err_games
        frame = frame.assign(how=np.where(frame["game_uuid"].isin(err_games), "tablebase error by the winner", "no tablebase error: " + frame["termination_reason"]))
        who = "you" if side == "player" else "opponents"
        counts = frame.groupby(["how", "time_class"], observed=False).size().unstack("time_class").fillna(0).astype(int)
        counts.columns = [f"{who} ({tc})" for tc in counts.columns]
        counts[f"{who} (all)"] = counts.sum(axis=1)
        return counts

    failed_p = first[(first["wdl_player"] == 2) & ~first["player_won"]]
    failed_o = first[(first["wdl_player"] == -2) & ~first["player_lost"]]
    report.text(
        f"Failed conversions: {len(failed_p)} of yours, {len(failed_o)} of the opponents'. 'Tablebase error' means the winning side made at least one move that "
        "changed the theoretical result; 'no tablebase error' means the win was never thrown away over the board and the game ended otherwise (timeout, repetition, agreement). "
        "'Timeout vs insufficient material' is the chess.com rule that a flag fall against a bare king (or king and minor piece) is a draw."
    )
    fail = pd.concat([failure_breakdown(failed_p, "player"), failure_breakdown(failed_o, "opponent")], axis=1).fillna(0).astype(int)
    fail = fail.sort_values("you (all)", ascending=False).rename_axis("how it ended").reset_index()
    report.table(fail)

    # Timeouts in won positions: flag separately because they are not tablebase errors.
    won_timeouts_p = int(((first["wdl_player"] == 2) & first["termination_reason"].isin(["timeout", "timeout vs insufficient"]) & ~first["player_won"]).sum())
    won_timeouts_o = int(((first["wdl_player"] == -2) & first["termination_reason"].isin(["timeout", "timeout vs insufficient"]) & ~first["player_lost"]).sum())
    report.text(f"Clock: {won_timeouts_p} of your tablebase wins ended lost or drawn on the clock (with or without an earlier tablebase error), against {won_timeouts_o} of the opponents'.")

    # --- 2. Per-move error rates in tablebase range -----------------------------------------
    report.heading("2. Tablebase errors per move")
    report.text(
        "A tablebase error is a move that turns a won position into a drawn or lost one, or a drawn position into a lost one. "
        "The denominator is moves played from won or drawn positions (a lost position cannot be made worse in tablebase terms). "
        "Rates are per 100 such moves."
    )
    overall = rate_table(at_risk.assign(all="all"), "tb_error", ["all"])
    by_tc = rate_table(at_risk, "tb_error", ["time_class"])
    err_tc = pd.concat([overall.rename(columns={"all": "time_class"}), by_tc], ignore_index=True)
    out = pd.DataFrame(
        {
            "time class": err_tc["time_class"],
            "you: errors / 100 moves": pct(err_tc, "player", "player_lo", "player_hi"),
            "your errors / moves": err_tc["player_errors"].astype(str) + " / " + err_tc["player_n"].astype(str),
            "opponents: errors / 100 moves": pct(err_tc, "opponent", "opponent_lo", "opponent_hi"),
            "their errors / moves": err_tc["opponent_errors"].astype(str) + " / " + err_tc["opponent_n"].astype(str),
            "games": err_tc["games"],
        }
    )
    report.table(out)

    # Error kinds.
    kinds = []
    for name, frame, flag in (("win -> draw", won, "err_win_draw"), ("win -> loss", won, "err_win_loss"), ("draw -> loss", drawn, "err_draw_loss")):
        row = rate_table(frame.assign(k=name), flag, ["k"]).iloc[0]
        kinds.append({"kind": name, "you: % of such positions": pct(pd.DataFrame([row]), "player", "player_lo", "player_hi").iloc[0], "your errors / moves": f"{row['player_errors']} / {row['player_n']}", "opponents: %": pct(pd.DataFrame([row]), "opponent", "opponent_lo", "opponent_hi").iloc[0], "their errors / moves": f"{row['opponent_errors']} / {row['opponent_n']}"})
    report.text("By kind of error (denominator: moves from won positions for the first two rows, from drawn positions for the third):")
    report.table(pd.DataFrame(kinds))

    # Expected points lost inside tablebase range, as a cross-check with the calibrated currency.
    ep_tb = compare_sides(tb, "ep_loss", by=["time_class"], n_boot=N_BOOT_CELLS)
    cp_tb = compare_sides(tb, "cp_loss", by=["time_class"], n_boot=N_BOOT_CELLS)
    ep_tb_out = pd.DataFrame(
        {
            "time class": ep_tb["time_class"].astype(str),
            "ep_loss you": ci_column(ep_tb, "player", "player_lo", "player_hi", ".4f"),
            "ep_loss opponents": ci_column(ep_tb, "opponent", "opponent_lo", "opponent_hi", ".4f"),
            "ep_loss diff": ci_column(ep_tb, "diff", "diff_lo", "diff_hi", ".4f"),
            "cp_loss you": ci_column(cp_tb, "player", "player_lo", "player_hi", ".1f"),
            "cp_loss opponents": ci_column(cp_tb, "opponent", "opponent_lo", "opponent_hi", ".1f"),
            "games": ep_tb["games"],
        }
    )
    report.text("Calibrated expected points lost per move inside tablebase range (all positions, both sides; the engine and the tablebase never disagree on the result class here):")
    report.table(ep_tb_out)

    # --- 3. By material type ----------------------------------------------------------------
    report.heading("3. Which endgame types go wrong (<= 5 pieces)")
    by_mat = rate_table(at_risk, "tb_error", ["tb_class"])
    by_mat["tb_class"] = pd.Categorical(by_mat["tb_class"], TB_CLASS_ORDER)
    by_mat = by_mat.sort_values("tb_class").reset_index(drop=True)
    by_mat = by_mat[(by_mat["player_n"] + by_mat["opponent_n"]) >= 30]
    mat_out = pd.DataFrame(
        {
            "material": by_mat["tb_class"].astype(str),
            "you: errors / 100 moves": pct(by_mat, "player", "player_lo", "player_hi"),
            "yours": by_mat["player_errors"].astype(str) + " / " + by_mat["player_n"].astype(str),
            "opponents: errors / 100 moves": pct(by_mat, "opponent", "opponent_lo", "opponent_hi"),
            "theirs": by_mat["opponent_errors"].astype(str) + " / " + by_mat["opponent_n"].astype(str),
            "games": by_mat["games"],
        }
    )
    report.text("Error rate per 100 moves from won-or-drawn positions, by coarse material class (classes with fewer than 30 such moves dropped):")
    report.table(mat_out)

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    plot_mat = by_mat[(by_mat["player_n"] >= 100) & (by_mat["opponent_n"] >= 100)]
    dot_ci(ax, plot_mat, plot_mat["tb_class"].astype(str).tolist(), xlabel="tablebase errors per 100 moves from won or drawn positions")
    ax.set_title("Tablebase errors by material type")
    ax.set_xlim(left=0)
    report.figure(fig, "errors_by_material", "Tablebase error rate per 100 at-risk moves by material class (classes with at least 100 such moves per side), you vs opponents, 95% game-bootstrap CIs.")
    plt.close(fig)

    # Finer: exact material strings with the most errors.
    fine = at_risk.groupby(["material", "side"], observed=True)["tb_error"].agg(["sum", "count"]).unstack("side").fillna(0)
    fine.columns = [f"{a}_{b}" for a, b in fine.columns]
    fine["errors"] = fine["sum_player"] + fine["sum_opponent"]
    fine = fine.sort_values("errors", ascending=False).head(12).reset_index()
    fine_out = pd.DataFrame(
        {
            "material (mover first)": fine["material"],
            "your errors / moves": fine["sum_player"].astype(int).astype(str) + " / " + fine["count_player"].astype(int).astype(str),
            "opponents' errors / moves": fine["sum_opponent"].astype(int).astype(str) + " / " + fine["count_opponent"].astype(int).astype(str),
        }
    )
    report.text(
        "The exact material balances with the most errors (counts, no CIs: these cells are too small for them to mean much; 'KRvKRP' is the mover with a bare rook against rook and pawn, i.e. the defender):"
    )
    report.table(fine_out)

    # Material by error kind: conversion (from won) vs holding (from drawn) per class, all time classes.
    conv_mat = rate_table(won, "tb_error", ["tb_class"]).rename(columns={"tb_class": "material"})
    hold_mat = rate_table(drawn, "tb_error", ["tb_class"]).rename(columns={"tb_class": "material"})
    conv_mat = conv_mat[(conv_mat["player_n"] + conv_mat["opponent_n"]) >= 30]
    hold_mat = hold_mat[(hold_mat["player_n"] + hold_mat["opponent_n"]) >= 30]
    split_out = pd.DataFrame(
        {
            "material": conv_mat["material"].astype(str),
            "winning side, you: errors / 100": pct(conv_mat, "player", "player_lo", "player_hi"),
            "yours": conv_mat["player_errors"].astype(str) + " / " + conv_mat["player_n"].astype(str),
            "winning side, opponents": pct(conv_mat, "opponent", "opponent_lo", "opponent_hi"),
            "theirs": conv_mat["opponent_errors"].astype(str) + " / " + conv_mat["opponent_n"].astype(str),
        }
    )
    report.text("Conversion errors only (moves from *won* positions):")
    report.table(split_out)
    split_out2 = pd.DataFrame(
        {
            "material": hold_mat["material"].astype(str),
            "drawn position, you: errors / 100": pct(hold_mat, "player", "player_lo", "player_hi"),
            "yours": hold_mat["player_errors"].astype(str) + " / " + hold_mat["player_n"].astype(str),
            "drawn position, opponents": pct(hold_mat, "opponent", "opponent_lo", "opponent_hi"),
            "theirs": hold_mat["opponent_errors"].astype(str) + " / " + hold_mat["opponent_n"].astype(str),
        }
    )
    report.text("Holding errors only (moves from *drawn* positions):")
    report.table(split_out2)

    # --- 4. Clock and DTZ -------------------------------------------------------------------
    report.heading("4. Clock, distance to conversion, and time spent")
    by_clock = rate_table(at_risk, "tb_error", ["clock_bin"])
    clock_out = pd.DataFrame(
        {
            "clock before move": by_clock["clock_bin"].astype(str),
            "you: errors / 100 moves": pct(by_clock, "player", "player_lo", "player_hi"),
            "yours": by_clock["player_errors"].astype(str) + " / " + by_clock["player_n"].astype(str),
            "opponents: errors / 100 moves": pct(by_clock, "opponent", "opponent_lo", "opponent_hi"),
            "theirs": by_clock["opponent_errors"].astype(str) + " / " + by_clock["opponent_n"].astype(str),
            "games": by_clock["games"],
        }
    )
    report.text("Error rate by the mover's clock before the move (all time classes pooled; tablebase positions are mostly reached late in the game, so the clock is usually low):")
    report.table(clock_out)

    # Same, split by time class for the two low-clock bins vs the rest (keeps cells big enough).
    at_risk["low_clock"] = np.where(at_risk["clock_before_seconds"] < 15, "<15s", ">=15s")
    by_clock_tc = rate_table(at_risk, "tb_error", ["time_class", "low_clock"])
    by_clock_tc = by_clock_tc[(by_clock_tc["player_n"] >= 30) & (by_clock_tc["opponent_n"] >= 30)]
    clock_tc_out = pd.DataFrame(
        {
            "time class": by_clock_tc["time_class"].astype(str),
            "clock": by_clock_tc["low_clock"],
            "you: errors / 100 moves": pct(by_clock_tc, "player", "player_lo", "player_hi"),
            "yours": by_clock_tc["player_errors"].astype(str) + " / " + by_clock_tc["player_n"].astype(str),
            "opponents: errors / 100 moves": pct(by_clock_tc, "opponent", "opponent_lo", "opponent_hi"),
            "theirs": by_clock_tc["opponent_errors"].astype(str) + " / " + by_clock_tc["opponent_n"].astype(str),
        }
    )
    report.text("Split by time class, below and above 15 seconds (cells with fewer than 30 moves on either side dropped):")
    report.table(clock_tc_out)

    fig, ax = plt.subplots(figsize=(7, 3.6))
    dot_ci(ax, by_clock, by_clock["clock_bin"].astype(str).tolist(), xlabel="tablebase errors per 100 at-risk moves")
    ax.set_title("Tablebase errors by clock before the move")
    ax.set_xlim(left=0)
    report.figure(fig, "errors_by_clock", "Tablebase error rate by the mover's remaining clock, you vs opponents, 95% game-bootstrap CIs.")
    plt.close(fig)

    # DTZ: in won positions, error rate and time spent by distance to zeroing.
    won["dtz_bin"] = pd.cut(won["abs_dtz"], DTZ_BINS, labels=DTZ_LABELS, right=False)
    dtz_err = rate_table(won, "tb_error", ["dtz_bin"])
    dtz_time = compare_sides(won, "time_spent_seconds", by=["dtz_bin"], n_boot=N_BOOT_CELLS)
    dtz_out = pd.DataFrame(
        {
            "DTZ (moves to a capture/pawn move keeping the win)": dtz_err["dtz_bin"].astype(str),
            "you: errors / 100 won-position moves": pct(dtz_err, "player", "player_lo", "player_hi"),
            "yours": dtz_err["player_errors"].astype(str) + " / " + dtz_err["player_n"].astype(str),
            "opponents: errors / 100": pct(dtz_err, "opponent", "opponent_lo", "opponent_hi"),
            "theirs": dtz_err["opponent_errors"].astype(str) + " / " + dtz_err["opponent_n"].astype(str),
            "seconds spent, you": ci_column(dtz_time, "player", "player_lo", "player_hi", ".2f"),
            "seconds spent, opponents": ci_column(dtz_time, "opponent", "opponent_lo", "opponent_hi", ".2f"),
        }
    )
    report.text(
        "Won positions only, by DTZ (Syzygy distance-to-zeroing: how many moves of best play until a capture or pawn move that keeps the win; longer means a harder, more technical win). "
        "Error rate and the time spent on the move:"
    )
    report.table(dtz_out)

    # Time spent vs clock in won positions: do you spend more when the clock allows?
    won_tc = compare_sides(won, "time_spent_seconds", by=["time_class"], n_boot=N_BOOT_CELLS)
    won_frac = compare_sides(won.assign(frac=won["time_spent_seconds"] / won["clock_before_seconds"].clip(lower=0.1)), "frac", by=["time_class"], n_boot=N_BOOT_CELLS)
    time_out = pd.DataFrame(
        {
            "time class": won_tc["time_class"].astype(str),
            "seconds per move, you": ci_column(won_tc, "player", "player_lo", "player_hi", ".2f"),
            "seconds per move, opponents": ci_column(won_tc, "opponent", "opponent_lo", "opponent_hi", ".2f"),
            "share of remaining clock, you": ci_column(won_frac, "player", "player_lo", "player_hi", ".3f"),
            "share of remaining clock, opponents": ci_column(won_frac, "opponent", "opponent_lo", "opponent_hi", ".3f"),
            "games": won_tc["games"],
        }
    )
    report.text("Time spent per move while winning in tablebase range, by time class:")
    report.table(time_out)

    # --- 5. Beyond 5 pieces -----------------------------------------------------------------
    report.heading("5. Beyond tablebase range: endgame-phase quality by material class (> 5 pieces)")
    report.text(
        "The endgame phase (material heuristic from the enrichment) with more than 5 pieces on the board, judged by calibrated expected points lost per move "
        "(`ep_loss`, main measure) and centipawn loss (`cp_loss`, secondary). The material class is from the piece types on the board regardless of side."
    )
    big["big_class"] = pd.Categorical(big["big_class"], BIG_CLASS_ORDER)
    ep_big = compare_sides(big, "ep_loss", by=["time_class", "big_class"], n_boot=N_BOOT_CELLS)
    cp_big = compare_sides(big, "cp_loss", by=["time_class", "big_class"], n_boot=N_BOOT_CELLS)
    ep_big = ep_big[ep_big["n"] >= 200].reset_index(drop=True)
    cp_big = cp_big.set_index(["time_class", "big_class"]).loc[list(zip(ep_big["time_class"], ep_big["big_class"]))].reset_index()
    big_out = pd.DataFrame(
        {
            "time class": ep_big["time_class"].astype(str),
            "material": ep_big["big_class"].astype(str),
            "ep_loss you": ci_column(ep_big, "player", "player_lo", "player_hi", ".4f"),
            "ep_loss opponents": ci_column(ep_big, "opponent", "opponent_lo", "opponent_hi", ".4f"),
            "ep_loss diff (you - opp)": ci_column(ep_big, "diff", "diff_lo", "diff_hi", ".4f"),
            "cp_loss you": ci_column(cp_big, "player", "player_lo", "player_hi", ".0f"),
            "cp_loss opponents": ci_column(cp_big, "opponent", "opponent_lo", "opponent_hi", ".0f"),
            "moves": ep_big["n"],
            "games": ep_big["games"],
        }
    )
    report.text("Cells with fewer than 200 moves are dropped.")
    report.table(big_out)

    ep_big_all = compare_sides(big, "ep_loss", by=["big_class"], n_boot=N_BOOT_CELLS)
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    dot_ci(ax, ep_big_all, ep_big_all["big_class"].astype(str).tolist(), scale=1000, xlabel="expected points lost per move (x 1000), endgame phase with > 5 pieces")
    ax.set_title("Endgame quality beyond tablebase range, by material class")
    report.figure(fig, "ep_loss_by_material", "Mean ep_loss per endgame-phase move with more than 5 pieces by material class (all time classes), you vs opponents, 95% game-bootstrap CIs.")
    plt.close(fig)

    # Blunder rate in big endgames (ep_loss >= 0.1) as a plainer summary.
    big["big_mistake"] = (big["ep_loss"] >= 0.10).astype(float)
    bm = compare_sides(big, "big_mistake", by=["time_class"], n_boot=N_BOOT_CELLS)
    bm_out = pd.DataFrame(
        {
            "time class": bm["time_class"].astype(str),
            "you: moves losing >= 0.10 expected points, %": pct(bm, "player", "player_lo", "player_hi"),
            "opponents: %": pct(bm, "opponent", "opponent_lo", "opponent_hi"),
            "difference (pts)": pct(bm, "diff", "diff_lo", "diff_hi"),
            "games": bm["games"],
        }
    )
    report.text("Share of endgame-phase moves (> 5 pieces) that lost at least 0.10 expected points:")
    report.table(bm_out)

    # --- 6. Summary -------------------------------------------------------------------------
    conv_all = conv.iloc[0]
    conv_bullet = conv[conv["time_class"] == "bullet"].iloc[0]
    err_all = err_tc.iloc[0]
    kind = {k["kind"]: k for k in kinds}
    sizeable = by_mat[(by_mat["player_n"] >= 100) & (by_mat["opponent_n"] >= 100)]
    worst = sizeable.sort_values("player", ascending=False).iloc[0]
    on_clock = failed_p["termination_reason"].isin(["timeout", "timeout vs insufficient"])
    clock_fail_p = int((on_clock & ~failed_p["game_uuid"].isin(my_err_games)).sum())
    bullet_fail = failed_p[failed_p["time_class"] == "bullet"]
    bullet_clock = int((~bullet_fail["game_uuid"].isin(my_err_games) & bullet_fail["termination_reason"].isin(["timeout", "timeout vs insufficient"])).sum())
    report.heading("What this means for the player")
    report.text(
        f"Over the board, your technique in the simplest endings is at least as good as rating-matched opponents': you convert tablebase-won endings {conv_all['conv_player'] * 100:.0f}% of the time "
        f"[{conv_all['conv_player_lo'] * 100:.0f}, {conv_all['conv_player_hi'] * 100:.0f}] against their {conv_all['conv_opp'] * 100:.0f}% [{conv_all['conv_opp_lo'] * 100:.0f}, {conv_all['conv_opp_hi'] * 100:.0f}], "
        f"and you throw away a win or draw on {err_all['player'] * 100:.1f} moves per 100 [{err_all['player_lo'] * 100:.1f}, {err_all['player_hi'] * 100:.1f}] against their {err_all['opponent'] * 100:.1f} [{err_all['opponent_lo'] * 100:.1f}, {err_all['opponent_hi'] * 100:.1f}]. "
        f"The split by kind is the useful part: you spoil *won* positions less often than they do ({kind['win -> draw']['you: % of such positions']} vs {kind['win -> draw']['opponents: %']} per 100 moves) "
        f"but lose *drawn* positions more often ({kind['draw -> loss']['you: % of such positions']} vs {kind['draw -> loss']['opponents: %']}), mostly in pawn endings and rook endings, so defending drawn endings (Philidor, the opposition) is the technique gap. "
        f"The material class that costs most, for both sides, is the **{worst['tb_class']}** ({worst['player'] * 100:.1f} errors per 100 at-risk moves in your hands [{worst['player_lo'] * 100:.1f}, {worst['player_hi'] * 100:.1f}], "
        f"{int(worst['player_errors'])} of {int(worst['player_n'])}; opponents {worst['opponent'] * 100:.1f} [{worst['opponent_lo'] * 100:.1f}, {worst['opponent_hi'] * 100:.1f}]), driven by rook and pawn versus rook from either side. "
        f"The biggest single leak, though, is not technique but the clock: {clock_fail_p} of your {len(failed_p)} failed conversions ended by timeout or 'timeout vs insufficient material' without any tablebase error, "
        f"{bullet_clock} of them in bullet, where your conversion rate is only {conv_bullet['conv_player'] * 100:.0f}% [{conv_bullet['conv_player_lo'] * 100:.0f}, {conv_bullet['conv_player_hi'] * 100:.0f}]. "
        "In a trivially won bullet ending the job is to mate fast and safely, not to play the tablebase-best move; keeping a pawn or a second piece on the board also stops the insufficient-material draw when the flag falls."
    )

    report.heading("Caveats")
    report.text(
        "- Only {:,} moves in {:,} games reach tablebase range, and only {} of them are errors ({} yours); every material or clock cell is small and the CIs say so. Treat differences whose CIs include zero as unresolved.\n".format(len(tb), tb["game_uuid"].nunique(), int(tb["tb_error"].sum()), int(tb.loc[tb["is_player_move"], "tb_error"].sum()))
        + "- Game-level conversion compares different games for you and for the opponents (the games where you were winning vs the games where they were); the difference CI treats them as independent samples. Opponents are rating-matched (within about 27 points) but are many different people.\n"
        "- The tablebase columns hold only win/draw/loss (no 'cursed win' distinction for the 50-move rule); repetition, 50-move and agreed draws without any tablebase error are counted as failed conversions by the rules, not as technique errors.\n"
        "- Tablebase positions arrive late in the game, so clock effects are confounded with the time class and with how long the game has lasted; the clock table pools time classes and the split table is coarse for that reason.\n"
        "- Beyond 5 pieces, 'endgame' is the material heuristic from the enrichment, and material classes are coarse (piece types present, regardless of which side has what); `ep_loss` is calibrated on these games with pre-game ratings and is the main measure, `cp_loss` secondary.\n"
        "- Time spent per move is from the clock comments and undercounts lag and pre-move time; DTZ is from the side to move and binned coarsely.\n"
        "- Bootstrap CIs are clustered by game with fixed seeds (n_boot {} for small tables, {} for cells).".format(N_BOOT, N_BOOT_CELLS)
    )

    path = report.write()
    print(f"wrote {path} in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
