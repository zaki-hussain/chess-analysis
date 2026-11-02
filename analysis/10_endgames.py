"""Q10: endgame technique - which endings you reach, whether you convert
tablebase wins and hold tablebase draws, which endings go wrong, and whether
failed wins were thrown away by a move or by the clock.

Two layers:
1. Tablebase range (at most `TB_PIECES` pieces, kings included): Syzygy gives
   the exact result before and after every move (`tb_wdl_before/after`,
   `tb_error`, `tb_dtz_*`). Game level: the first position in range fixes the
   theoretical result for the rest of the game (pieces only leave the board),
   so a game "enters" a won, drawn or lost ending for each side. Conversion
   (won ending -> won game) and hold (drawn ending -> not lost) rates, you vs
   opponents, with the calibrated model's expected score at entry as the
   baseline (score above it in Elo terms). Move level: tablebase errors per
   100 at-risk moves (moves from won or drawn positions) by material class,
   kind of error, clock band (seconds left and buffer beyond one increment)
   and DTZ. Failed conversions are split into "a wrong move" (the winner made
   a tablebase error) and "the clock" (no wrong move, ended on time).
2. Beyond tablebase range: calibrated expected points lost per move in the
   endgame phase with more than `TB_PIECES` pieces, by coarse material class.

Everything is split by exact time control (`data.tc_groups`). Tablebase data
are small (about 9,900 moves in 469 games, 160 errors), so every table is
also pooled per time class ("all bullet"), by increment within a time class
("bullet with increment"), and over all games; per-control cells carry their
counts and a `thin` flag. CIs resample whole games.

Writes CSVs to presentation/data/10_endgames/ and SVGs to
presentation/figures/10_endgames/ (no prose; the reading is in
presentation/README.md).

Run: .venv/bin/python analysis/10_endgames.py   (about 30 s)
"""

from __future__ import annotations

import time
from functools import partial

import numpy as np
import pandas as pd

from chess_analysis import charts, toolkit
from chess_analysis.toolkit import Results, add_elo, ci_cols, compare_sides, load_data

SLUG = "10_endgames"
SEED = 10
N_BOOT = 1000  # resamples for game-level rates (one value per game: cheap)
N_BOOT_CELLS = 500  # resamples for per-move cells (compare_sides); the first decimal per 100 moves is stable at this count
SIDES = ("player", "opponent")
ALL = "all games"  # pooled over every reported control (the thin 0.5+0 and 30+0 are never included)

# Syzygy 3-4-5: the tables in /opt/syzygy cover positions with at most 5 pieces, kings included.
TB_PIECES = 5
# chess.com result codes that mean the game ended on the clock: lost on time, or drawn because the
# flag fell against a side that could not mate ("timeout vs insufficient material").
CLOCK_ENDINGS = {"timeout": "lost on time", "timevsinsufficient": "drawn on time (insufficient material)"}

# Thin flags. A conversion or hold share from fewer than 30 endings has a CI of about +-0.15; an error rate
# from fewer than 100 at-risk moves rests on two or three errors (the overall rate is about 3 per 100).
MIN_ENDINGS = 30
MIN_AT_RISK_MOVES = 100
# A per-move rate is also thin when its moves come from fewer than 30 games: the CI resamples games, and with
# fewer a handful of games carries the whole rate (the 1+0 draw-to-loss rate rests on 24 games).
MIN_CELL_GAMES = 30
# ... or when both sides together made fewer than 5 errors: the comparison then rests on a few events, and a
# side with zero errors gets a meaningless 0-to-0 bootstrap CI.
MIN_EVENTS = 5
# Beyond tablebase range: mean expected points lost per move has a per-move SD of about 0.08, so 200 moves
# per side give a CI of roughly +-0.01, a third of the mean; smaller cells are flagged.
MIN_EP_MOVES = 200
# A move that loses at least a tenth of a game point by the calibrated model: a clear mistake.
BIG_MISTAKE_EP = 0.10

# Clock bands before the move (or at entry into the ending). Under 5 s is flag territory in every control;
# 5-15 s is a second or two a move for a typical 10-20 move tablebase ending; 15-60 s leaves thinking time;
# 60+ s is comfortable. Applied to plain seconds left and to the buffer beyond one increment (in 1+1 a
# player on 5 s still gets a second back each move, in 1+0 they do not).
CLOCK_EDGES = [-np.inf, 5.0, 15.0, 60.0, np.inf]
CLOCK_LABELS = ["<5 s", "5-15 s", "15-60 s", "60+ s"]
CLOCK_MEASURES = {"seconds left": "clock_before_seconds", "buffer beyond increment": "clock_buffer_seconds"}
# DTZ (moves of best play to the next capture or pawn move that keeps the win): 1-2 is a forced capture or
# promotion, 3-5 a short manoeuvre, 6-10 and 11-20 real technique, 21+ long known wins (e.g. KBN v K, KQ v KR).
DTZ_EDGES = [1, 3, 6, 11, 21, np.inf]
DTZ_LABELS = ["1-2", "3-5", "6-10", "11-20", "21+"]
TOP_MATERIAL = 12  # exact material balances listed (by total errors): beyond that the cells hold one or two errors
# chess.com clocks have a resolution of 0.1 s; a clock shown as 0.0 is floored there so the share of the
# remaining clock spent on a move stays finite.
CLOCK_RESOLUTION_SECONDS = 0.1
# Chart x limits: the longest drawn CI whisker plus 8% room; when every cell of a chart is thin, a range that
# leaves room for the "too few" labels.
XLIM_PAD = 1.08
XLIM_EMPTY = 10.0
WILSON_Z = 1.96  # 95% Wilson score intervals (5b): honest for shares from very few events, unlike the bootstrap

# Coarse material classes for tablebase range (describe the whole board, not the side to move).
TB_CLASS_ORDER = [
    "pawn ending", "rook ending", "rook vs pawns/bare king", "queen ending",
    "queen vs pawns/bare king", "queen vs rook/minor", "rook vs minor",
    "minor vs minor", "minor vs pawns/bare king",
]  # fmt: skip
# Coarse classes beyond tablebase range, from the piece types on the board.
BIG_CLASS_ORDER = ["pawn ending", "rook ending", "minor piece ending", "rook + minor", "queen ending", "queen + others"]
STATE_LABELS = {2: "won", 0: "drawn", -2: "lost"}  # tablebase state of an ending from your side
KIND_ORDER = ["win to draw", "win to loss", "draw to loss", "any (won or drawn)"]


# --- views: controls, class pools, increment pools, all games -------------------------------------------


def build_views(groups: list[str], tc_class: dict[str, str], increment: dict[str, float]) -> list[dict]:
    """Each control, then per time class its pool ("all bullet", if it has 2+ controls) and its increment
    pools ("bullet with increment", if the class has controls with and without increment and the pool has
    2+ controls; a 1-control side is the control itself), then all games. Class and increment come from the
    data (`data.tc_class`, the games' `increment_seconds`), not from the label text."""
    out = [{"tc_group": g, "members": [g], "pooled": False, "cls": tc_class[g], "increment": increment[g]} for g in groups]
    for cls, members in classes_of(out).items():
        if len(members) >= 2:
            out.append({"tc_group": f"all {cls}", "members": members, "pooled": True, "cls": cls})
        kinds = increment_kinds(members, increment)
        if len(kinds) == 2:
            for name, part in kinds.items():
                if len(part) >= 2:
                    out.append({"tc_group": f"{cls} {name}", "members": part, "pooled": True, "cls": cls, "kind": name})
    out.append({"tc_group": ALL, "members": list(groups), "pooled": True, "cls": None})
    for v in out:
        v["controls"] = " ".join(m.split(" ", 1)[1] for m in v["members"])  # display only: "1+1 2+1"
    return out


def classes_of(views: list[dict]) -> dict[str, list[str]]:
    """Time class -> its controls, in report order (single-control views only)."""
    classes: dict[str, list[str]] = {}
    for v in views:
        if not v["pooled"]:
            classes.setdefault(v["cls"], []).append(v["tc_group"])
    return classes


def increment_kinds(members: list[str], increment: dict[str, float]) -> dict[str, list[str]]:
    """Controls of one class split into 'no increment' / 'with increment'."""
    kinds: dict[str, list[str]] = {}
    for m in members:
        kinds.setdefault("with increment" if increment[m] > 0 else "no increment", []).append(m)
    return kinds


def control_increments(views: list[dict]) -> dict[str, float]:
    return {v["tc_group"]: v["increment"] for v in views if not v["pooled"]}


def class_order(views: list[dict]) -> list[str]:
    """One view per time class (its pool, or its only control) and all games: the readable summary rows."""
    labels = {v["tc_group"] for v in views}
    out = [f"all {cls}" if f"all {cls}" in labels else members[0] for cls, members in classes_of(views).items()]
    return [*out, ALL]


def increment_order(views: list[dict]) -> list[str]:
    """Per time class: its no-increment and with-increment sides (pool or single control) where it has
    both, else the class view; then all games."""
    labels = {v["tc_group"] for v in views}
    increment = control_increments(views)
    out = []
    for cls, members in classes_of(views).items():
        kinds = increment_kinds(members, increment)
        if len(kinds) == 2:
            for name in ("no increment", "with increment"):
                out.append(f"{cls} {name}" if f"{cls} {name}" in labels else kinds[name][0])
        else:
            out.append(f"all {cls}" if f"all {cls}" in labels else members[0])
    return [*out, ALL]


def view_row(v: dict) -> dict:
    return {"tc_group": v["tc_group"], "pooled": v["pooled"], "controls": v["controls"]}


# --- material ------------------------------------------------------------------------------------------------


def piece_counts(fen: pd.Series) -> pd.DataFrame:
    """Counts of every piece letter in the board part of a FEN, plus the total with both kings."""
    board = fen.str.split(" ").str[0]
    out = pd.DataFrame(index=fen.index)
    for letter in "QRBNPqrbnp":
        out[letter] = board.str.count(letter)
    out["total"] = out[list("QRBNPqrbnp")].sum(axis=1) + 2
    return out


def tb_material_class(pc: pd.DataFrame) -> pd.Series:
    """Coarse class of a tablebase-range position from the piece sets of both sides."""
    wq, bq = pc["Q"] > 0, pc["q"] > 0
    wr, br = pc["R"] > 0, pc["r"] > 0
    wm, bm = (pc["B"] + pc["N"]) > 0, (pc["b"] + pc["n"]) > 0
    conditions = [~(wq | bq | wr | br | wm | bm), wq & bq, (wq & (br | bm)) | (bq & (wr | wm)), wq | bq, wr & br, (wr & bm) | (br & wm), wr | br, wm & bm]
    choices = ["pawn ending", "queen ending", "queen vs rook/minor", "queen vs pawns/bare king", "rook ending", "rook vs minor", "rook vs pawns/bare king", "minor vs minor"]
    return pd.Series(np.select(conditions, choices, default="minor vs pawns/bare king"), index=pc.index)


def big_material_class(pc: pd.DataFrame) -> pd.Series:
    """Coarse class of an endgame position beyond tablebase range from the piece types present."""
    has_q = (pc["Q"] + pc["q"]) > 0
    has_r = (pc["R"] + pc["r"]) > 0
    has_m = (pc["B"] + pc["N"] + pc["b"] + pc["n"]) > 0
    conditions = [~(has_q | has_r | has_m), has_q & (has_r | has_m), has_q, has_r & has_m, has_r]
    choices = ["pawn ending", "queen + others", "queen ending", "rook + minor", "rook ending"]
    return pd.Series(np.select(conditions, choices, default="minor piece ending"), index=pc.index)


def canonical_material(pc: pd.DataFrame, color: pd.Series) -> pd.Series:
    """'KRPvKR'-style string, the mover's pieces first."""
    white = pd.Series("", index=pc.index)
    black = pd.Series("", index=pc.index)
    for letter in "QRBNP":
        white = white + pc[letter].map(lambda k, s=letter: s * int(k))
        black = black + pc[letter.lower()].map(lambda k, s=letter: s * int(k))
    is_white = (color == "white").to_numpy()
    mover = np.where(is_white, white.to_numpy(), black.to_numpy())
    other = np.where(is_white, black.to_numpy(), white.to_numpy())
    return pd.Series([f"K{a}vK{b}" for a, b in zip(mover, other)], index=pc.index)


# --- bootstrap helpers (rows are games) ----------------------------------------------------------------------


# Means of one value per game (and differences between two disjoint sets of games), games resampled.
boot_mean = partial(toolkit.boot_mean, n_boot=N_BOOT, seed=SEED)
boot_diff = partial(toolkit.boot_mean_diff, n_boot=N_BOOT, seed=SEED)


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for k events in n independent trials (one won ending per game)."""
    if n == 0:
        return np.nan, np.nan
    p, z2 = k / n, WILSON_Z**2
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = WILSON_Z * np.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, float(centre - half)), min(1.0, float(centre + half))  # clamp: avoids -0.0 at k = 0


def newcombe(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """95% CI of p1 - p2 from two independent samples (Newcombe's hybrid score method, from Wilson ends)."""
    if n1 == 0 or n2 == 0:
        return np.nan, np.nan
    p1, p2 = k1 / n1, k2 / n2
    (l1, u1), (l2, u2) = wilson(k1, n1), wilson(k2, n2)
    d = p1 - p2
    return float(d - np.hypot(p1 - l1, u2 - p2)), float(d + np.hypot(u1 - p1, p2 - l2))


def counts_last(table: pd.DataFrame, counts: list[str]) -> pd.DataFrame:
    """Tidy column order: everything else (levels, estimates, Elo columns), then the counts, then `thin`."""
    tail = [c for c in counts if c in table.columns] + ["thin"]
    return table[[c for c in table.columns if c not in tail] + tail]


def x_limit(frames: list[pd.DataFrame], prefix: str) -> float:
    """Right x limit for per-100 bars: the longest non-thin CI end plus room (XLIM_PAD), or XLIM_EMPTY."""
    ends = [f[~f["thin"]][[f"{prefix}{s}_hi" for s in SIDES]].to_numpy(dtype=float) for f in frames]
    values = np.concatenate([e.ravel() for e in ends]) if ends else np.array([])
    values = values[np.isfinite(values)]
    return float(values.max() * XLIM_PAD) if len(values) else XLIM_EMPTY


# --- data preparation -------------------------------------------------------------------------------------


def prepare(data):
    moves = data.moves
    pc = piece_counts(moves["fen_before"])
    moves = moves.assign(n_pieces=pc["total"], tb_class=tb_material_class(pc), big_class=big_material_class(pc))

    tb = moves[moves["tb_wdl_before"].notna() & (moves["n_pieces"] <= TB_PIECES)].copy()
    tb["tb_wdl_before"] = tb["tb_wdl_before"].astype(int)
    tb["tb_wdl_after"] = tb["tb_wdl_after"].astype(int)
    tb["tb_error"] = tb["tb_error"].astype(bool)
    tb["abs_dtz"] = tb["tb_dtz_before"].astype(float).abs()
    tb["material"] = canonical_material(pc.loc[tb.index], tb["color"])
    tb["err_win_draw"] = (tb["tb_wdl_before"] == 2) & (tb["tb_wdl_after"] == 0)
    tb["err_win_loss"] = (tb["tb_wdl_before"] == 2) & (tb["tb_wdl_after"] == -2)
    tb["err_draw_loss"] = (tb["tb_wdl_before"] == 0) & (tb["tb_wdl_after"] == -2)
    tb = tb.sort_values(["game_uuid", "ply"]).reset_index(drop=True)

    # One row per game at its entry into tablebase range (drop_duplicates, not groupby.first: that would
    # pick the first non-null value per column).
    entry = tb.drop_duplicates("game_uuid").set_index("game_uuid")
    errors = tb[tb["tb_error"]].groupby(["game_uuid", "is_player_move"]).size().unstack(fill_value=0)
    games = data.games.set_index("uuid")
    mine = entry["is_player_move"]
    ending = pd.DataFrame(
        {
            "tc_group": entry["tc_group"].astype(str),
            "tb_class": entry["tb_class"],
            "state": np.where(mine, entry["tb_wdl_before"], -entry["tb_wdl_before"]),  # your side's view
            "outcome": entry["outcome"],
            "my_score": games.loc[entry.index, "my_score"],
            "my_result": games.loc[entry.index, "my_result"],
            "opponent_result": games.loc[entry.index, "opponent_result"],
            # Calibrated expected score at entry (model with both clocks and pre-game ratings), your view.
            "my_expected": np.where(mine, entry["ep_before"], 1 - entry["ep_before"]),
            "my_clock": np.where(mine, entry["clock_before_seconds"], entry["opponent_clock_seconds"]),
            "opponent_clock": np.where(mine, entry["opponent_clock_seconds"], entry["clock_before_seconds"]),
            "increment": entry["increment_seconds"],
            "my_errors": errors.get(True, pd.Series(dtype=float)).reindex(entry.index).fillna(0).astype(int),
            "opponent_errors": errors.get(False, pd.Series(dtype=float)).reindex(entry.index).fillna(0).astype(int),
        },
        index=entry.index,
    ).reset_index()

    # Won endings, one row per (game, winning side): the winner's view of everything.
    won = []
    for side, sign in (("player", 1), ("opponent", -1)):
        part = ending[ending["state"] == 2 * sign]
        mine_side = side == "player"
        won.append(
            pd.DataFrame(
                {
                    "game_uuid": part["game_uuid"], "tc_group": part["tc_group"], "tb_class": part["tb_class"], "side": side,
                    "score": part["my_score"] if mine_side else 1 - part["my_score"],
                    "expected": part["my_expected"] if mine_side else 1 - part["my_expected"],
                    "end_code": part["my_result"] if mine_side else part["opponent_result"],
                    "loser_code": part["opponent_result"] if mine_side else part["my_result"],
                    "wrong_move": (part["my_errors"] if mine_side else part["opponent_errors"]) > 0,
                    "clock": part["my_clock"] if mine_side else part["opponent_clock"],
                    "buffer": (part["my_clock"] if mine_side else part["opponent_clock"]) - part["increment"],
                }  # fmt: skip
            )
        )
    won = pd.concat(won, ignore_index=True)
    won["converted"] = won["score"] == 1.0
    won["cause"] = np.select(
        [won["converted"], won["wrong_move"], won["end_code"] == "timeout", won["end_code"] == "timevsinsufficient"],
        ["converted", "wrong move", CLOCK_ENDINGS["timeout"], CLOCK_ENDINGS["timevsinsufficient"]],
        default="other, no wrong move",
    )

    big = moves[(moves["phase"] == "endgame") & (moves["n_pieces"] > TB_PIECES)].copy()
    return moves, tb, ending, won, big


# --- per-move error rates ----------------------------------------------------------------------------------


def error_cells(frame: pd.DataFrame, flag: str, views: list[dict], by: list[str] | None = None, min_moves: int = MIN_AT_RISK_MOVES) -> pd.DataFrame:
    """Errors per 100 moves (`flag`), you vs opponents in the same games, per view and `by` level.
    CIs and the difference resample whole games (compare_sides). Thin: fewer than `min_moves` moves on a
    side, fewer than MIN_CELL_GAMES games, or fewer than MIN_EVENTS errors on both sides together."""
    by = by or []
    rows = []
    for v in views:
        sub = frame[frame["tc_group"].isin(v["members"])]
        if sub.empty:
            continue
        sub = sub.assign(_per100=sub[flag].astype(float) * 100)
        cmp = compare_sides(sub, "_per100", by=by, n_boot=N_BOOT_CELLS)
        counts = sub.groupby([*by, "is_player_move"], observed=True)[flag].agg(["sum", "count"]).unstack("is_player_move", fill_value=0) if by else None
        for _, c in cmp.iterrows():
            row = view_row(v)
            key = tuple(c[b] for b in by)
            for b, k in zip(by, key):
                row[b] = k
            for side in SIDES:
                for part in ("", "_lo", "_hi"):
                    row[f"per_100_{side}{part}"] = c[f"{side}{part}"]
            for part in ("", "_lo", "_hi"):
                row[f"per_100_diff{part}"] = c[f"diff{part}"]
            if by:
                cnt = counts.loc[key if len(by) > 1 else key[0]]
                pm, pe = int(cnt.get(("count", True), 0)), int(cnt.get(("sum", True), 0))
                om, oe = int(cnt.get(("count", False), 0)), int(cnt.get(("sum", False), 0))
            else:
                p, o = sub[sub["is_player_move"]], sub[~sub["is_player_move"]]
                pm, pe, om, oe = len(p), int(p[flag].sum()), len(o), int(o[flag].sum())
            games = int(c["games"])
            thin = min(pm, om) < min_moves or games < MIN_CELL_GAMES or pe + oe < MIN_EVENTS
            row.update({"player_moves": pm, "player_errors": pe, "opponent_moves": om, "opponent_errors": oe, "games": games, "thin": thin})
            rows.append(row)
    return pd.DataFrame(rows)


# --- 10.1 which endings ------------------------------------------------------------------------------------


def q1_reached(results: Results, views, data, ending, big) -> None:
    games = data.games.assign(tc_group=data.games["tc_group"].astype(str))
    reach, by_class, big_rows = [], [], []
    big_games = big.groupby(["game_uuid", "big_class"]).size().reset_index()[["game_uuid", "big_class"]]
    endgame_games = set(big["game_uuid"])
    for v in views:
        g = games[games["tc_group"].isin(v["members"])]
        e = ending[ending["tc_group"].isin(v["members"])]
        n_games, n_tb = len(g), len(e)
        row = {**view_row(v), "tablebase_share": n_tb / n_games if n_games else np.nan, "endgame_phase_share": float(g["uuid"].isin(endgame_games).mean()) if n_games else np.nan}
        for state, label in STATE_LABELS.items():
            row[f"{label}_for_you"] = int((e["state"] == state).sum())
            row[f"{label}_for_you_share"] = float((e["state"] == state).mean()) if n_tb else np.nan
        row.update({"games": n_games, "tablebase_games": n_tb, "thin": n_tb < MIN_ENDINGS})
        reach.append(row)
        for cls in TB_CLASS_ORDER:
            part = e[e["tb_class"] == cls]
            r = {**view_row(v), "tb_class": cls, "share": len(part) / n_tb if n_tb else np.nan}
            for state, label in STATE_LABELS.items():
                r[f"{label}_for_you_share"] = float((part["state"] == state).sum() / n_tb) if n_tb else np.nan
            for state, label in STATE_LABELS.items():
                r[f"{label}_for_you"] = int((part["state"] == state).sum())
            r.update({"endings": len(part), "tablebase_games": n_tb, "thin": n_tb < MIN_ENDINGS})
            by_class.append(r)
        bg = big_games[big_games["game_uuid"].isin(g["uuid"])]
        for cls in BIG_CLASS_ORDER:
            k = int((bg["big_class"] == cls).sum())
            big_rows.append({**view_row(v), "big_class": cls, "share_of_games": k / n_games if n_games else np.nan, "games_reaching": k, "games": n_games, "thin": k < MIN_ENDINGS})
    table = pd.DataFrame(by_class)
    results.table("1_endgames_reached", table, f"Tablebase endings (<= {TB_PIECES} pieces) by the material class of the first position in range: share of the view's tablebase games, split by the theoretical result from your side (won / drawn / lost for you); counts.")
    results.table("1b_tablebase_reach", pd.DataFrame(reach), f"Share of games that reached tablebase range (<= {TB_PIECES} pieces) and the endgame phase beyond it; entry state from your side (won / drawn / lost for you), counts and shares.")
    results.table("1c_big_endgames_reached", pd.DataFrame(big_rows), f"Share of games with at least one endgame-phase move with more than {TB_PIECES} pieces in each coarse material class (a game can reach several); thin = fewer than {MIN_ENDINGS} games reaching the class.")

    order = class_order(views)
    fig, axes = panel_row(order, "Which tablebase endings do you reach?")
    colours = {"won": charts.PLAYER, "drawn": charts.AXIS, "lost": charts.SERIES[1]}
    ypos = np.arange(len(TB_CLASS_ORDER))
    for label in order:
        ax = axes[label]
        part = table[table["tc_group"] == label].set_index("tb_class").reindex(TB_CLASS_ORDER)
        states = ("won", "drawn", "lost")
        left = charts.stacked_bars(ax, ypos, [part[f"{state}_for_you_share"].to_numpy(dtype=float) for state in states], [colours[state] for state in states], labels=[f"{state} for you" for state in states], width=0.7, horizontal=True)
        n = int(part["tablebase_games"].iloc[0])
        ax.set_title(f"{label} ({n} games)")
        ax.set_yticks(ypos, TB_CLASS_ORDER)
        ax.grid(axis="y", visible=False)
        ax.set_xlim(0, max(0.45, float(np.nanmax(left)) * 1.08))
        ax.xaxis.set_major_formatter(_pct_formatter())
    next(iter(axes.values())).set_ylim(len(TB_CLASS_ORDER) - 0.5, -0.5)
    charts.legend(fig, loc="upper right")
    fig.tight_layout()
    charts.footnote(fig, f"Share of games that entered tablebase range (<= {TB_PIECES} pieces) in each material class, by the exact result from your side at entry. Bars add up to 100% per panel.")
    results.figure(fig, "1_endgames_reached")


def panel_row(labels: list[str], title: str, panel: tuple[float, float] = (2.75, 3.4)):
    """One row of panels sharing the y axis (class views side by side), titled with the question."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(labels), figsize=(panel[0] * len(labels), panel[1] + 0.5), sharey=True, squeeze=False)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    out = {}
    for label, ax in zip(labels, axes[0]):
        ax.set_title(label)
        out[label] = ax
    return fig, out


def _pct_formatter():
    from matplotlib.ticker import FuncFormatter

    return FuncFormatter(lambda x, _: f"{x * 100:.0f}%")


# --- 10.2 conversion / 10.3 hold ------------------------------------------------------------------------


def q2_conversion(results: Results, views, won, tb) -> pd.DataFrame:
    rows = []
    for v in views:
        w = won[won["tc_group"].isin(v["members"])]
        p, o = (w[w["side"] == s] for s in SIDES)
        row = view_row(v)
        for name, col in (("conversion", "converted"), ("score", "score")):
            row.update(ci_cols(f"{name}_player", boot_mean(p[col].astype(float))))
            row.update(ci_cols(f"{name}_opponent", boot_mean(o[col].astype(float), seed=SEED + 1)))
            row.update(ci_cols(f"{name}_diff", boot_diff(p[col].astype(float), o[col].astype(float))))
        for side, part in zip(SIDES, (p, o)):
            row[f"expected_{side}"] = float(part["expected"].mean()) if len(part) else np.nan
            row.update(ci_cols(f"excess_{side}", boot_mean((part["score"] - part["expected"]).astype(float), seed=SEED + 2)))
            row[f"points_dropped_per_100_{side}"] = 100 * float(1 - part["score"].mean()) if len(part) else np.nan
            row[f"entry_clock_median_{side}"] = float(part["clock"].median()) if len(part) else np.nan
            row[f"entry_buffer_median_{side}"] = float(part["buffer"].median()) if len(part) else np.nan
        row.update({"player_endings": len(p), "opponent_endings": len(o), "player_converted": int(p["converted"].sum()), "opponent_converted": int(o["converted"].sum()), "thin": min(len(p), len(o)) < MIN_ENDINGS})
        rows.append(row)
    table = pd.DataFrame(rows)
    for side in SIDES:
        table = add_elo(table, f"expected_{side}", [f"excess_{side}", f"excess_{side}_lo", f"excess_{side}_hi"])
    table = counts_last(table, ["player_endings", "opponent_endings", "player_converted", "opponent_converted"])
    results.table(
        "2_conversion", table,
        "Won tablebase endings (the first position in range is a win for that side): share converted into a won game and mean score, you vs opponents (different games: each side's own won endings; diff CIs resample both sets). "
        "expected = calibrated model's expected score at entry (it knows both clocks and ratings); excess = score - expected, with its Elo-equivalent; points dropped per 100 won endings; the winner's median clock at entry (seconds left, and buffer beyond one increment).",
    )  # fmt: skip

    kinds = []
    for kind, frame, flag in (
        ("win to draw", tb[tb["tb_wdl_before"] == 2], "err_win_draw"), ("win to loss", tb[tb["tb_wdl_before"] == 2], "err_win_loss"),
        ("draw to loss", tb[tb["tb_wdl_before"] == 0], "err_draw_loss"), ("any (won or drawn)", tb[tb["tb_wdl_before"] >= 0], "tb_error"),
    ):  # fmt: skip
        cells = error_cells(frame, flag, views)
        cells.insert(3, "kind", kind)
        kinds.append(cells)
    kinds = pd.concat(kinds, ignore_index=True)
    kinds["kind"] = pd.Categorical(kinds["kind"], KIND_ORDER)
    kinds["_v"] = kinds["tc_group"].map({v["tc_group"]: i for i, v in enumerate(views)})
    kinds = kinds.sort_values(["_v", "kind"]).drop(columns="_v")
    results.table("2b_errors_by_kind", kinds, "Tablebase errors per 100 moves, by kind: from won positions (win to draw, win to loss), from drawn positions (draw to loss), and from any won-or-drawn position; you vs opponents in the same games, diff = you minus opponents (games resampled); move and error counts.")

    order = chart_order(views)
    spoiled = kinds[kinds["kind"] == "win to draw"]
    view_bars(
        results,
        [
            {"table": table, "prefix": "conversion_", "subtitle": "won endings converted into a win", "xlabel": "share of won endings", "xmax": 1.0, "percent": True, "count": endings_text("endings")},
            {"table": spoiled, "prefix": "per_100_", "subtitle": "moves that turn a win into a draw", "xlabel": "per 100 moves from won positions", "xmax": x_limit([spoiled], "per_100_"), "count": moves_text},
        ],
        order, "2_conversion", "How often do you win a won tablebase ending?",
        f"Won endings: the first position with <= {TB_PIECES} pieces is a tablebase win for that side; each side's own won endings. Right: per-move errors from won positions in the same games. 95% CIs, games resampled.",
    )
    return kinds


def q3_hold(results: Results, views, ending, kinds: pd.DataFrame) -> None:
    rows = []
    for v in views:
        d = ending[(ending["state"] == 0) & ending["tc_group"].isin(v["members"])]
        hp = (d["outcome"] != "loss").astype(float)
        ho = (d["outcome"] != "win").astype(float)
        row = view_row(v)
        row.update(ci_cols("hold_player", boot_mean(hp)))
        row.update(ci_cols("hold_opponent", boot_mean(ho)))
        row.update(ci_cols("hold_diff", boot_mean(hp - ho)))  # same games: paired
        row["expected_player"] = float(d["my_expected"].mean()) if len(d) else np.nan
        row.update(ci_cols("score_player", boot_mean(d["my_score"].astype(float))))
        row.update(ci_cols("excess_player", boot_mean((d["my_score"] - d["my_expected"]).astype(float))))
        row.update({
            "drawn_endings": len(d), "player_held": int(hp.sum()), "opponent_held": int(ho.sum()),
            "ended_drawn": int((d["outcome"] == "draw").sum()), "you_won": int((d["outcome"] == "win").sum()), "you_lost": int((d["outcome"] == "loss").sum()),
            "thin": len(d) < MIN_ENDINGS,
        })  # fmt: skip
        rows.append(row)
    table = add_elo(pd.DataFrame(rows), "expected_player", ["excess_player", "excess_player_lo", "excess_player_hi"])
    table = counts_last(table, ["drawn_endings", "player_held", "opponent_held", "ended_drawn", "you_won", "you_lost"])
    results.table(
        "3_hold", table,
        "Drawn tablebase endings (the first position in range is a draw): share not lost by you (hold_player) and by your opponents (hold_opponent) in the same games; hold_diff = you minus opponents, paired by game (= your wins minus your losses from drawn endings). "
        "Your score vs the model's expected score at entry, with Elo-equivalent.",
    )  # fmt: skip
    held = kinds[kinds["kind"] == "draw to loss"]
    view_bars(
        results,
        [
            {"table": table.assign(player_endings=table["drawn_endings"], opponent_endings=table["drawn_endings"]), "prefix": "hold_", "subtitle": "drawn endings not lost", "xlabel": "share of drawn endings", "xmax": 1.0, "percent": True, "count": endings_text("endings")},
            {"table": held, "prefix": "per_100_", "subtitle": "moves that turn a draw into a loss", "xlabel": "per 100 moves from drawn positions", "xmax": x_limit([held], "per_100_"), "count": moves_text},
        ],
        chart_order(views), "3_hold", "How often do you hold a drawn tablebase ending?",
        f"Drawn endings: the first position with <= {TB_PIECES} pieces is a tablebase draw; both sides from the same games. Right: per-move errors from drawn positions. 95% CIs, games resampled.",
    )


def view_bars(results: Results, panels: list[dict], order: list[str], name: str, title: str, note: str) -> None:
    """Horizontal you-vs-opponents bars, one row per view, one panel per measure; thin rows are left blank
    with their counts. Each panel: table, prefix (columns <prefix>player/_lo/_hi...), subtitle, xlabel,
    xmax (1.0 = 100% for shares, else from x_limit), percent (axis as %), count (row -> text for a thin row)."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(panels), figsize=(4.3 * len(panels) + 1.2, 0.36 * len(order) + 1.5), sharey=True, squeeze=False)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    for ax, panel in zip(axes[0], panels):
        table, prefix = panel["table"], panel["prefix"]
        shown = table.copy()
        cols = [f"{prefix}{s}{p}" for s in SIDES for p in ("", "_lo", "_hi")]
        shown.loc[shown["thin"], cols] = np.nan
        charts.side_bars(ax, shown, "tc_group", value=prefix, order=order, horizontal=True)
        idx = table.set_index("tc_group")
        for i, label in enumerate(order):
            if label not in idx.index or idx.loc[label, "thin"]:
                text = panel["count"](idx.loc[label]) if label in idx.index else "no data"
                ax.text(0.01, i, text, transform=ax.get_yaxis_transform(), va="center", ha="left", fontsize=7.5, color=charts.INK_MUTED)
        n_controls = sum(1 for o in order if not o.startswith("all "))  # chart_order: controls first, then "all ..." pools
        ax.axhline(n_controls - 0.5, color=charts.INK_MUTED, linewidth=0.8)
        ax.set_xlim(0, panel["xmax"])
        if panel.get("percent"):
            ax.xaxis.set_major_formatter(_pct_formatter())
        ax.set_xlabel(panel["xlabel"])
        ax.set_title(panel["subtitle"])
    axes[0][0].set_ylim(len(order) - 0.5, -0.5)
    charts.legend(fig, loc="upper right")
    fig.tight_layout()
    charts.footnote(fig, note + f" Rows below the line pool several controls; blank rows are too thin (shares: under {MIN_ENDINGS} endings; per-move rates: under {MIN_AT_RISK_MOVES} moves a side, {MIN_CELL_GAMES} games or {MIN_EVENTS} errors).")
    results.figure(fig, name)


def chart_order(views: list[dict]) -> list[str]:
    """Every control, then the class pools and all games."""
    controls = [v["tc_group"] for v in views if not v["pooled"]]
    return controls + [o for o in class_order(views) if o not in controls]


def endings_text(unit: str):
    def text(row) -> str:
        p, o = int(row["player_endings"]), int(row["opponent_endings"])
        return f"too few {unit} ({p})" if p == o else f"too few {unit} ({p} yours, {o} theirs)"

    return text


def moves_text(row) -> str:
    """Label for a thin per-move cell: its games, moves and errors (any of the three can make it thin)."""
    g, e = int(row["games"]), int(row["player_errors"] + row["opponent_errors"])
    return f"too few: {g} game{'s' * (g != 1)}, {int(row['player_moves'])}/{int(row['opponent_moves'])} moves, {e} error{'s' * (e != 1)}"


# --- 10.4 which endings produce errors ---------------------------------------------------------------------


def q4_material(results: Results, views, tb, big) -> None:
    at_risk = tb[tb["tb_wdl_before"] >= 0]
    by_mat = error_cells(at_risk, "tb_error", views, by=["tb_class"])
    by_mat["tb_class"] = pd.Categorical(by_mat["tb_class"], TB_CLASS_ORDER)
    by_mat["_v"] = by_mat["tc_group"].map({v["tc_group"]: i for i, v in enumerate(views)})
    by_mat = by_mat.sort_values(["_v", "tb_class"]).drop(columns="_v")
    results.table("4_errors_by_material", by_mat, f"Tablebase errors per 100 at-risk moves (moves from won or drawn positions) by material class, you vs opponents in the same games; diff = you minus opponents (games resampled); thin = fewer than {MIN_AT_RISK_MOVES} at-risk moves on either side.")

    split = []
    for state, frame in (("won", tb[tb["tb_wdl_before"] == 2]), ("drawn", tb[tb["tb_wdl_before"] == 0])):
        cells = error_cells(frame, "tb_error", views, by=["tb_class"])
        cells.insert(3, "position", state)
        split.append(cells)
    split = pd.concat(split, ignore_index=True)
    split["tb_class"] = pd.Categorical(split["tb_class"], TB_CLASS_ORDER)
    split["_v"] = split["tc_group"].map({v["tc_group"]: i for i, v in enumerate(views)})
    split = split.sort_values(["_v", "position", "tb_class"]).drop(columns="_v")
    results.table("4b_errors_by_material_kind", split, "As 4_errors_by_material, split into conversion errors (moves from won positions) and holding errors (moves from drawn positions).")

    fine = at_risk.groupby(["material", "side"])["tb_error"].agg(["sum", "count"]).unstack("side", fill_value=0)
    fine.columns = [f"{side}_{stat}" for stat, side in fine.columns]
    fine = fine.rename(columns={"player_sum": "player_errors", "player_count": "player_moves", "opponent_sum": "opponent_errors", "opponent_count": "opponent_moves"})
    fine["errors"] = fine["player_errors"] + fine["opponent_errors"]
    fine = fine.reset_index().sort_values(["errors", "material"], ascending=[False, True]).head(TOP_MATERIAL)
    fine.insert(0, "tc_group", ALL)
    fine["thin"] = fine[["player_moves", "opponent_moves"]].min(axis=1) < MIN_AT_RISK_MOVES
    fine = fine[["tc_group", "material", "player_errors", "player_moves", "opponent_errors", "opponent_moves", "errors", "thin"]]
    results.table("4c_exact_material", fine, f"All games pooled (per control the cells hold one or two errors): the {TOP_MATERIAL} exact material balances (mover's pieces first: KRvKRP = a bare rook defending against rook and pawn) with the most tablebase errors from won or drawn positions; counts only; thin = fewer than {MIN_AT_RISK_MOVES} moves on a side.")

    ep = []
    for measure in ("ep_loss", "cp_loss"):
        for v in views:
            sub = tb[tb["tc_group"].isin(v["members"])]
            c = compare_sides(sub, measure, n_boot=N_BOOT_CELLS)
            for _, r in c.iterrows():
                ep.append({**view_row(v), "measure": measure, **{k: r[k] for k in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"]}, "moves": int(r["n"]), "games": int(r["games"]), "thin": int(r["n"]) < 2 * MIN_AT_RISK_MOVES})  # both sides together
    results.table("4d_tablebase_ep_loss", pd.DataFrame(ep), f"Inside tablebase range, all positions: mean expected points lost per move (ep_loss, calibrated model) and centipawn loss (cp_loss), you vs opponents in the same games; diff = you minus opponents (games resampled); thin = fewer than {2 * MIN_AT_RISK_MOVES} moves in all.")

    big_rows = []
    for measure in ("ep_loss", "cp_loss"):
        for v in views:
            sub = big[big["tc_group"].isin(v["members"])]
            c = compare_sides(sub, measure, by=["big_class"], n_boot=N_BOOT_CELLS)
            sides = sub.groupby(["big_class", "is_player_move"])[measure].count().unstack(fill_value=0)
            for _, r in c.iterrows():
                pm, om = int(sides.loc[r["big_class"]].get(True, 0)), int(sides.loc[r["big_class"]].get(False, 0))
                big_rows.append({**view_row(v), "measure": measure, "big_class": r["big_class"], **{k: r[k] for k in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"]}, "player_moves": pm, "opponent_moves": om, "games": int(r["games"]), "thin": min(pm, om) < MIN_EP_MOVES})
    big_table = pd.DataFrame(big_rows)
    big_table["big_class"] = pd.Categorical(big_table["big_class"], BIG_CLASS_ORDER)
    big_table["_v"] = big_table["tc_group"].map({v["tc_group"]: i for i, v in enumerate(views)})
    big_table = big_table.sort_values(["measure", "_v", "big_class"]).drop(columns="_v")
    results.table("4e_big_endgames_ep_loss", big_table, f"Beyond tablebase range (endgame phase, more than {TB_PIECES} pieces): mean expected points lost per move (ep_loss) and centipawn loss (cp_loss) by coarse material class, you vs opponents in the same games; thin = fewer than {MIN_EP_MOVES} moves on either side.")

    big_m = big.assign(big_mistake=(big["ep_loss"] >= BIG_MISTAKE_EP).astype(float))
    mistakes = error_cells(big_m, "big_mistake", views, min_moves=MIN_EP_MOVES)
    results.table("4f_big_mistakes", mistakes.rename(columns={"player_errors": "player_mistakes", "opponent_errors": "opponent_mistakes"}), f"Beyond tablebase range (endgame phase, more than {TB_PIECES} pieces): moves losing at least {BIG_MISTAKE_EP} expected points, per 100 moves, you vs opponents in the same games.")

    order = class_order(views)
    xmax = x_limit([by_mat[by_mat["tc_group"].isin(order)]], "per_100_")
    fig, axes = panel_row(order, "Which tablebase endings produce the most errors?")
    for label in order:
        ax = axes[label]
        part = by_mat[by_mat["tc_group"] == label].copy()
        if part.empty or part["thin"].all():
            ax.text(0.5, 0.5, "too few moves, games\nor errors in every class", transform=ax.transAxes, ha="center", va="center", color=charts.INK_MUTED, fontsize=8.5)
            ax.set_xlim(0, xmax)
            ax.set_xlabel("errors per 100 at-risk moves")
            continue
        part["tb_class"] = part["tb_class"].astype(str)
        part.loc[part["thin"], ["per_100_player", "per_100_player_lo", "per_100_player_hi", "per_100_opponent", "per_100_opponent_lo", "per_100_opponent_hi"]] = np.nan
        charts.side_bars(ax, part, "tb_class", value="per_100_", order=TB_CLASS_ORDER, horizontal=True)
        idx = part.set_index("tb_class")
        for i, cls in enumerate(TB_CLASS_ORDER):
            if cls not in idx.index or idx.loc[cls, "thin"]:
                ax.text(0.5, i, "too few", va="center", ha="left", fontsize=7, color=charts.INK_MUTED)
        ax.set_xlim(0, xmax)
        ax.set_xlabel("errors per 100 at-risk moves")
    next(iter(axes.values())).set_ylim(len(TB_CLASS_ORDER) - 0.5, -0.5)
    next(iter(axes.values())).set_yticks(range(len(TB_CLASS_ORDER)), TB_CLASS_ORDER)
    charts.legend(fig, loc="upper right")
    fig.tight_layout()
    charts.footnote(fig, f"Tablebase errors (a move that turns a win into a draw or loss, or a draw into a loss) per 100 moves from won or drawn positions (95% CI, games resampled). 'too few': under {MIN_AT_RISK_MOVES} such moves on a side, {MIN_CELL_GAMES} games or {MIN_EVENTS} errors in all.")
    results.figure(fig, "4_errors_by_material")


# --- 10.5 how failed wins ended ------------------------------------------------------------------------------


CAUSES = ["wrong move", "lost on time", "drawn on time (insufficient material)", "on the clock, no wrong move", "other, no wrong move", "any failure"]


def cause_flags(w: pd.DataFrame) -> dict[str, pd.Series]:
    on_clock = w["cause"].isin(list(CLOCK_ENDINGS.values()))
    return {
        "wrong move": w["cause"] == "wrong move",
        CLOCK_ENDINGS["timeout"]: w["cause"] == CLOCK_ENDINGS["timeout"],
        CLOCK_ENDINGS["timevsinsufficient"]: w["cause"] == CLOCK_ENDINGS["timevsinsufficient"],
        "on the clock, no wrong move": on_clock,
        "other, no wrong move": w["cause"] == "other, no wrong move",
        "any failure": ~w["converted"],
    }


def q5_failures(results: Results, views, won, tb) -> None:
    rows = []
    for v in views:
        w = won[won["tc_group"].isin(v["members"])]
        p, o = (w[w["side"] == s] for s in SIDES)
        fp, fo = cause_flags(p), cause_flags(o)
        for cause in CAUSES:
            row = {**view_row(v), "cause": cause}
            row.update(ci_cols("per_100_player", tuple(100 * x for x in boot_mean(fp[cause].astype(float)))))
            row.update(ci_cols("per_100_opponent", tuple(100 * x for x in boot_mean(fo[cause].astype(float), seed=SEED + 1))))
            row.update(ci_cols("per_100_diff", tuple(100 * x for x in boot_diff(fp[cause].astype(float), fo[cause].astype(float)))))
            failed_p, failed_o = int((~p["converted"]).sum()), int((~o["converted"]).sum())
            row.update({
                "share_of_failures_player": fp[cause].sum() / failed_p if failed_p else np.nan, "share_of_failures_opponent": fo[cause].sum() / failed_o if failed_o else np.nan,
                "player_count": int(fp[cause].sum()), "opponent_count": int(fo[cause].sum()), "player_failed": failed_p, "opponent_failed": failed_o,
                "player_endings": len(p), "opponent_endings": len(o), "thin": min(len(p), len(o)) < MIN_ENDINGS,
            })  # fmt: skip
            rows.append(row)
    table = pd.DataFrame(rows)
    results.table(
        "5_failed_conversions", table,
        "How won tablebase endings failed, per 100 won endings of that side: 'wrong move' = the winner made at least one tablebase error; otherwise by how the game ended (lost on time; drawn on time because the opponent could not mate; other). "
        "share_of_failures = share of that side's failed conversions. You vs opponents are different games (diff CIs resample both sets).",
    )  # fmt: skip

    # Within each time class with and without increment: does the increment change the clock failures?
    contrast = []
    for v_no, v_inc in increment_pairs(views):
        for side in SIDES:
            a = won[(won["side"] == side) & won["tc_group"].isin(v_no["members"])]
            b = won[(won["side"] == side) & won["tc_group"].isin(v_inc["members"])]
            for cause in ("on the clock, no wrong move", "wrong move", "any failure"):
                fa, fb = cause_flags(a)[cause].astype(float), cause_flags(b)[cause].astype(float)
                row = {"tc_group": v_no["tc_group"], "compared_with": v_inc["tc_group"], "side": side, "cause": cause}
                row.update(ci_cols("per_100_no_increment", tuple(100 * x for x in boot_mean(fa))))
                row.update(ci_cols("per_100_with_increment", tuple(100 * x for x in boot_mean(fb, seed=SEED + 1))))
                row.update(ci_cols("per_100_diff", tuple(100 * x for x in boot_diff(fa, fb))))
                ka, kb = int(fa.sum()), int(fb.sum())
                row["wilson_no_increment_lo"], row["wilson_no_increment_hi"] = (100 * x for x in wilson(ka, len(a)))
                row["wilson_with_increment_lo"], row["wilson_with_increment_hi"] = (100 * x for x in wilson(kb, len(b)))
                row["newcombe_diff_lo"], row["newcombe_diff_hi"] = (100 * x for x in newcombe(ka, len(a), kb, len(b)))
                row.update({"count_no_increment": int(fa.sum()), "count_with_increment": int(fb.sum()), "endings_no_increment": len(a), "endings_with_increment": len(b), "thin": min(len(a), len(b)) < MIN_ENDINGS})
                contrast.append(row)
    results.table("5b_increment_contrast", pd.DataFrame(contrast), "Within a time class: won tablebase endings failed per 100, controls without increment minus controls with increment (each side separately; different games, both sets resampled). wilson_* and newcombe_diff_* are score intervals for the same per-100 rates and their difference: wider and more honest than the bootstrap when a side has only a few events.")

    at_risk = tb[tb["tb_wdl_before"] >= 0]
    clock_rows = []
    for measure, col in CLOCK_MEASURES.items():
        banded = at_risk.assign(clock_band=pd.Categorical(pd.cut(at_risk[col], CLOCK_EDGES, labels=CLOCK_LABELS, right=False).astype(str), CLOCK_LABELS))
        cells = error_cells(banded, "tb_error", views, by=["clock_band"])
        cells.insert(3, "measure", measure)
        clock_rows.append(cells)
    clock_table = pd.concat(clock_rows, ignore_index=True)
    clock_table["clock_band"] = pd.Categorical(clock_table["clock_band"].astype(str), CLOCK_LABELS)
    clock_table["_v"] = clock_table["tc_group"].map({v["tc_group"]: i for i, v in enumerate(views)})
    clock_table = clock_table.sort_values(["_v", "measure", "clock_band"]).drop(columns="_v")
    results.table("5c_errors_by_clock", clock_table, "Tablebase errors per 100 at-risk moves by the mover's clock before the move, as seconds left and as the buffer beyond one increment (equal in no-increment controls); you vs opponents in the same games.")

    entry_rows = []
    for measure, col in (("seconds left", "clock"), ("buffer beyond increment", "buffer")):
        w_all = won.assign(band=pd.cut(won[col], CLOCK_EDGES, labels=CLOCK_LABELS, right=False).astype(str))
        for v in views:
            w = w_all[w_all["tc_group"].isin(v["members"])]
            for side in SIDES:
                for band in CLOCK_LABELS:
                    part = w[(w["side"] == side) & (w["band"] == band)]
                    flags = cause_flags(part)
                    row = {**view_row(v), "measure": measure, "side": side, "winner_clock": band}
                    row.update(ci_cols("conversion", boot_mean(part["converted"].astype(float))))
                    row.update(ci_cols("clock_failure_per_100", tuple(100 * x for x in boot_mean(flags["on the clock, no wrong move"].astype(float)))))
                    row.update({"clock_failures": int(flags["on the clock, no wrong move"].sum()), "wrong_moves": int(flags["wrong move"].sum()), "endings": len(part), "thin": len(part) < MIN_ENDINGS})
                    entry_rows.append(row)
    results.table("5d_entry_clock", pd.DataFrame(entry_rows), "Won tablebase endings by the winner's clock at entry into tablebase range (seconds left and buffer beyond one increment): share converted and failures on the clock without a wrong move per 100, per side.")

    won_moves = tb[tb["tb_wdl_before"] == 2].copy()
    won_moves["dtz_band"] = pd.Categorical(pd.cut(won_moves["abs_dtz"], DTZ_EDGES, labels=DTZ_LABELS, right=False).astype(str), DTZ_LABELS)
    dtz = error_cells(won_moves[won_moves["dtz_band"].notna()], "tb_error", views, by=["dtz_band"])
    secs = []
    for v in views:
        sub = won_moves[won_moves["tc_group"].isin(v["members"])]
        c = compare_sides(sub, "time_spent_seconds", by=["dtz_band"], n_boot=N_BOOT_CELLS)
        for _, r in c.iterrows():
            secs.append({"tc_group": v["tc_group"], "dtz_band": r["dtz_band"], **{f"seconds_{k}": r[k] for k in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi"]}})
    secs = pd.DataFrame(secs).assign(dtz_band=lambda f: f["dtz_band"].astype(str))
    dtz = dtz.assign(dtz_band=dtz["dtz_band"].astype(str)).merge(secs, on=["tc_group", "dtz_band"], how="left")
    dtz["dtz_band"] = pd.Categorical(dtz["dtz_band"], DTZ_LABELS)
    dtz["_v"] = dtz["tc_group"].map({v["tc_group"]: i for i, v in enumerate(views)})
    dtz = dtz.sort_values(["_v", "dtz_band"]).drop(columns="_v")
    results.table("5e_dtz", dtz, "Won tablebase positions by DTZ (moves of best play to the next capture or pawn move that keeps the win): errors per 100 moves and seconds spent on the move, you vs opponents in the same games.")

    time_rows = []
    won_moves["clock_share_spent"] = won_moves["time_spent_seconds"] / won_moves["clock_before_seconds"].clip(lower=CLOCK_RESOLUTION_SECONDS)
    for v in views:
        sub = won_moves[won_moves["tc_group"].isin(v["members"])]
        row = view_row(v)
        for measure in ("time_spent_seconds", "clock_share_spent"):
            c = compare_sides(sub, measure, n_boot=N_BOOT_CELLS)
            if c.empty:
                continue
            r = c.iloc[0]
            for k in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"]:
                row[f"{measure}_{k}"] = r[k]
        p, o = sub[sub["is_player_move"]], sub[~sub["is_player_move"]]
        row.update({"player_moves": len(p), "opponent_moves": len(o), "games": sub["game_uuid"].nunique(), "thin": min(len(p), len(o)) < MIN_AT_RISK_MOVES})
        time_rows.append(row)
    results.table("5f_time_in_won", pd.DataFrame(time_rows), "Moves from won tablebase positions: seconds spent per move and the share of the remaining clock spent, you vs opponents in the same games.")

    # Chart: wrong move vs the clock, per 100 won endings, by increment view.
    order = increment_order(views)
    import matplotlib.pyplot as plt

    drawn = table[table["cause"].isin(["wrong move", "on the clock, no wrong move"]) & table["tc_group"].isin(order)]
    xmax = x_limit([drawn], "per_100_")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.4, 0.42 * len(order) + 1.6), sharey=True)
    fig.suptitle("How did failed wins end: a wrong move or the clock?", x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    for ax, cause, sub_title in ((ax1, "wrong move", "spoiled by a wrong move"), (ax2, "on the clock, no wrong move", "lost or drawn on the clock, no wrong move")):
        part = table[table["cause"] == cause].copy()
        part.loc[part["thin"], [f"per_100_{s}{p}" for s in SIDES for p in ("", "_lo", "_hi")]] = np.nan
        charts.side_bars(ax, part, "tc_group", value="per_100_", order=order, horizontal=True)
        idx = table[table["cause"] == cause].set_index("tc_group")
        for i, label in enumerate(order):
            if idx.loc[label, "thin"]:
                ax.text(0.5, i, f"too few ({int(idx.loc[label, 'player_endings'])} yours, {int(idx.loc[label, 'opponent_endings'])} theirs)", va="center", ha="left", fontsize=7, color=charts.INK_MUTED)
        ax.set_title(sub_title)
        ax.set_xlim(0, xmax)
        ax.set_xlabel("per 100 won endings")
        ax.axhline(len(order) - 1.5, color=charts.INK_MUTED, linewidth=0.8)
    controls_of = {v["tc_group"]: v["controls"] for v in views}
    ax1.set_yticks(range(len(order)), [f"{o}\n({controls_of[o]})" if o != ALL and " " in controls_of[o] else o for o in order])
    ax1.set_ylim(len(order) - 0.5, -0.5)
    charts.legend(fig, loc="upper right")
    charts.footnote(fig, f"Won endings: the first position with <= {TB_PIECES} pieces is a tablebase win for that side; each side's own won endings (95% CI, games resampled). 'On the clock' = lost on time, or drawn on time because the other side could not mate.")
    fig.tight_layout()
    results.figure(fig, "5_failed_conversions")


def increment_pairs(views: list[dict]) -> list[tuple[dict, dict]]:
    """(no-increment view, with-increment view) per time class that has both kinds of control."""
    by_label = {v["tc_group"]: v for v in views}
    increment = control_increments(views)
    pairs = []
    for cls, members in classes_of(views).items():
        kinds = increment_kinds(members, increment)
        if len(kinds) == 2:
            pick = [by_label.get(f"{cls} {k}", by_label[kinds[k][0]]) for k in ("no increment", "with increment")]
            pairs.append((pick[0], pick[1]))
    return pairs


def main() -> None:
    started = time.time()
    charts.style()
    data = load_data()
    increment = data.games.groupby(data.games["tc_group"].astype(str))["increment_seconds"].first().to_dict()
    views = build_views(data.tc_groups, data.tc_class, increment)
    _, tb, ending, won, big = prepare(data)
    results = Results(SLUG)
    q1_reached(results, views, data, ending, big)
    kinds = q2_conversion(results, views, won, tb)
    q3_hold(results, views, ending, kinds)
    q4_material(results, views, tb, big)
    q5_failures(results, views, won, tb)
    results.finish()
    print(f"{len(tb):,} tablebase moves in {tb['game_uuid'].nunique()} games, {int(tb['tb_error'].sum())} errors; done in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
