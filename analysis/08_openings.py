"""Q8: Opening repertoire.

Which openings the player scores and plays well in, by colour; whether staying
in book longer helps; the lines that cost most; recurring opening mistakes.

Inputs: load_data() (standard live games), data/<user>/games_decomposed.parquet
(Q1: per-game ep lost by phase, ep at move 1), opening_name / opening_eco /
book_ply on the games, in_book / phase / ep_loss on the moves.

Run from the repo root: .venv/bin/python analysis/08_openings.py
"""

from __future__ import annotations

import time

import chess
import numpy as np
import pandas as pd

from chess_analysis.toolkit import (
    SIDE_COLOURS,
    TIME_CLASSES,
    Report,
    ci_column,
    cluster_bootstrap_mean,
    compare_sides,
    elo_expected_score,
    figure_style,
    load_data,
    repo_root,
)

SLUG = "08_openings"
N_BOOT = 500
SEED = 8
MIN_GAMES = 20
MIN_MOVES = 30
COLOURS = ["white", "black"]
BOOK_BINS = [1, 3, 5, 7, 9, 99]
BOOK_LABELS = ["1-2", "3-4", "5-6", "7-8", "9+"]
RECUR_MIN_N = 5
RECUR_MIN_EP = 0.05


# --- small statistics helpers (copied in spirit from analysis/01_decomposition.py) -------


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    if pd.isna(est):
        return ""
    return f"{est:{f}} [{lo:{f}}, {hi:{f}}]" if pd.notna(lo) else f"{est:{f}}"


def game_mean(frame: pd.DataFrame, column: str, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """Mean of a per-game column with a game-bootstrap CI (each game is one cluster)."""
    return cluster_bootstrap_mean(frame[column].astype("float64"), frame["uuid"], n_boot=n_boot, seed=SEED)


def paired_diff(frame: pd.DataFrame, a: str, b: str, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """mean(a) - mean(b) over the same games, bootstrapped by game."""
    d = (frame[a].astype("float64") - frame[b].astype("float64")).dropna().to_numpy()
    if len(d) == 0:
        return float("nan"), float("nan"), float("nan")
    if len(d) < 2:
        return float(d.mean()), float("nan"), float("nan")
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    boot = d[idx].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(d.mean()), float(lo), float(hi)


def group_diff(a: np.ndarray, b: np.ndarray, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """mean(a) - mean(b) for two disjoint sets of games (independent game bootstraps)."""
    a, b = np.asarray(a, dtype="float64"), np.asarray(b, dtype="float64")
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(SEED)
    ba = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    bb = b[rng.integers(0, len(b), size=(n_boot, len(b)))].mean(axis=1)
    lo, hi = np.quantile(ba - bb, [0.025, 0.975])
    return float(a.mean() - b.mean()), float(lo), float(hi)


def mix(frame: pd.DataFrame) -> str:
    """'b/bz/r' game counts of a set of games."""
    counts = frame["time_class"].value_counts()
    return "/".join(str(int(counts.get(tc, 0))) for tc in TIME_CLASSES)


def san_of(fen: str, uci: str) -> str:
    try:
        return chess.Board(fen).san(chess.Move.from_uci(uci))
    except Exception:  # noqa: BLE001 - a non-parsable move is shown raw
        return uci or ""


def move_label(fen: str, san: str) -> str:
    """'7. Ng5' or '7... Nf6' from the position the mover faced."""
    parts = fen.split()
    number, colour = parts[5], parts[1]
    return f"{number}. {san}" if colour == "w" else f"{number}... {san}"


# --- data preparation ---------------------------------------------------------------------


def prepare(data) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    games, moves = data.games.copy(), data.moves.copy()
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet")
    games = games.merge(decomposed, on="uuid", how="left")
    info = {"all_games": len(games), "no_moves": int((games["n_plies"].fillna(0) == 0).sum())}
    games = games[games["n_plies"].fillna(0) > 0].copy()

    games["family"] = games["opening_name"].str.split(":").str[0].str.strip()
    games["colour"] = games["my_color"]
    # Opponent's pre-game rating proxy: post-game rating + my rating change (rated), post-game rating (unrated).
    rated = games["rated"].astype(bool)
    opp_pre = np.where(rated, games["opponent_rating_after"].astype("float64") + games["my_rating_change"].astype("float64"), games["opponent_rating_after"].astype("float64"))
    games["opp_rating_pre"] = opp_pre
    mine = games["my_rating_before"].astype("float64")
    valid = mine.notna() & (mine >= 100) & pd.Series(opp_pre, index=games.index).notna() & (pd.Series(opp_pre, index=games.index) >= 100)
    games["expected"] = np.where(valid, elo_expected_score(mine.fillna(0), pd.Series(opp_pre, index=games.index).fillna(0)), np.nan)
    games["overperf"] = games["my_score"] - games["expected"]
    info["no_rating"] = int((~valid).sum())

    # Position after the last book move, from the player's view.
    last_book = moves[moves["in_book"]].sort_values(["game_uuid", "ply"]).groupby("game_uuid").tail(1)
    sign = np.where(last_book["is_player_move"], 1.0, -1.0)
    after = pd.DataFrame(
        {
            "uuid": last_book["game_uuid"].to_numpy(),
            "ep_after_book": np.where(last_book["is_player_move"], last_book["ep_after"], 1 - last_book["ep_after"]),
            "cp_after_book": last_book["cp_after"].to_numpy() * sign,
        }
    )
    games = games.merge(after, on="uuid", how="left")
    games["book_gain"] = games["ep_after_book"] - games["my_ep_start"]

    # The first out-of-book move: who played it and what it cost.
    deviation = moves[moves["ply"] == moves["book_ply"] + 1]
    dev = pd.DataFrame(
        {
            "uuid": deviation["game_uuid"].to_numpy(),
            "deviator": np.where(deviation["is_player_move"], "you", "opponent"),
            "deviation_ep_loss": deviation["ep_loss"].to_numpy(),
            "deviation_cp_loss": deviation["cp_loss"].to_numpy(),
        }
    )
    games = games.merge(dev, on="uuid", how="left")
    games["book_bin"] = pd.cut(games["book_ply"].astype("float64"), bins=BOOK_BINS, labels=BOOK_LABELS, right=False)

    # First moves: the line start from the player's side.
    first = moves[moves["ply"] == 1][["game_uuid", "san"]].rename(columns={"san": "ply1"})
    second = moves[moves["ply"] == 2][["game_uuid", "san"]].rename(columns={"san": "ply2"})
    games = games.merge(first, left_on="uuid", right_on="game_uuid", how="left").drop(columns="game_uuid")
    games = games.merge(second, left_on="uuid", right_on="game_uuid", how="left").drop(columns="game_uuid")
    games["line_start"] = np.where(games["colour"] == "white", "1." + games["ply1"].fillna("?"), "1." + games["ply1"].fillna("?") + " " + games["ply2"].fillna("(no reply)"))

    # Recency: games in the 12 months before the last game, and the years a line was played.
    cutoff = games["start_datetime"].max() - pd.Timedelta(days=365)
    games["recent"] = games["start_datetime"] >= cutoff
    info["cutoff"] = cutoff.date().isoformat()

    moves = moves.merge(games[["uuid", "family", "colour", "book_bin", "year"]].rename(columns={"uuid": "game_uuid"}), on="game_uuid", how="inner")
    return games, moves, info


def years_span(years: pd.Series) -> str:
    lo, hi = int(years.min()), int(years.max())
    return f"{lo % 100:02d}" if lo == hi else f"{lo % 100:02d}–{hi % 100:02d}"


# --- report sections ----------------------------------------------------------------------


def section_intro(report: Report, games: pd.DataFrame, info: dict, username: str) -> None:
    counts = games.groupby(["time_class", "colour"]).size().unstack("colour").reindex(TIME_CLASSES)
    n_moves = info["n_moves"]
    report.text(
        f"Data: {len(games):,} standard live games with at least one move (player {username}; "
        + ", ".join(f"{tc} {int(counts.loc[tc].sum()):,} = {int(counts.loc[tc, 'white']):,} white / {int(counts.loc[tc, 'black']):,} black" for tc in TIME_CLASSES)
        + f") and {n_moves:,} analysed moves; {info['no_moves']} game(s) without moves excluded. "
        f"Every game with moves reached a named opening line (Lichess list); the deepest named line is `opening_name`, "
        f"its depth `book_ply` (median {games['book_ply'].median():.0f} plies, 75th percentile {games['book_ply'].quantile(0.75):.0f}). "
        f"Names are grouped into {games['family'].nunique()} *families* (the text before the colon: 'Italian Game: Giuoco Piano' -> 'Italian Game'). "
        f"Numbers are estimate [95% CI], CIs bootstrapped by game ({N_BOOT} resamples); each game is one cluster. "
        f"*Score* is your points per game; *expected* is the Elo expectation from your pre-game rating and the opponent's "
        f"pre-game rating reconstructed as their post-game rating plus your rating change (post-game rating in unrated games; "
        f"an approximation, {info['no_rating']} game(s) without usable ratings are left out of expected-score columns); "
        f"*over-performance* = score − expected. *Opening ep lost* is the sum of `ep_loss` over a side's moves in the material-heuristic "
        f"opening phase (roughly the first 10–12 moves), *ep after book* is the calibrated expected points for you after the last book move, "
        f"and *book gain* is that minus the model's expectation at move 1, so it measures what the known part of the opening did to your chances. "
        f"*last 12 mo* counts the games since {info['cutoff']} and *years* the span in which the line occurred (your repertoire changed: a line with no recent games is history, not a target). "
        f"Cells with fewer than {MIN_GAMES} games are left blank."
    )


def line_rows(games: pd.DataFrame, key: str, min_games: int = MIN_GAMES, with_mix: bool = True) -> pd.DataFrame:
    """Per-group repertoire statistics (score, expected, over-performance, opening ep lost both sides, eval after book)."""
    rows = []
    for name, part in games.groupby(key, observed=True):
        if len(part) < min_games:
            continue
        over = game_mean(part, "overperf")
        me, opp = game_mean(part, "my_ep_lost_opening"), game_mean(part, "opp_ep_lost_opening")
        diff = paired_diff(part, "opp_ep_lost_opening", "my_ep_lost_opening")
        gain = game_mean(part, "book_gain")
        cp = game_mean(part, "cp_after_book")
        score = game_mean(part, "my_score")
        rows.append(
            {
                key: name, "games": len(part), "recent": int(part["recent"].sum()), "years": years_span(part["year"]), "mix (b/bz/r)": mix(part) if with_mix else None,
                "score": score[0], "score_lo": score[1], "score_hi": score[2],
                "expected": float(part["expected"].mean()),
                "over": over[0], "over_lo": over[1], "over_hi": over[2],
                "deficit_points": float(-(part["overperf"].sum())),
                "my_open": me[0], "my_open_lo": me[1], "my_open_hi": me[2],
                "opp_open": opp[0], "opp_open_lo": opp[1], "opp_open_hi": opp[2],
                "open_diff": diff[0], "open_diff_lo": diff[1], "open_diff_hi": diff[2],
                "gain": gain[0], "gain_lo": gain[1], "gain_hi": gain[2],
                "cp": cp[0], "cp_lo": cp[1], "cp_hi": cp[2],
                "share_you_deviate": float((part["deviator"] == "you").mean()),
            }  # fmt: skip
        )
    return pd.DataFrame(rows)


def present(table: pd.DataFrame, key: str, label: str, with_mix: bool = True) -> pd.DataFrame:
    out = pd.DataFrame({label: table[key], "games": table["games"], "last 12 mo": table["recent"], "years": table["years"]})
    if with_mix:
        out["mix (b/bz/r)"] = table["mix (b/bz/r)"]
    out["score"] = ci_column(table, "score", "score_lo", "score_hi", ".2f")
    out["expected"] = table["expected"].map(lambda v: f"{v:.2f}")
    out["score − expected"] = ci_column(table, "over", "over_lo", "over_hi", ".3f")
    out["your opening ep lost"] = ci_column(table, "my_open", "my_open_lo", "my_open_hi", ".3f")
    out["theirs"] = ci_column(table, "opp_open", "opp_open_lo", "opp_open_hi", ".3f")
    out["theirs − yours"] = ci_column(table, "open_diff", "open_diff_lo", "open_diff_hi", ".3f")
    out["book gain (ep)"] = ci_column(table, "gain", "gain_lo", "gain_hi", ".3f")
    out["cp after book"] = ci_column(table, "cp", "cp_lo", "cp_hi", ".0f")
    return out


def section_shape(report: Report, games: pd.DataFrame) -> dict:
    report.heading("1. The shape of the repertoire: first moves")
    report.text(
        "As White the line start is your first move; as Black it is the opponent's first move and your reply. "
        "Opening ep lost is per game for the opening phase, yours and the opponents' in the same games (theirs − yours positive = they gave away more)."
    )
    out = {}
    for colour in COLOURS:
        part = games[games["colour"] == colour]
        table = line_rows(part, "line_start").sort_values("games", ascending=False)
        out[colour] = table
        report.heading(f"{colour.capitalize()} ({len(part):,} games)", level=3)
        report.table(present(table, "line_start", "line start"))
        total = part["line_start"].isin(table["line_start"]).mean()
        report.text(f"Lines with ≥ {MIN_GAMES} games cover {total:.1%} of your {colour} games; the overall {colour} over-performance is {fmt(*game_mean(part, 'overperf'))}.")
    return out


def section_repertoire(report: Report, games: pd.DataFrame) -> dict[str, pd.DataFrame]:
    report.heading("2. Repertoire table by opening family and colour")
    report.text(
        f"Families with ≥ {MIN_GAMES} games, all time classes pooled (the mix column gives bullet/blitz/rapid counts), sorted by frequency. "
        "`score − expected` is the paired per-game mean of your score minus the Elo expectation. "
        "`book gain` is your expected points after the last book move minus the model's expectation at move 1 (positive = the known part of the line left you better off than the ratings predicted)."
    )
    out = {}
    for colour in COLOURS:
        part = games[games["colour"] == colour]
        table = line_rows(part, "family").sort_values("games", ascending=False)
        out[colour] = table
        covered = part["family"].isin(table["family"]).mean()
        report.heading(f"{colour.capitalize()}: {len(table)} families covering {covered:.0%} of {len(part):,} games", level=3)
        report.table(present(table, "family", "family"))
    # per time class, compact
    report.heading("By time class: score − expected per family", level=3)
    report.text(f"Same families, one column per time class; a cell needs ≥ {MIN_GAMES} games. Opening ep lost difference (theirs − yours) in the second block.")
    for colour in COLOURS:
        part = games[games["colour"] == colour]
        fams = out[colour]["family"].tolist()
        rows = []
        for fam in fams:
            row = {"family": fam}
            row_d = {"family": fam}
            for tc in TIME_CLASSES:
                cell = part[(part["family"] == fam) & (part["time_class"] == tc)]
                if len(cell) >= MIN_GAMES:
                    row[f"{tc} (n)"] = f"{fmt(*game_mean(cell, 'overperf'))} ({len(cell)})"
                    row_d[f"{tc} (n)"] = f"{fmt(*paired_diff(cell, 'opp_ep_lost_opening', 'my_ep_lost_opening'))} ({len(cell)})"
                else:
                    row[f"{tc} (n)"] = ""
                    row_d[f"{tc} (n)"] = ""
            rows.append((row, row_d))
        table_over = pd.DataFrame([r for r, _ in rows])
        table_diff = pd.DataFrame([d for _, d in rows])
        keep = table_over.drop(columns="family").apply(lambda r: any(v != "" for v in r), axis=1)
        report.text(f"**{colour.capitalize()}, score − expected:**")
        report.table(table_over[keep])
        report.text(f"**{colour.capitalize()}, opening ep lost, theirs − yours:**")
        report.table(table_diff[keep])
    return out


def figure_repertoire(report: Report, tables: dict[str, pd.DataFrame]) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
    for ax, colour in zip(axes, COLOURS):
        t = tables[colour]
        ax.axhline(0, color="grey", lw=0.8)
        ax.axvline(0, color="grey", lw=0.8)
        ax.errorbar(t["open_diff"], t["over"], yerr=[t["over"] - t["over_lo"], t["over_hi"] - t["over"]], xerr=[t["open_diff"] - t["open_diff_lo"], t["open_diff_hi"] - t["open_diff"]], fmt="none", ecolor="#bbbbbb", elinewidth=0.7, zorder=1)
        ax.scatter(t["open_diff"], t["over"], s=np.sqrt(t["games"]) * 6, color=SIDE_COLOURS["player"], alpha=0.75, zorder=2)
        for _, r in t.iterrows():
            if r["games"] >= 200 or r["over_lo"] > 0 or r["over_hi"] < 0:
                ax.annotate(r["family"].replace(" Defense", " Def.").replace(" Opening", " Op."), (r["open_diff"], r["over"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
        ax.set_title(f"{colour.capitalize()} (bubble area ~ games)")
        ax.set_xlabel("opening-phase ep lost, opponents − you (per game)")
    axes[0].set_ylabel("score − Elo expectation (per game)")
    fig.tight_layout()
    report.figure(fig, "repertoire_map", "Each bubble is an opening family with ≥ 20 games: vertical = how far your score exceeds the rating expectation, horizontal = how much more the opponents give away than you in the opening phase. Bars are 95% CIs; labelled: families with ≥ 200 games or a CI that excludes zero.")
    plt.close(fig)


def section_book_depth(report: Report, games: pd.DataFrame) -> dict:
    report.heading("3. Does staying in book longer help?")
    report.text(
        "Games binned by `book_ply`, the depth of the deepest named line reached (a joint property of both players: a game leaves the list when either side plays something unnamed). "
        "Per bin: your score vs expectation, the opening-phase ep lost by each side, and the expected points after the last book move minus the move-1 expectation (`book gain`)."
    )
    rows, out = [], {}
    for tc in TIME_CLASSES:
        for b in BOOK_LABELS:
            cell = games[(games["time_class"] == tc) & (games["book_bin"] == b)]
            if len(cell) < MIN_GAMES:
                continue
            over, me, opp = game_mean(cell, "overperf"), game_mean(cell, "my_ep_lost_opening"), game_mean(cell, "opp_ep_lost_opening")
            diff, gain = paired_diff(cell, "opp_ep_lost_opening", "my_ep_lost_opening"), game_mean(cell, "book_gain")
            rows.append(
                {
                    "time class": tc, "book plies": b, "games": len(cell), "white share": f"{(cell['colour'] == 'white').mean():.2f}",
                    "score": f"{cell['my_score'].mean():.2f}", "expected": f"{cell['expected'].mean():.2f}", "score − expected": fmt(*over),
                    "your opening ep lost": fmt(*me), "theirs": fmt(*opp), "theirs − yours": fmt(*diff), "book gain (ep)": fmt(*gain),
                    "you left book first": f"{(cell['deviator'] == 'you').mean():.2f}",
                }  # fmt: skip
            )
            out[(tc, b)] = over
    report.table(pd.DataFrame(rows))

    # correlation-style summary: slope of over-performance on book_ply per time class
    report.heading("Who leaves the book first, and what it costs", level=3)
    report.text(
        "The first move after the last book move is the *deviation*. Below, per time class: how often you were the one to deviate, the ep_loss of the deviating move for each side "
        "(game-bootstrap CIs; the two sets of games are disjoint, so the difference is an independent bootstrap), and your score vs expectation in the games where you deviated vs where the opponent did."
    )
    rows = []
    dev_out = {}
    for tc in TIME_CLASSES:
        part = games[(games["time_class"] == tc) & games["deviator"].notna()]
        you, opp = part[part["deviator"] == "you"], part[part["deviator"] == "opponent"]
        d_ep = group_diff(you["deviation_ep_loss"], opp["deviation_ep_loss"])
        d_over = group_diff(you["overperf"], opp["overperf"])
        share = cluster_bootstrap_mean((part["deviator"] == "you").astype(float), part["uuid"], n_boot=N_BOOT, seed=SEED)
        rows.append(
            {
                "time class": tc, "games": len(part), "you deviated first": fmt(*share),
                "your deviation ep_loss": fmt(*game_mean(you, "deviation_ep_loss")), "their deviation ep_loss": fmt(*game_mean(opp, "deviation_ep_loss")),
                "yours − theirs": fmt(*d_ep),
                "your deviation cp_loss": fmt(*game_mean(you, "deviation_cp_loss"), f=".0f"), "their deviation cp_loss": fmt(*game_mean(opp, "deviation_cp_loss"), f=".0f"),
                "score − expected when you deviate": fmt(*game_mean(you, "overperf")), "when they deviate": fmt(*game_mean(opp, "overperf")), "difference": fmt(*d_over),
            }  # fmt: skip
        )
        dev_out[tc] = {"share": share, "d_ep": d_ep, "d_over": d_over}
    report.table(pd.DataFrame(rows))
    return {"bins": out, "dev": dev_out}


def figure_book_depth(report: Report, games: pd.DataFrame) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    colours_tc = {"bullet": "#1f6f8b", "blitz": "#c46a2d", "rapid": "#6a994e"}
    x = np.arange(len(BOOK_LABELS))
    for k, tc in enumerate(TIME_CLASSES):
        ys, los, his, gs, glo, ghi = [], [], [], [], [], []
        for b in BOOK_LABELS:
            cell = games[(games["time_class"] == tc) & (games["book_bin"] == b)]
            if len(cell) < MIN_GAMES:
                ys.append(np.nan), los.append(np.nan), his.append(np.nan), gs.append(np.nan), glo.append(np.nan), ghi.append(np.nan)
                continue
            e, lo, hi = game_mean(cell, "overperf")
            ys.append(e), los.append(lo), his.append(hi)
            e, lo, hi = game_mean(cell, "book_gain")
            gs.append(e), glo.append(lo), ghi.append(hi)
        ys, los, his, gs, glo, ghi = map(np.array, (ys, los, his, gs, glo, ghi))
        off = (k - 1) * 0.18
        axes[0].errorbar(x + off, ys, yerr=[ys - los, his - ys], fmt="o-", color=colours_tc[tc], label=tc, capsize=2, lw=1)
        axes[1].errorbar(x + off, gs, yerr=[gs - glo, ghi - gs], fmt="o-", color=colours_tc[tc], label=tc, capsize=2, lw=1)
    for ax, title, ylab in zip(axes, ["Score − Elo expectation", "Expected points after book − at move 1"], ["points per game", "expected points"]):
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(BOOK_LABELS)
        ax.set_xlabel("book depth (plies)")
        ax.set_title(title)
        ax.set_ylabel(ylab)
    axes[0].legend()
    fig.tight_layout()
    report.figure(fig, "book_depth", "Your result relative to expectation (left) and the model's view of the position after the book moves (right) by how deep the game stayed in named theory, per time class. 95% CIs by game.")
    plt.close(fig)


def section_opening_quality(report: Report, moves: pd.DataFrame) -> dict:
    report.heading("4. Move quality in the opening phase, you vs opponents")
    report.text(
        "Opening-phase moves (material heuristic) by colour and time class: mean `ep_loss` per move (main), the share of moves losing ≥ 0.1 ep, and `cp_loss` (secondary). "
        "The difference is you − opponents within the same games (negative = you lose less)."
    )
    opening = moves[moves["phase"] == "opening"].copy()
    opening["big"] = (opening["ep_loss"] >= 0.1).astype(float)
    out = {}
    rows = []
    ep = compare_sides(opening, "ep_loss", by=["time_class", "colour"], n_boot=N_BOOT)
    big = compare_sides(opening, "big", by=["time_class", "colour"], n_boot=N_BOOT)
    cp = compare_sides(opening, "cp_loss", by=["time_class", "colour"], n_boot=N_BOOT)
    for tc in TIME_CLASSES:
        for colour in COLOURS:
            e = ep[(ep["time_class"] == tc) & (ep["colour"] == colour)].iloc[0]
            b = big[(big["time_class"] == tc) & (big["colour"] == colour)].iloc[0]
            c = cp[(cp["time_class"] == tc) & (cp["colour"] == colour)].iloc[0]
            out[(tc, colour)] = e
            rows.append(
                {
                    "time class": tc, "colour": colour, "games": int(e["games"]), "moves": int(e["n"]),
                    "your ep_loss": fmt(e["player"], e["player_lo"], e["player_hi"], ".4f"), "their ep_loss": fmt(e["opponent"], e["opponent_lo"], e["opponent_hi"], ".4f"),
                    "you − them": fmt(e["diff"], e["diff_lo"], e["diff_hi"], ".4f"),
                    "your share ≥ 0.1": fmt(b["player"], b["player_lo"], b["player_hi"], ".3f"), "their share ≥ 0.1": fmt(b["opponent"], b["opponent_lo"], b["opponent_hi"], ".3f"),
                    "your cp_loss": fmt(c["player"], c["player_lo"], c["player_hi"], ".1f"), "their cp_loss": fmt(c["opponent"], c["opponent_lo"], c["opponent_hi"], ".1f"),
                    "cp you − them": fmt(c["diff"], c["diff_lo"], c["diff_hi"], ".1f"),
                }  # fmt: skip
            )
    report.table(pd.DataFrame(rows))

    # by ply, pooled time classes, per colour
    early = moves[moves["ply"] <= 30]
    by_ply = compare_sides(early, "ep_loss", by=["colour", "ply"], n_boot=300)
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, colour in zip(axes, COLOURS):
        t = by_ply[by_ply["colour"] == colour].sort_values("ply")
        mine_plies = t[(t["ply"] % 2 == 1) == (colour == "white")]
        theirs_plies = t[(t["ply"] % 2 == 1) != (colour == "white")]
        ax.fill_between(mine_plies["ply"], mine_plies["player_lo"], mine_plies["player_hi"], color=SIDE_COLOURS["player"], alpha=0.2)
        ax.plot(mine_plies["ply"], mine_plies["player"], "o-", color=SIDE_COLOURS["player"], label="you", ms=3)
        ax.fill_between(theirs_plies["ply"], theirs_plies["opponent_lo"], theirs_plies["opponent_hi"], color=SIDE_COLOURS["opponent"], alpha=0.2)
        ax.plot(theirs_plies["ply"], theirs_plies["opponent"], "o-", color=SIDE_COLOURS["opponent"], label="opponents", ms=3)
        ax.set_title(f"You as {colour}")
        ax.set_xlabel("ply (half-move)")
    axes[0].set_ylabel("mean ep_loss per move")
    axes[0].legend()
    fig.tight_layout()
    report.figure(fig, "ep_loss_by_ply", "Mean ep_loss per move over the first 30 plies, you vs opponents, by your colour (all time classes pooled; shaded 95% CIs by game). Each side's moves sit on alternate plies.")
    plt.close(fig)
    # where in the first 30 plies the gap to the opponents sits: ply bins by colour and time class
    report.text("The same comparison by ply bin (each side's own moves in that bin), per colour and time class; `you − them` in ep per move:")
    early = early.copy()
    early["plies"] = pd.cut(early["ply"], bins=[1, 11, 21, 31], labels=["1-10", "11-20", "21-30"], right=False)
    by_bin = compare_sides(early, "ep_loss", by=["colour", "time_class", "plies"], n_boot=N_BOOT)
    rows = []
    for colour in COLOURS:
        for tc in TIME_CLASSES:
            row = {"colour": colour, "time class": tc}
            for b in ["1-10", "11-20", "21-30"]:
                r = by_bin[(by_bin["colour"] == colour) & (by_bin["time_class"] == tc) & (by_bin["plies"] == b)]
                if len(r) and r.iloc[0]["n"] >= MIN_MOVES:
                    r = r.iloc[0]
                    row[f"plies {b}: you / them"] = f"{r['player']:.4f} / {r['opponent']:.4f}"
                    row[f"plies {b}: you − them"] = fmt(r["diff"], r["diff_lo"], r["diff_hi"], ".4f")
                else:
                    row[f"plies {b}: you / them"] = ""
                    row[f"plies {b}: you − them"] = ""
            rows.append(row)
    report.table(pd.DataFrame(rows))
    return out


def recurring(moves: pd.DataFrame, games: pd.DataFrame, side_player: bool, top: int = 15, min_n: int = RECUR_MIN_N, min_ep: float = RECUR_MIN_EP) -> pd.DataFrame:
    """Repeated (position, move) pairs in the opening phase with the largest total ep_loss (mean >= min_ep, n >= min_n)."""
    part = moves[(moves["is_player_move"] == side_player) & (moves["phase"] == "opening")]
    grp = part.groupby(["fen_before", "san"], sort=False)
    agg = grp.agg(n=("ep_loss", "size"), total=("ep_loss", "sum"), mean_ep=("ep_loss", "mean"), mean_cp=("cp_loss", "mean"), cp_before=("cp_before", "mean"), best=("best_move", lambda s: s.mode().iloc[0] if len(s.mode()) else ""), colour=("colour", lambda s: s.mode().iloc[0]), family=("family", lambda s: s.mode().iloc[0]), years=("year", years_span)).reset_index()
    agg = agg[(agg["n"] >= min_n) & (agg["mean_ep"] >= min_ep)].sort_values("total", ascending=False).head(top)
    rows = []
    for r in agg.itertuples():
        occ = part[(part["fen_before"] == r.fen_before) & (part["san"] == r.san)]
        e, lo, hi = cluster_bootstrap_mean(occ["ep_loss"], occ["game_uuid"], n_boot=N_BOOT, seed=SEED)
        # score in those games
        uuids = occ["game_uuid"].unique()
        gs = games[games["uuid"].isin(uuids)]
        rows.append(
            {
                "colour": r.colour, "family": r.family, "move": move_label(r.fen_before, r.san), "best": san_of(r.fen_before, r.best),
                "times": int(r.n), "years": r.years, "total ep lost": round(float(r.total), 2), "ep_loss per occurrence": fmt(e, lo, hi), "cp_loss": f"{r.mean_cp:.0f}",
                "eval before (cp, mover)": f"{r.cp_before:+.0f}", "score in those games": f"{gs['my_score'].mean():.2f}", "expected": f"{gs['expected'].mean():.2f}",
            }  # fmt: skip
        )
    return pd.DataFrame(rows)


def section_fix(report: Report, games: pd.DataFrame, moves: pd.DataFrame) -> dict:
    report.heading("5. Fix these lines")
    report.text(
        f"Named lines (`opening_name`, the deepest named line reached) with ≥ {MIN_GAMES} games, per colour. *Deficit* is the total points below rating expectation accumulated in that line "
        "(games × (expected − score)); lines are ranked by it, and the per-game `score − expected` CI says how sure the deficit is. "
        "The opening ep columns show whether the problem is in the opening phase itself (you give away more than the opponents in the same games) or later. "
        "The best lines by surplus follow for contrast."
    )
    out = {}
    for colour in COLOURS:
        part = games[games["colour"] == colour]
        table = line_rows(part, "opening_name", with_mix=True)
        out[colour] = table
        worst = table.sort_values("deficit_points", ascending=False).head(8)
        best = table.sort_values("deficit_points", ascending=True).head(5)
        report.heading(f"{colour.capitalize()}: {len(table)} lines with ≥ {MIN_GAMES} games; the 8 with the largest deficit", level=3)
        shown = present(worst, "opening_name", "line")
        shown.insert(2, "deficit (points)", worst["deficit_points"].map(lambda v: f"{v:+.1f}"))
        report.table(shown)
        report.text(f"**{colour.capitalize()}: the 5 lines with the largest surplus**")
        shown = present(best, "opening_name", "line")
        shown.insert(2, "deficit (points)", best["deficit_points"].map(lambda v: f"{v:+.1f}"))
        report.table(shown)

        # recurring mistakes inside the worst lines
        report.text(f"**{colour.capitalize()}: the recurring move inside each deficit line.** For each of the 8 lines above, your repeated (position, move) pair in the opening phase with the largest total ep_loss (played ≥ 3 times, mean ep_loss ≥ {RECUR_MIN_EP}); blank if none.")
        rows = []
        for line in worst["opening_name"]:
            uuids = part.loc[part["opening_name"] == line, "uuid"]
            mv = moves[moves["game_uuid"].isin(uuids) & moves["is_player_move"] & (moves["phase"] == "opening")]
            agg = mv.groupby(["fen_before", "san"]).agg(n=("ep_loss", "size"), total=("ep_loss", "sum"), mean_ep=("ep_loss", "mean"), mean_cp=("cp_loss", "mean"), best=("best_move", lambda s: s.mode().iloc[0] if len(s.mode()) else ""), years=("year", years_span)).reset_index()
            agg = agg[(agg["n"] >= 3) & (agg["mean_ep"] >= RECUR_MIN_EP)].sort_values("total", ascending=False)
            if agg.empty:
                rows.append({"line": line, "games": int(len(uuids)), "move": "", "best": "", "times": "", "years": "", "mean ep_loss": "", "mean cp_loss": ""})
                continue
            r = agg.iloc[0]
            rows.append({"line": line, "games": int(len(uuids)), "move": move_label(r["fen_before"], r["san"]), "best": san_of(r["fen_before"], r["best"]), "times": int(r["n"]), "years": r["years"], "mean ep_loss": f"{r['mean_ep']:.3f}", "mean cp_loss": f"{r['mean_cp']:.0f}"})
        report.table(pd.DataFrame(rows))
    return out


def section_recurring(report: Report, games: pd.DataFrame, moves: pd.DataFrame) -> dict:
    report.heading("6. Recurring opening mistakes")
    report.text(
        f"Across all games: the (position, move) pairs you have played at least {RECUR_MIN_N} times in the opening phase with mean `ep_loss` ≥ {RECUR_MIN_EP}, ranked by total expected points lost. "
        "`best` is Stockfish's move in that position; `eval before` is the engine's view (centipawns, from your side) before the move; the last two columns are your score and rating expectation in the games where the position arose. "
        "The second table is the mirror image: the opponents' most expensive repeated moves against you, i.e. the traps and inaccuracies your repertoire keeps provoking."
    )
    mine = recurring(moves, games, side_player=True)
    report.text("**Yours:**")
    report.table(mine)
    theirs = recurring(moves, games, side_player=False)
    report.text("**Opponents' (same filters):**")
    report.table(theirs)
    return {"mine": mine, "theirs": theirs}


def section_meaning(report: Report, games: pd.DataFrame, shape: dict, rep: dict, book: dict, quality: dict, fix: dict, recur: dict) -> None:
    report.heading("What this means for the player")
    white_over = game_mean(games[games["colour"] == "white"], "overperf")
    black_over = game_mean(games[games["colour"] == "black"], "overperf")
    # families with the clearest signal: CI excludes zero
    good, bad = [], []
    for colour in COLOURS:
        t = rep[colour]
        for r in t.itertuples():
            tag = f"{fmt(r.over, r.over_lo, r.over_hi, '.2f')}, {r.games} games, {r.recent} in the last 12 months"
            if r.over_lo > 0:
                good.append(f"{r.family} as {colour} ({tag})")
            if r.over_hi < 0:
                bad.append(f"{r.family} as {colour} ({tag})")
    # opening-phase quality summary
    qcells = [f"{tc}/{c}: {q['diff']:+.4f} [{q['diff_lo']:+.4f}, {q['diff_hi']:+.4f}]" for (tc, c), q in quality.items()]
    worse = [k for k, q in quality.items() if q["diff_lo"] > 0]
    better = [k for k, q in quality.items() if q["diff_hi"] < 0]
    # book depth: deepest vs shallowest bin per tc
    book_txt = []
    for tc in TIME_CLASSES:
        bins = {b: v for (t, b), v in book["bins"].items() if t == tc}
        if bins:
            first, last = list(bins.items())[0], list(bins.items())[-1]
            book_txt.append(f"{tc}: {first[0]} plies {fmt(*first[1], f='.2f')} vs {last[0]} plies {fmt(*last[1], f='.2f')}")
    dev = book["dev"]
    worst_white = fix["white"].sort_values("deficit_points", ascending=False).head(3)
    worst_black = fix["black"].sort_values("deficit_points", ascending=False).head(3)
    stale = [f"{r.opening_name} ({c})" for c, t in (("White", worst_white), ("Black", worst_black)) for r in t.itertuples() if r.recent == 0]
    for colour in COLOURS:
        t = fix[colour].sort_values("deficit_points", ascending=False).head(8).iloc[3:]
        stale += [f"{r.opening_name} ({colour.capitalize()})" for r in t.itertuples() if r.recent == 0]
    mine = recur["mine"]
    # the most expensive repeated move that is still part of the repertoire (played in the last two calendar years)
    last_year = int(games["year"].max())
    current = mine[mine["years"].str[-2:].astype(int) + 2000 >= last_year - 1] if len(mine) else mine
    top_mistake = current.iloc[0] if len(current) else None
    report.text(
        f"Your repertoire is narrow and that is fine: 1.e4 as White ({(games.loc[games['colour'] == 'white', 'ply1'] == 'e4').mean():.0%} of white games) and 1...e5 / 1...d5 as Black. "
        f"Over all games you score above the rating expectation as White ({fmt(*white_over)}) and as Black ({fmt(*black_over)}); the openings rarely explain much of that: "
        f"the per-family over-performance CIs mostly straddle zero. Families where the CI is clearly positive: {'; '.join(good) if good else 'none'}. Clearly negative: {'; '.join(bad) if bad else 'none'}. "
        f"In the opening phase itself you give away about as much as the opponents (you − them per move, {'; '.join(qcells)}); "
        f"cells where you are measurably worse: {', '.join(f'{tc} {c}' for tc, c in worse) if worse else 'none'}; measurably better: {', '.join(f'{tc} {c}' for tc, c in better) if better else 'none'}. "
        f"Staying in book longer is not a lever: score − expectation by book depth ({'; '.join(book_txt)}), and you are the one who leaves the book first in "
        f"{dev['bullet']['share'][0]:.0%} / {dev['blitz']['share'][0]:.0%} / {dev['rapid']['share'][0]:.0%} of bullet / blitz / rapid games, and your deviating move is not the worse one: its ep_loss minus that of the opponents' deviations is "
        f"{dev['bullet']['d_ep'][0]:+.3f} [{dev['bullet']['d_ep'][1]:+.3f}, {dev['bullet']['d_ep'][2]:+.3f}] in bullet "
        f"({dev['blitz']['d_ep'][0]:+.3f} blitz, {dev['rapid']['d_ep'][0]:+.3f} rapid), i.e. you leave the named lines by choice, not by blundering. "
        f"The actionable part is section 5 and 6: the lines with the largest deficits (White: {', '.join(worst_white['opening_name'])}; Black: {', '.join(worst_black['opening_name'])}) "
        + (f"and a handful of repeated moves still in use, led by {top_mistake['move']} in the {top_mistake['family']} as {top_mistake['colour']} ({top_mistake['times']} times over {top_mistake['years']}, {top_mistake['ep_loss per occurrence']} ep each; the engine prefers {top_mistake['best']}). " if top_mistake is not None else ". ")
        + (f"Check the years column before working on a line: of the deficit lines above, {', '.join(stale)} had no game in the last 12 months and belong to an earlier repertoire. " if stale else "")
        + "Learning the engine's reply in the few recurring positions is the cheapest opening work available; the rest of the result is decided after the opening."
    )


def transposition_note(games: pd.DataFrame, moves: pd.DataFrame) -> str:
    """Families whose label does not match the move order (names are assigned by position), e.g. 'Zukertort Opening' reached via 1.e4 d5 2.Nf3."""
    notes = []
    for colour in COLOURS:
        part = games[games["colour"] == colour]
        for fam, grp in part.groupby("family"):
            if len(grp) < MIN_GAMES:
                continue
            first = grp["ply1"].mode().iloc[0]
            conventional = {"Zukertort Opening": "Nf3", "Nimzo-Larsen Attack": "b3", "English Opening": "c4", "Bird Opening": "f4", "Hungarian Opening": "g3", "Van't Kruijs Opening": "e3", "Mieses Opening": "d3"}
            if fam in conventional and first != conventional[fam]:
                seq = moves[moves["game_uuid"].isin(grp["uuid"]) & (moves["ply"] <= 4)].sort_values(["game_uuid", "ply"]).groupby("game_uuid")["san"].apply(" ".join).mode().iloc[0]
                notes.append(f"'{fam}' as {colour} ({len(grp)} games) mostly began {seq}")
    return "; ".join(notes)


def section_caveats(report: Report, info: dict, games: pd.DataFrame, moves: pd.DataFrame) -> None:
    report.heading("Caveats")
    trans = transposition_note(games, moves)
    report.text(
        "\n".join(
            [
                "- Opening names come from the Lichess list and at this level describe the first 2–4 moves (median book depth 4 plies); 'family' is the text before the colon. Treat the tables as repertoire shape, not theory.",
                "- Names are assigned by position, so transpositions carry labels that do not match the move order" + (f": {trans}." if trans else "."),
                f"- Expected score is the Elo formula on chess.com ratings, with the opponent's pre-game rating reconstructed as post-game rating + your rating change ({info['no_rating']} game(s) with unusable ratings dropped from those columns). Chess.com uses Glicko, so the expectation is approximate; the over-performance baseline over all games is positive (Q1), so compare families with each other and with the colour's overall value, not with zero.",
                "- Book depth is a joint property of both players and correlates with the opening family and with the opponents' knowledge; a deep book line is not something you can choose unilaterally. No rating adjustment beyond the expected-score control is made.",
                "- The opening phase is the material heuristic (roughly the first 10–12 moves), which extends well beyond book; `ep_loss` is relative to Stockfish 17.1 at 1M nodes, so some 'losses' are moves no human would avoid.",
                "- `ep after book` and `book gain` come from the pooled calibrated model (cross-validated Brier 0.179), which knows ratings, clocks and the time control; it is under-confident at the extremes and both players are judged by the same model.",
                "- Per-line and per-cell numbers rest on 20–200 games; CIs are wide and many comparisons are made, so expect a few CIs to exclude zero by chance. Rank by deficit is descriptive.",
                "- Recurring mistakes are identified by exact position (`fen_before`) and move; transpositions into the same position are counted together, slightly different positions separately. The engine's best move is one line, not the only good move.",
                "- Opponents are matched by rating, so 'their' numbers describe the field at your level in the same games, not an absolute standard.",
                f"- Bootstrap CIs use {N_BOOT} resamples by game (300 for the per-ply figure); each game is one cluster.",
            ]
        )
    )


def main() -> None:
    t0 = time.time()
    figure_style()
    data = load_data()
    games, moves, info = prepare(data)
    info["n_moves"] = len(moves)
    report = Report(SLUG, "Opening repertoire: where the score comes from, by colour")
    section_intro(report, games, info, data.username)
    shape = section_shape(report, games)
    rep = section_repertoire(report, games)
    figure_repertoire(report, rep)
    book = section_book_depth(report, games)
    figure_book_depth(report, games)
    quality = section_opening_quality(report, moves)
    fix = section_fix(report, games, moves)
    recur = section_recurring(report, games, moves)
    section_meaning(report, games, shape, rep, book, quality, fix, recur)
    section_caveats(report, info, games, moves)
    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
