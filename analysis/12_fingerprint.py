"""Question 12: style fingerprint. Can a classifier pick your games out, and on what?

Two comparisons, both on one feature row per player-game (clock-usage
profile, move-kind rates, opening choice by colour, book depth, game length,
plus the engine error profile where available; never the result, ratings,
names, colour or time control):

1. You vs your opponents in the same games (`step 1`): every game gives one
   row for each side; regularised logistic regression and gradient boosting,
   cross-validated 5-fold by game (both rows of a game in the same fold),
   with each feature group dropped in turn, and trained on other years and
   tested on an unseen one.
2. You vs strangers of your rating (`step 2`): engine-free features for
   games of your 200 most frequent opponents against third parties
   (data/opponents/fingerprint_features.parquet, committed). Rating-band
   matched pooled fits (a test stranger is never seen in training), the
   per-pair test (you vs each stranger against stranger vs stranger), the
   "vs the rest" baseline at equal sample size, and the habits that differ.
   A fit is made per exact time control where both you and the strangers
   have at least `MIN_FIT_GAMES` matched games; every time class is also
   fitted with your games matched to strangers' games on rating band *and*
   exact control (the fallback for the controls without their own fit), and
   on rating band only (to show how much of the clock signal is increment).

Every table goes to presentation/data/12_fingerprint/ and every chart to
presentation/figures/12_fingerprint/; the reading is in
presentation/README.md. No prose is written here.

Run from the repo root: .venv/bin/python analysis/12_fingerprint.py
(about 9-10 minutes, single-threaded on purpose: multi-threaded boosting is
much slower on a shared machine and is not needed for determinism). --rebuild-features recomputes the stranger
feature table from the raw opponent archives under data/opponents/<name>/raw/
(not committed; scripts/download_opponents.py fetches them, ~1 h) and takes
about 17 minutes more.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import chess
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold, StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from chess_analysis import charts
from chess_analysis.config import load_settings
from chess_analysis.dataset import build_tables
from chess_analysis.enrich import PIECE_NAMES, _time_columns, parse_time_control, phase
from chess_analysis.openings import game_openings, load_book
from chess_analysis.parse import parse_game
from chess_analysis.toolkit import MIN_GROUP_GAMES, TIME_CLASSES, Results, compare_sides, load_data, opponent_rating_pre, rating_band, repo_root, tc_group

SLUG = "12_fingerprint"
SEED = 12
N_BOOT = 500  # bootstrap resamples for every CI (AUC CIs move by < 0.002 beyond this)
N_FOLDS = 5  # cross-validation folds: 80% of the data trains each model, enough for the smallest fits
MIN_OWN_MOVES = 8  # a side with fewer moves has no usable clock or move-kind profile (mostly early resignations)
CAP_PER_CLASS = 120  # stranger games sampled per time class when the feature table is rebuilt (download budget)
# A model fit (step 1 per control, step 2 per exact control) needs this many games (step 2: band-matched, on
# each side). Below it the training folds hold fewer than 120 games, a 5-fold AUC's CI is about +-0.07 or wider,
# and every step-1 control under it scored 0.48-0.71 against 0.84+ above it: the model is short of data, which
# says nothing about how you play there. Step-1 controls below it are marked thin and not drawn.
MIN_FIT_GAMES = 150
# Unseen-year test: a test year needs this many games in the control to get its own AUC (CI about +-0.1)...
MIN_YEAR_TEST_GAMES = 50
# ...and the other years this many to train on (with fewer, boosting predicted a constant: AUC 0.5).
MIN_YEAR_TRAIN_GAMES = 100
MIN_MATCHED = 30  # games per side for one person-vs-person classifier (per-pair test and "vs the rest"); old default
MIN_STRANGERS = 20  # strangers needed before their AUC distribution (mean, quartiles, percentile) is read
N_PARTNERS = 2  # stranger-vs-stranger pairs per stranger (two keeps the run under 20 minutes)
N_ME_DRAWS = 20  # random subsamples of your games in the "vs the rest" baseline (their 2.5-97.5% range is your interval)
REST_RATIO = 3  # "vs the rest": up to this many rest-of-pool games per game of the person (more negatives, same bands)
BAND = 100  # rating-band width for matching: +-50 points is about a 57/43 expected score, close to even
GOOD_AUC = 0.7  # conventional threshold for "acceptable" discrimination (Hosmer & Lemeshow); share of pairs above it
TOP_HABITS = 8  # habits drawn in the chart: the most important features of the matched time-class fits
TOP_IMPORTANCE = 12  # features kept per fit in the importance table
BOOK_MAX_PLY = 40  # Lichess named lines never go deeper
CLOCK_CLIP = 1.5  # clock fractions of base above this are clock glitches (a few stranger games in odd time controls)

# Feature definitions. They must stay as they are: the committed strangers' table
# (data/opponents/fingerprint_features.parquet) was built with them, and changing one needs --rebuild-features.
UNDER_CLOCK_SECONDS = 10  # "under10": a move started with less than this on the clock (plain seconds, increment included)
INSTANT_SECONDS = 1  # "fast1": a move played in less than this (premoves and pre-planned recaptures)
CLOCK_CHECKPOINTS = (10, 20, 40)  # "left_m<k>": clock left after your k-th move (end of opening, middlegame, late game)
EARLY_QUEEN_MOVES = 10  # "queen_early": queen moves among your first this many moves
EP_LOSS_LARGE = 0.1  # "ep_ge01": a move losing at least this many expected points (a clear mistake)
EP_LOSS_HUGE = 0.2  # "ep_ge02": ... at least this many (a blunder that swings a fifth of a point)
CP_LOSS_BLUNDER = 300  # "cp_ge300": the usual centipawn blunder threshold (as in moves_analysed's label)

# Models: deliberately modest settings (kept from the old analysis; not tuned on these data).
LOGISTIC_C = 0.5  # inverse L2 strength: mild shrinkage, correlated clock features otherwise blow up coefficients
HGB_TREES = 200  # boosting iterations (no early stopping, so every fit is identical across reruns)
HGB_LEARNING_RATE = 0.06  # small steps, paired with the tree count above
HGB_LEAVES = 15  # small trees: a few hundred games per fit cannot support deep interactions
HGB_MIN_LEAF = 20  # games per leaf: stops splits on a handful of rows
HGB_L2 = 1.0  # leaf-value shrinkage
PERM_REPEATS = 5  # permutation-importance shuffles per feature (ranks are stable beyond this)
SELF_CHECK_GAMES = 300  # your games replayed with the light replay to check it reproduces the stored enrichment

# --- feature groups -------------------------------------------------------------
CLOCK = [
    "t_opening", "t_middlegame", "t_endgame", "t_sd", "t_max", "left_m10", "left_m20", "left_m40", "left_end",
    "under10", "fast1",
]  # fmt: skip
MOVES = [
    "n_moves", "game_plies", "capture_rate", "check_rate", "castled", "castle_ply", "castle_long", "pawn_share",
    "knight_share", "bishop_share", "rook_share", "queen_share", "king_share", "promotions", "en_passant",
    "queen_early",
]  # fmt: skip
FIRST_MOVES = ["W_e4", "W_d4", "W_other", "B_e4_e5", "B_e4_c5", "B_e4_other", "B_d4_d5", "B_d4_Nf6", "B_d4_other", "B_other"]
ECO = ["eco_A", "eco_B", "eco_C", "eco_D", "eco_E", "eco_none"]
OPENING = [*FIRST_MOVES, *ECO, "book_ply", "left_book_first"]
ENGINE = ["ep_mean", "ep_ge01", "ep_ge02", "cp_mean", "cp_ge300", "best_share"]
GROUPS = {"clock": CLOCK, "opening": OPENING, "moves": MOVES, "engine": ENGINE}
STEP2_GROUPS = ["clock", "opening", "moves"]  # the strangers' games have no engine analysis
META = ["game_uuid", "color", "player", "time_class", "rating", "year", "rated", "base_seconds", "increment_seconds", "vs_player"]
STEP1_ALL = CLOCK + MOVES + OPENING + ENGINE
STEP2_ALL = CLOCK + MOVES + OPENING
MODELS = ["boosting", "logistic"]
HEADLINE_MODEL = "boosting"  # the better of the two in every old fit; logistic stays in the tables as the check

# Plain-language reading of every engine-free feature: (label, unit, rows it is measured on).
# Units: "share" (0-1, read as %), "seconds", "moves", "plies", "per game". Rows: which player-games the
# feature means something for (e.g. the reply to 1.e4 only in Black games where White opened 1.e4).
FEATURE_INFO = {
    "t_opening": ("time per move, opening", "seconds", "all"),
    "t_middlegame": ("time per move, middlegame", "seconds", "all"),
    "t_endgame": ("time per move, endgame", "seconds", "all"),
    "t_sd": ("spread (SD) of time per move", "seconds", "all"),
    "t_max": ("longest think", "seconds", "all"),
    "left_m10": ("clock left after your 10th move", "share of base", "all"),
    "left_m20": ("clock left after your 20th move", "share of base", "all"),
    "left_m40": ("clock left after your 40th move", "share of base", "all"),
    "left_end": ("clock left at the end", "share of base", "all"),
    "under10": ("moves started under 10 s", "share of moves", "all"),
    "fast1": ("moves played in under 1 s", "share of moves", "all"),
    "n_moves": ("own moves per game", "moves", "all"),
    "game_plies": ("game length", "plies", "all"),
    "capture_rate": ("captures", "share of moves", "all"),
    "check_rate": ("checks", "share of moves", "all"),
    "castled": ("games where you castled", "share of games", "all"),
    "castle_ply": ("castling move (own move number)", "moves", "castled"),
    "castle_long": ("castling queenside", "share of castled games", "castled"),
    "pawn_share": ("pawn moves", "share of moves", "all"),
    "knight_share": ("knight moves", "share of moves", "all"),
    "bishop_share": ("bishop moves", "share of moves", "all"),
    "rook_share": ("rook moves", "share of moves", "all"),
    "queen_share": ("queen moves", "share of moves", "all"),
    "king_share": ("king moves (not castling)", "share of moves", "all"),
    "promotions": ("promotions", "per game", "all"),
    "en_passant": ("en-passant captures", "per game", "all"),
    "queen_early": ("queen moves in the first 10", "per game", "all"),
    "W_e4": ("1.e4 as White", "share of White games", "white"),
    "W_d4": ("1.d4 as White", "share of White games", "white"),
    "W_other": ("other first move as White", "share of White games", "white"),
    "B_e4_e5": ("1...e5 against 1.e4", "share of Black games vs 1.e4", "black_e4"),
    "B_e4_c5": ("1...c5 against 1.e4", "share of Black games vs 1.e4", "black_e4"),
    "B_e4_other": ("other reply to 1.e4", "share of Black games vs 1.e4", "black_e4"),
    "B_d4_d5": ("1...d5 against 1.d4", "share of Black games vs 1.d4", "black_d4"),
    "B_d4_Nf6": ("1...Nf6 against 1.d4", "share of Black games vs 1.d4", "black_d4"),
    "B_d4_other": ("other reply to 1.d4", "share of Black games vs 1.d4", "black_d4"),
    "B_other": ("Black games where White opened neither e4 nor d4", "share of Black games", "black"),
    "eco_A": ("games ending in an A-code line", "share of games", "all"),
    "eco_B": ("games ending in a B-code line", "share of games", "all"),
    "eco_C": ("games ending in a C-code line", "share of games", "all"),
    "eco_D": ("games ending in a D-code line", "share of games", "all"),
    "eco_E": ("games ending in an E-code line", "share of games", "all"),
    "eco_none": ("games with no named line", "share of games", "all"),
    "book_ply": ("book depth", "plies", "all"),
    "left_book_first": ("you left the named lines first", "share of games", "all"),
}
# Extra habit for you vs your opponents only (4c, never a model feature): the strangers' table has no
# clock-buffer column, so this needs --rebuild-features before it could enter step 2.
STEP1_EXTRA_INFO = {
    "under10_buffer": ("moves started under 10 s beyond one increment", "share of moves", "all"),
}
HABIT_INFO = {**FEATURE_INFO, **STEP1_EXTRA_INFO}
SECONDS = {"t_opening", "t_middlegame", "t_endgame", "t_sd", "t_max"}  # stored as fraction of base: x base_seconds


def seed_for(*parts: object) -> int:
    """A seed from a string key, so every random step is reproducible and independent of run order."""
    return zlib.crc32(":".join(map(str, parts)).encode()) % (2**31)


# --- features -----------------------------------------------------------------


def add_move_kinds(moves: pd.DataFrame) -> pd.DataFrame:
    """En passant and castling side from the stored uci / fen_before (your own games)."""
    moves = moves.copy()
    ep_square = moves["fen_before"].str.split(" ").str[3]
    moves["is_en_passant"] = (moves["piece"] == "pawn") & moves["is_capture"] & (moves["uci"].str[2:4] == ep_square)
    moves["castle_long"] = moves["is_castling"] & moves["uci"].isin(["e1c1", "e8c8"])
    return moves


def game_features(moves: pd.DataFrame, openings: pd.DataFrame, with_engine: bool) -> pd.DataFrame:
    """One row per (game, colour) of engine-free style features (plus engine ones when asked).

    `moves` needs: game_uuid, ply, color, san, piece, is_capture, is_castling, castle_long,
    is_promotion, is_en_passant, gives_check, phase, clock_before_seconds, time_spent_seconds,
    clock_seconds, base_seconds (and ep_loss, cp_loss, is_best_move for the engine group).
    `openings` is indexed by game uuid with opening_eco and book_ply.
    """
    m = moves.sort_values(["game_uuid", "ply"], kind="stable")
    key = [m["game_uuid"], m["color"]]
    own_idx = m.groupby(["game_uuid", "color"], sort=False).cumcount() + 1
    g = m.groupby(["game_uuid", "color"], sort=False)
    f = pd.DataFrame(index=g.size().index)
    f["n_moves"] = g.size()
    f["game_plies"] = m.groupby("game_uuid")["ply"].max().reindex(f.index.get_level_values(0)).to_numpy()

    base = m["base_seconds"]
    frac_spent = (m["time_spent_seconds"].clip(lower=0) / base).clip(upper=CLOCK_CLIP)
    for ph in ("opening", "middlegame", "endgame"):
        f[f"t_{ph}"] = frac_spent.where(m["phase"] == ph).groupby(key).mean()
    f["t_sd"] = frac_spent.groupby(key).std()
    f["t_max"] = frac_spent.groupby(key).max()
    left = (m["clock_seconds"] / base).clip(upper=CLOCK_CLIP)
    for k in CLOCK_CHECKPOINTS:
        f[f"left_m{k}"] = left.where(own_idx == k).groupby(key).max()
    f["left_end"] = left.groupby(key).last()
    f["under10"] = (m["clock_before_seconds"] < UNDER_CLOCK_SECONDS).groupby(key).mean()
    f["fast1"] = (m["time_spent_seconds"] < INSTANT_SECONDS).groupby(key).mean()

    f["capture_rate"] = m["is_capture"].groupby(key).mean()
    f["check_rate"] = m["gives_check"].groupby(key).mean()
    f["castled"] = m["is_castling"].groupby(key).max().astype(float)
    f["castle_ply"] = own_idx.where(m["is_castling"]).groupby(key).max()
    f["castle_long"] = m["castle_long"].groupby(key).max().astype(float)
    for piece in ("pawn", "knight", "bishop", "rook", "queen"):
        f[f"{piece}_share"] = (m["piece"] == piece).groupby(key).mean()
    f["king_share"] = ((m["piece"] == "king") & ~m["is_castling"]).groupby(key).mean()
    f["promotions"] = m["is_promotion"].groupby(key).sum().astype(float)
    f["en_passant"] = m["is_en_passant"].groupby(key).sum().astype(float)
    f["queen_early"] = ((m["piece"] == "queen") & (own_idx <= EARLY_QUEEN_MOVES)).groupby(key).sum().astype(float)

    # Opening choice by colour: the first move (White) or the reply to it (Black).
    p1 = m.loc[m["ply"] == 1].set_index("game_uuid")["san"]
    p2 = m.loc[m["ply"] == 2].set_index("game_uuid")["san"]
    uuids = f.index.get_level_values(0)
    colors = f.index.get_level_values(1)
    w1 = p1.reindex(uuids).to_numpy(dtype=object)
    b1 = p2.reindex(uuids).to_numpy(dtype=object)
    cat = np.where(
        colors == "white",
        np.where(w1 == "e4", "W_e4", np.where(w1 == "d4", "W_d4", "W_other")),
        np.where(
            w1 == "e4",
            np.where(b1 == "e5", "B_e4_e5", np.where(b1 == "c5", "B_e4_c5", "B_e4_other")),
            np.where(w1 == "d4", np.where(b1 == "d5", "B_d4_d5", np.where(b1 == "Nf6", "B_d4_Nf6", "B_d4_other")), "B_other"),
        ),
    )
    for name in FIRST_MOVES:
        f[name] = (cat == name).astype(float)
    eco = openings["opening_eco"].reindex(uuids).fillna("").astype(str).str[:1].to_numpy(dtype=object)
    for letter in "ABCDE":
        f[f"eco_{letter}"] = (eco == letter).astype(float)
    f["eco_none"] = (eco == "").astype(float)
    book_ply = openings["book_ply"].reindex(uuids).fillna(0).to_numpy(dtype=float)
    f["book_ply"] = book_ply
    # The move after the deepest named position left the book: White's if book_ply is even.
    left_first = ((book_ply % 2 == 0) == (colors == "white")).astype(float)
    f["left_book_first"] = np.where(book_ply > 0, left_first, np.nan)

    if with_engine:
        f["ep_mean"] = m["ep_loss"].groupby(key).mean()
        f["ep_ge01"] = (m["ep_loss"] >= EP_LOSS_LARGE).groupby(key).mean()
        f["ep_ge02"] = (m["ep_loss"] >= EP_LOSS_HUGE).groupby(key).mean()
        f["cp_mean"] = m["cp_loss"].groupby(key).mean()
        f["cp_ge300"] = (m["cp_loss"] >= CP_LOSS_BLUNDER).groupby(key).mean()
        f["best_share"] = m["is_best_move"].astype(float).groupby(key).mean()
    f = f.reset_index()
    f = f[f["n_moves"] >= MIN_OWN_MOVES]
    return f


def step1_features(data) -> pd.DataFrame:
    """Both sides of your own games, with the game meta columns and the time-control group."""
    moves = add_move_kinds(data.moves)
    openings = data.games.set_index("uuid")[["opening_eco", "book_ply"]]
    feats = game_features(moves, openings, with_engine=True)
    # Clock-buffer version of under10 (clock beyond one increment; equals the clock in no-increment games).
    buffer = (moves["clock_buffer_seconds"] < UNDER_CLOCK_SECONDS).groupby([moves["game_uuid"], moves["color"]]).mean()
    feats["under10_buffer"] = buffer.reindex(pd.MultiIndex.from_frame(feats[["game_uuid", "color"]])).to_numpy()
    games = data.games.set_index("uuid")
    g = games.reindex(feats["game_uuid"])
    is_me = feats["color"].to_numpy() == g["my_color"].to_numpy()
    rated = g["rated"].to_numpy(dtype=bool)
    opp_pre = opponent_rating_pre(g).to_numpy()  # post-game rating + your change (rated), post-game rating (unrated)
    feats["player"] = np.where(is_me, data.username, "opp:" + g["opponent"].astype(str).str.lower().to_numpy())
    feats["time_class"] = g["time_class"].to_numpy()
    feats["tc_group"] = g["tc_group"].astype(str).to_numpy()
    feats["rating"] = np.where(is_me, g["my_rating_before"].astype("float64").to_numpy(), opp_pre)
    feats["year"] = g["year"].to_numpy()
    feats["rated"] = rated
    feats["base_seconds"] = g["base_seconds"].to_numpy(dtype=float)
    feats["increment_seconds"] = g["increment_seconds"].to_numpy(dtype=float)
    feats["vs_player"] = True
    feats["is_me"] = is_me
    # Keep only games where both sides survived the move filter (paired design), in the reported controls.
    both = feats.groupby("game_uuid")["color"].transform("size") == 2
    feats = feats[both & feats["tc_group"].isin(data.tc_groups)]
    return feats.reset_index(drop=True)


# --- light replay for the opponents' archives (only with --rebuild-features) --------------


def light_replay(initial_fen: str, sans: list[str]) -> list[tuple] | None:
    """Per move: piece, is_capture, is_castling, castle_long, is_promotion, is_en_passant, gives_check, pieces, position key (first plies only)."""
    board = chess.Board(initial_fen)
    rows = []
    for ply, san in enumerate(sans, 1):
        try:
            move = board.parse_san(san)
        except ValueError:
            return None
        piece = PIECE_NAMES[board.piece_type_at(move.from_square)]
        pieces = chess.popcount(board.knights | board.bishops | board.rooks | board.queens)
        is_capture = board.is_capture(move)
        is_castling = board.is_castling(move)
        castle_long = is_castling and chess.square_file(move.to_square) == 2
        is_ep = board.is_en_passant(move)
        board.push(move)
        key = board.epd() if ply <= BOOK_MAX_PLY else None
        rows.append((piece, is_capture, is_castling, castle_long, move.promotion is not None, is_ep, board.is_check(), pieces, key))
    return rows


def replay_games(rows: list[dict], move_lists: list[list[dict]], book: dict) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """(moves frame with the columns game_features needs, openings frame) for parsed games."""
    frames = []
    fen_rows = []
    for row, moves in zip(rows, move_lists):
        if not moves:
            continue
        replayed = light_replay(row["initial_setup"], [mv["san"] for mv in moves])
        if replayed is None:
            continue
        base, increment = parse_time_control(row["time_control"])
        if base is None:
            continue
        gm = pd.DataFrame(moves)
        state = pd.DataFrame(replayed, columns=["piece", "is_capture", "is_castling", "castle_long", "is_promotion", "is_en_passant", "gives_check", "pieces", "key"], index=gm.index)
        gm = pd.concat([gm, state.drop(columns="key"), _time_columns(gm, base, increment)], axis=1)
        gm["phase"] = [phase(p, n) for p, n in zip(state["pieces"], gm["move_number"])]
        gm["base_seconds"] = base
        gm["increment_seconds"] = increment
        frames.append(gm)
        keys = state["key"].dropna()
        fen_rows.append(pd.DataFrame({"game_uuid": row["uuid"], "ply": gm.loc[keys.index, "ply"].to_numpy(), "key": keys.to_numpy()}))
    if not frames:
        return None
    moves = pd.concat(frames, ignore_index=True)
    fens = pd.concat(fen_rows, ignore_index=True)
    # game_openings() wants fen_after; the keys are already position keys and position_key() is idempotent on them.
    openings = game_openings(fens.rename(columns={"key": "fen_after"}), book)
    return moves, openings


def complete_opponents(root: Path, log_path: Path | None) -> list[str]:
    """Opponents with a saved archive; with a download log, only those whose download finished."""
    dirs = sorted(p.parent.name for p in root.glob("*/raw") if any(p.glob("*.json")))
    if log_path is None or not log_path.exists():
        return dirs
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    entries = [(int(mt.group(1)), mt.group(3)) for ln in lines if (mt := re.match(r"\[(\d+)/(\d+)\] (\S+)", ln))]
    done = any(ln.startswith("done:") for ln in lines)
    names = []
    for i, (idx, name) in enumerate(entries):
        later = i + 1 < len(entries) and entries[i + 1][0] == idx + 1
        if (later or done) and name in dirs:
            names.append(name)
    return names


def sample_opponent(name: str, raw_dir: Path, username: str, book: dict) -> tuple[pd.DataFrame | None, dict]:
    """Deterministic sample of up to CAP_PER_CLASS standard live games per time class vs third parties."""
    candidates: dict[str, list[tuple[int, str, dict, str]]] = {tc: [] for tc in TIME_CLASSES}
    available = {tc: 0 for tc in TIME_CLASSES}
    for path in sorted(raw_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for game in data.get("games", []):
            if game.get("rules") != "chess" or game.get("time_class") not in candidates:
                continue
            white = str(game.get("white", {}).get("username", "")).lower()
            black = str(game.get("black", {}).get("username", "")).lower()
            if username in (white, black) or name not in (white, black) or not game.get("pgn"):
                continue
            tc = game["time_class"]
            available[tc] += 1
            candidates[tc].append((int(game.get("end_time", 0)), game["uuid"], game, path.stem))
    rng = np.random.default_rng([SEED, zlib.crc32(name.encode())])
    rows, move_lists = [], []
    for tc in TIME_CLASSES:
        pool = sorted(candidates[tc], key=lambda c: (c[0], c[1]))
        if len(pool) > CAP_PER_CLASS:
            pool = [pool[i] for i in sorted(rng.choice(len(pool), CAP_PER_CLASS, replace=False))]
        for _, _, game, month in pool:
            row, moves = parse_game(game, month)
            rows.append(row)
            move_lists.append(moves)
    info = {"opponent": name, **{f"{tc}_available": available[tc] for tc in TIME_CLASSES}}
    if not rows:
        return None, info
    replayed = replay_games(rows, move_lists, book)
    if replayed is None:
        return None, info
    moves, openings = replayed
    feats = game_features(moves, openings, with_engine=False)
    g = pd.DataFrame(rows).set_index("uuid").reindex(feats["game_uuid"])
    is_white = g["white_username"].str.lower().to_numpy() == name
    own = feats["color"].to_numpy() == np.where(is_white, "white", "black")
    feats = feats[own].copy()
    g = g[own]
    feats["player"] = name
    feats["time_class"] = g["time_class"].to_numpy()
    feats["rating"] = np.where(is_white[own], g["white_rating"].to_numpy(dtype=float), g["black_rating"].to_numpy(dtype=float))
    feats["year"] = pd.to_datetime(g["end_time"], unit="s").dt.year.to_numpy()
    feats["rated"] = g["rated"].to_numpy(dtype=bool)
    tcs = g["time_control"].map(parse_time_control)
    feats["base_seconds"] = [t[0] for t in tcs]
    feats["increment_seconds"] = [t[1] for t in tcs]
    feats["vs_player"] = False
    for tc in TIME_CLASSES:
        info[f"{tc}_sampled"] = int((feats["time_class"] == tc).sum())
    return feats, info


def rebuild_features(feats1: pd.DataFrame, opp_root: Path, features_path: Path, username: str, log_path: Path | None) -> None:
    """Recompute data/opponents/fingerprint_features.parquet from the raw opponent archives."""
    t0 = time.time()
    book = load_book(load_settings().openings_dir)
    names = complete_opponents(opp_root, log_path)
    frames, infos = [], []
    for i, name in enumerate(names, 1):
        feats, info = sample_opponent(name, opp_root / name / "raw", username.lower(), book)
        infos.append(info)
        if feats is not None:
            frames.append(feats)
        if i % 20 == 0:
            print(f"  replayed {i}/{len(names)} opponents ({time.time() - t0:.0f} s)", flush=True)
    manifest = pd.DataFrame(infos)
    mine = feats1[feats1["is_me"]].drop(columns=["is_me", "tc_group"])
    table = pd.concat([mine, *frames], ignore_index=True)
    table = table[META + CLOCK + MOVES + OPENING + ENGINE]
    for c in CLOCK + MOVES + OPENING + ENGINE + ["rating"]:
        table[c] = table[c].astype("float32")
    table["year"] = table["year"].astype("int16")
    table = table.merge(manifest.rename(columns={"opponent": "player"}), on="player", how="left")
    features_path.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(features_path, index=False, compression="zstd")
    print(f"  features written to {features_path} in {time.time() - t0:.0f} s", flush=True)


def self_check(data, n_games: int = SELF_CHECK_GAMES) -> tuple[pd.DataFrame, int]:
    """Light replay vs the stored enrichment on a sample of your own games: max |difference| per feature."""
    settings = load_settings()
    book = load_book(settings.openings_dir)
    games, moves = build_tables(settings.user_dir / "raw")
    sample = data.games.sample(n_games, random_state=SEED)["uuid"]
    games = games[games["uuid"].isin(sample)]
    moves = moves[moves["game_uuid"].isin(sample)]
    rows = games.to_dict("records")
    move_lists = [moves[moves["game_uuid"] == r["uuid"]].to_dict("records") for r in rows]
    replayed_moves, openings = replay_games(rows, move_lists, book)
    light = game_features(replayed_moves, openings, with_engine=False).set_index(["game_uuid", "color"])
    stored = game_features(add_move_kinds(data.moves[data.moves["game_uuid"].isin(sample)]), data.games.set_index("uuid")[["opening_eco", "book_ply"]], with_engine=False).set_index(["game_uuid", "color"])
    common = light.index.intersection(stored.index)
    diff = (light.loc[common, STEP2_ALL].astype(float).fillna(-1) - stored.loc[common, STEP2_ALL].astype(float).fillna(-1)).abs().max()
    return pd.DataFrame({"feature": diff.index, "max_abs_diff": diff.to_numpy(), "rows_checked": len(common)}), len(common)


# --- classifiers and metrics ------------------------------------------------------------


def make_model(kind: str) -> object:
    if kind == "logistic":
        return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)), ("scale", StandardScaler()), ("lr", LogisticRegression(C=LOGISTIC_C, max_iter=2000))])
    return HistGradientBoostingClassifier(max_iter=HGB_TREES, learning_rate=HGB_LEARNING_RATE, max_leaf_nodes=HGB_LEAVES, min_samples_leaf=HGB_MIN_LEAF, l2_regularization=HGB_L2, early_stopping=False, random_state=SEED)


def oof_predict(X: np.ndarray, y: np.ndarray, folds: list[tuple[np.ndarray, np.ndarray]], kind: str) -> np.ndarray:
    p = np.full(len(y), np.nan)
    for train, test in folds:
        model = make_model(kind)
        model.fit(X[train], y[train])
        p[test] = model.predict_proba(X[test])[:, 1]
    return p


def game_folds(groups: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    return list(GroupKFold(n_splits=N_FOLDS).split(np.zeros(len(groups)), groups=groups))


def stratified_folds(y: np.ndarray, seed: int) -> list[tuple[np.ndarray, np.ndarray]]:
    return list(StratifiedKFold(n_splits=N_FOLDS, shuffle=True, random_state=seed).split(np.zeros(len(y)), y))


def _weighted_auc(order_y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """AUC for each row of weights `w` (n_boot x n), given labels sorted by increasing score."""
    neg = w * (1 - order_y)
    pos = w * order_y
    cum_neg = np.cumsum(neg, axis=1) - neg  # negatives strictly below (ties ignored: scores are continuous)
    return (pos * cum_neg).sum(axis=1) / np.maximum(pos.sum(axis=1) * neg.sum(axis=1), 1e-12)


def auc_draws(y: np.ndarray, preds: dict, clusters: np.ndarray, seed: int, n_boot: int = N_BOOT) -> dict:
    """{name: (AUC, bootstrap draws)} for several prediction vectors, all on the same cluster resamples.

    Sharing the resamples makes the difference between two feature sets a paired comparison.
    """
    codes, inv = np.unique(clusters, return_inverse=True)
    rng = np.random.default_rng(seed)
    counts = np.stack([np.bincount(rng.integers(0, len(codes), len(codes)), minlength=len(codes)) for _ in range(n_boot)]).astype(float) if len(codes) > 1 else None
    out = {}
    for name, p in preds.items():
        order = np.argsort(p, kind="stable")
        oy = y[order].astype(float)
        est = float(_weighted_auc(oy[None, :], np.ones((1, len(y))))[0])
        boot = _weighted_auc(oy[None, :], counts[:, inv[order]]) if counts is not None else np.full(1, np.nan)
        out[name] = (est, boot)
    return out


def ci(boot: np.ndarray) -> tuple[float, float]:
    if np.all(np.isnan(boot)):
        return float("nan"), float("nan")
    lo, hi = np.nanquantile(boot, [0.025, 0.975])
    return float(lo), float(hi)


def mean_draws(values: np.ndarray, clusters: np.ndarray, seed: int, n_boot: int = N_BOOT) -> tuple[float, np.ndarray]:
    """Mean and bootstrap draws of the mean, resampling whole clusters (games or players)."""
    ok = ~np.isnan(values)
    values, clusters = values[ok], clusters[ok]
    if len(values) == 0:
        return float("nan"), np.full(n_boot, np.nan)
    codes, inv = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(codes))
    counts = np.bincount(inv, minlength=len(codes)).astype(float)
    est = float(sums.sum() / counts.sum())
    if len(codes) < 2:
        return est, np.full(n_boot, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(codes), size=(n_boot, len(codes)))
    return est, sums[idx].sum(axis=1) / counts[idx].sum(axis=1)


def mean_ci(values: np.ndarray, clusters: np.ndarray, seed: int) -> tuple[float, float, float]:
    est, boot = mean_draws(np.asarray(values, dtype=float), np.asarray(clusters), seed)
    return (est, *ci(boot))


def pairwise_draws(frame: pd.DataFrame, p: np.ndarray, seed: int) -> tuple[float, float, float]:
    """Share of games where your row scores higher than your opponent's (chance 0.5), CI by game."""
    t = pd.DataFrame({"g": frame["game_uuid"].to_numpy(), "me": frame["is_me"].to_numpy(), "p": p})
    wide = t.pivot(index="g", columns="me", values="p")
    correct = (wide[True] > wide[False]).astype(float).to_numpy()
    return mean_ci(correct, wide.index.to_numpy(), seed)


# --- plain-unit habits --------------------------------------------------------------------


def plain_values(frame: pd.DataFrame, feature: str) -> np.ndarray:
    """The feature in its plain unit (seconds instead of fraction of base), NaN outside the rows it applies to."""
    v = frame[feature].to_numpy(dtype=float).copy()
    if feature in SECONDS:
        v = v * frame["base_seconds"].to_numpy(dtype=float)
    rows = HABIT_INFO[feature][2]
    color = frame["color"].to_numpy()
    if rows == "white":
        v[color != "white"] = np.nan
    elif rows == "black":
        v[color != "black"] = np.nan
    elif rows == "black_e4":
        faced = frame[["B_e4_e5", "B_e4_c5", "B_e4_other"]].sum(axis=1).to_numpy() > 0
        v[~faced] = np.nan
    elif rows == "black_d4":
        faced = frame[["B_d4_d5", "B_d4_Nf6", "B_d4_other"]].sum(axis=1).to_numpy() > 0
        v[~faced] = np.nan
    elif rows == "castled":
        v[frame["castled"].to_numpy(dtype=float) < 1] = np.nan
    return v


def feature_columns(feature: str) -> dict:
    label, unit, _ = HABIT_INFO[feature]
    return {"feature": feature, "habit": label, "measure": unit}


# --- step 1: you vs your opponents in the same games ------------------------------------------


def step1_sets() -> dict[str, list[str]]:
    sets = {"all": STEP1_ALL}
    for g in GROUPS:
        sets[f"no {g}"] = [c for c in STEP1_ALL if c not in GROUPS[g]]
    for g in GROUPS:
        sets[f"{g} only"] = GROUPS[g]
    return sets


def step1_unit(label: str, part: pd.DataFrame) -> dict:
    """5-fold-by-game fits for every feature set and model; AUC, pairwise accuracy and AUC lost per group."""
    part = part.reset_index(drop=True)
    y = part["is_me"].to_numpy(dtype=int)
    games = part["game_uuid"].to_numpy()
    folds = game_folds(games)
    sets = step1_sets()
    preds = {(s, k): oof_predict(part[cols].to_numpy(dtype=float), y, folds, k) for s, cols in sets.items() for k in MODELS}
    draws = auc_draws(y, preds, games, seed_for("step1", label))
    n_games = int(part["game_uuid"].nunique())
    thin = n_games < MIN_FIT_GAMES
    auc_rows, drop_rows = [], []
    for k in MODELS:
        est, boot = draws[("all", k)]
        pw = pairwise_draws(part, preds[("all", k)], seed_for("pair1", label, k))
        auc_rows.append({"tc_group": label, "test": "same years (5-fold by game)", "model": k, "auc": est, "auc_lo": ci(boot)[0], "auc_hi": ci(boot)[1], "pairwise": pw[0], "pairwise_lo": pw[1], "pairwise_hi": pw[2], "games": n_games, "thin": thin})
        for g in GROUPS:
            w_est, w_boot = draws[(f"no {g}", k)]
            o_est, o_boot = draws[(f"{g} only", k)]
            lost_lo, lost_hi = ci(boot - w_boot)
            drop_rows.append({
                "tc_group": label, "comparison": "you vs opponents (same games)", "feature_group": g, "model": k,
                "auc_all": est, "auc_without": w_est, "auc_lost": est - w_est, "auc_lost_lo": lost_lo, "auc_lost_hi": lost_hi,
                "auc_group_only": o_est, "auc_group_only_lo": ci(o_boot)[0], "auc_group_only_hi": ci(o_boot)[1],
                "games": n_games, "thin": thin,
            })  # fmt: skip
    return {"auc": auc_rows, "drop": drop_rows}


def step1_years(label: str, part: pd.DataFrame, five_fold: dict[str, float]) -> list[dict]:
    """Train on the other years, test on one; 'all held-out' pools the test years with enough games."""
    part = part.reset_index(drop=True)
    y = part["is_me"].to_numpy(dtype=int)
    X = part[STEP1_ALL].to_numpy(dtype=float)
    years = part["year"].to_numpy()
    games = part["game_uuid"].to_numpy()
    counts = part.groupby("year")["game_uuid"].nunique()
    rows = []
    pooled = {k: np.full(len(y), np.nan) for k in MODELS}
    for yr, n in counts.items():
        test = np.flatnonzero(years == yr)
        train = np.flatnonzero(years != yr)
        if counts.drop(yr).sum() < MIN_YEAR_TRAIN_GAMES:
            continue  # too few games in the other years to train on (boosting would predict a constant)
        thin = n < MIN_YEAR_TEST_GAMES
        for k in MODELS:
            p = make_model(k).fit(X[train], y[train]).predict_proba(X[test])[:, 1]
            if not thin:
                pooled[k][test] = p
            est, boot = auc_draws(y[test], {"p": p}, games[test], seed_for("year", label, yr, k))["p"]
            pw = pairwise_draws(part.iloc[test], p, seed_for("yearpair", label, yr, k))
            rows.append({"tc_group": label, "test_year": str(int(yr)), "model": k, "auc": est, "auc_lo": ci(boot)[0], "auc_hi": ci(boot)[1], "pairwise": pw[0], "pairwise_lo": pw[1], "pairwise_hi": pw[2], "auc_same_years": five_fold[k], "games": int(n), "thin": thin})
    mask = ~np.isnan(pooled[MODELS[0]])
    if mask.any():
        n = int(part.loc[mask, "game_uuid"].nunique())
        for k in MODELS:
            est, boot = auc_draws(y[mask], {"p": pooled[k][mask]}, games[mask], seed_for("yearpool", label, k))["p"]
            pw = pairwise_draws(part[mask], pooled[k][mask], seed_for("yearpoolpair", label, k))
            rows.append({"tc_group": label, "test_year": "all held-out", "model": k, "auc": est, "auc_lo": ci(boot)[0], "auc_hi": ci(boot)[1], "pairwise": pw[0], "pairwise_lo": pw[1], "pairwise_hi": pw[2], "auc_same_years": five_fold[k], "games": n, "thin": n < MIN_FIT_GAMES})
    return rows


def habits_vs_opponents(label: str, part: pd.DataFrame) -> list[dict]:
    """Your per-game habits vs your opponents' in the same games (difference bootstrapped by game)."""
    rows = []
    for feature in HABIT_INFO:
        long = pd.DataFrame({"game_uuid": part["game_uuid"].to_numpy(), "is_player_move": part["is_me"].to_numpy(), "value": plain_values(part, feature)})
        cmp = compare_sides(long, "value", n_boot=N_BOOT)
        if cmp.empty:
            continue
        r = cmp.iloc[0]
        rows.append({"tc_group": label, **feature_columns(feature), "you": r["player"], "you_lo": r["player_lo"], "you_hi": r["player_hi"], "opponents": r["opponent"], "opponents_lo": r["opponent_lo"], "opponents_hi": r["opponent_hi"], "diff": r["diff"], "diff_lo": r["diff_lo"], "diff_hi": r["diff_hi"], "games": int(r["games"]), "n": int(r["n"]), "thin": int(r["games"]) < MIN_GROUP_GAMES})
    return rows


# --- step 2: you vs strangers of your rating --------------------------------------------------


@dataclass
class Unit:
    """One step-2 comparison: your games and strangers' games, matched on `key` (band, or band + exact control)."""

    label: str
    fit: str
    time_class: str
    mine: pd.DataFrame
    strangers: pd.DataFrame
    key: str

    @property
    def matched_on_control(self) -> bool:
        return self.key == "band_tc"

    @property
    def tc_group(self) -> str:
        """The control ("bullet 1+0") or pooled time class ("all bullet") the fit covers, as in step 1."""
        return self.label.split(" (")[0]

    @property
    def matching(self) -> str:
        """What your games and the strangers' are matched on (a single-control fit is matched on both by construction)."""
        return "band only" if self.label.endswith("(band only)") else "band and control"


def tidy_units(frame: pd.DataFrame, names: dict[str, tuple[str, str]]) -> pd.DataFrame:
    """Replace the internal fit label ('unit', 'fit') by tc_group and matching, first in the table."""
    frame = frame.copy()
    tc = frame["unit"].map(lambda u: names[u][0])
    match = frame["unit"].map(lambda u: names[u][1])
    frame = frame.drop(columns=[c for c in ("unit", "fit") if c in frame.columns])
    frame.insert(0, "matching", match)
    frame.insert(0, "tc_group", tc)
    return frame


def band_match(a: pd.DataFrame, b: pd.DataFrame, rng: np.random.Generator, key: str, ratio: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Subsample both frames so that their histograms of `key` agree (b may keep up to `ratio` x a's count per cell)."""
    ia, ib = [], []
    for cell in sorted(set(a[key].dropna()) & set(b[key].dropna())):
        xa = a.index[a[key] == cell].to_numpy()
        xb = b.index[b[key] == cell].to_numpy()
        ka = min(len(xa), len(xb))
        kb = min(len(xb), ratio * ka)
        ia.extend(sorted(rng.choice(xa, ka, replace=False)))
        ib.extend(sorted(rng.choice(xb, kb, replace=False)))
    return a.loc[ia], b.loc[ib]


def step2_sets() -> dict[str, list[str]]:
    sets = {"all": STEP2_ALL}
    for g in STEP2_GROUPS:
        sets[f"no {g}"] = [c for c in STEP2_ALL if c not in GROUPS[g]]
    for g in STEP2_GROUPS:
        sets[f"{g} only"] = GROUPS[g]
    return sets


PAIR_SETS = {"all": STEP2_ALL, "no clock": MOVES + OPENING, "no opening": CLOCK + MOVES, "no clock, no opening": MOVES}


def pooled_folds(frame: pd.DataFrame, me: str, rng: np.random.Generator) -> list[tuple[np.ndarray, np.ndarray]]:
    """Folds where every stranger's games sit in one fold and your games are split at random."""
    players = frame["player"].to_numpy()
    strangers = np.array(sorted(set(players) - {me}))
    assign = dict(zip(strangers, rng.permutation(len(strangers)) % N_FOLDS))
    fold = np.array([assign.get(p, -1) for p in players])
    mine = fold == -1
    fold[mine] = rng.integers(0, N_FOLDS, mine.sum())
    return [(np.flatnonzero(fold != k), np.flatnonzero(fold == k)) for k in range(N_FOLDS)]


def control_mix(frame: pd.DataFrame) -> pd.Series:
    labels = pd.Series([f"{b / 60:g}+{i:g}" for b, i in zip(frame["base_seconds"].astype(float), frame["increment_seconds"].fillna(0).astype(float))])
    return labels.value_counts(normalize=True)


def pooled_fit(unit: Unit, me: str) -> dict | None:
    """You vs all band-matched strangers at once: AUC per feature set, importances, habits, control mix."""
    rng = np.random.default_rng(seed_for("pooled", unit.label))
    a, b = band_match(unit.mine, unit.strangers, rng, unit.key)
    if len(a) < MIN_FIT_GAMES:
        return None
    frame = pd.concat([a, b], ignore_index=True)
    y = np.r_[np.ones(len(a), dtype=int), np.zeros(len(b), dtype=int)]
    folds = pooled_folds(frame, me, rng)
    clusters = np.where(frame["player"] == me, frame["game_uuid"], frame["player"]).astype(str)
    sets = step2_sets()
    if not unit.matched_on_control and unit.fit != "time control":
        # The band-only class fits exist only to show the increment confound: all features vs no clock.
        sets = {s: sets[s] for s in ("all", "no clock")}
    preds = {(s, k): oof_predict(frame[cols].to_numpy(dtype=float), y, folds, k) for s, cols in sets.items() for k in MODELS}
    draws = auc_draws(y, preds, clusters, seed_for("pooledauc", unit.label))
    base = {"unit": unit.label, "fit": unit.fit, "time_class": unit.time_class}
    counts = {"you_games": len(a), "stranger_games": len(b), "strangers": int(b["player"].nunique())}
    auc_rows, drop_rows = [], []
    for k in MODELS:
        est, boot = draws[("all", k)]
        acc = mean_ci(((preds[("all", k)] >= 0.5) == y).astype(float), clusters, seed_for("acc", unit.label, k))
        auc_rows.append({**base, "model": k, "auc": est, "auc_lo": ci(boot)[0], "auc_hi": ci(boot)[1], "accuracy": acc[0], "accuracy_lo": acc[1], "accuracy_hi": acc[2], **counts})
        for g in STEP2_GROUPS:
            if (f"no {g}", k) not in draws:
                continue
            w_est, w_boot = draws[(f"no {g}", k)]
            o_est, o_boot = draws.get((f"{g} only", k), (np.nan, np.full(1, np.nan)))
            lo, hi = ci(boot - w_boot)
            drop_rows.append({**base, "comparison": "you vs strangers", "feature_group": g, "model": k, "auc_all": est, "auc_without": w_est, "auc_lost": est - w_est, "auc_lost_lo": lo, "auc_lost_hi": hi, "auc_group_only": o_est, "auc_group_only_lo": ci(o_boot)[0], "auc_group_only_hi": ci(o_boot)[1], **counts})

    # Importances: boosting permutation importance on a held-out fold, standardised logistic coefficients on everything.
    X = frame[STEP2_ALL].to_numpy(dtype=float)
    train, test = folds[0]
    hgb = make_model("boosting").fit(X[train], y[train])
    perm = permutation_importance(hgb, X[test], y[test], scoring="roc_auc", n_repeats=PERM_REPEATS, random_state=SEED, n_jobs=1)
    lr = make_model("logistic").fit(X, y)
    imp = pd.DataFrame({"feature": STEP2_ALL, "perm_auc_drop": perm.importances_mean, "perm_sd": perm.importances_std, "logistic_coef": lr.named_steps["lr"].coef_[0]})
    imp = imp.sort_values(["perm_auc_drop", "feature"], ascending=[False, True]).reset_index(drop=True)
    imp["rank"] = np.arange(1, len(imp) + 1)
    imp_rows = [{**base, **feature_columns(r.feature), "perm_auc_drop": r.perm_auc_drop, "perm_sd": r.perm_sd, "logistic_coef": r.logistic_coef, "rank": int(r.rank)} for r in imp.head(TOP_IMPORTANCE).itertuples()]

    # Habits in plain units, you (CI by game) vs strangers (CI by player), on the matched sample.
    rank = dict(zip(imp["feature"], imp["rank"]))
    habit_rows = []
    for feature in FEATURE_INFO:
        va, vb = plain_values(a, feature), plain_values(b, feature)
        ya, da = mean_draws(va, a["game_uuid"].to_numpy(), seed_for("hab", unit.label, feature, "you"))
        yb, db = mean_draws(vb, b["player"].to_numpy(), seed_for("hab", unit.label, feature, "them"))
        habit_rows.append({**base, **feature_columns(feature), "you": ya, "you_lo": ci(da)[0], "you_hi": ci(da)[1], "strangers": yb, "strangers_lo": ci(db)[0], "strangers_hi": ci(db)[1], "diff": ya - yb, "diff_lo": ci(da - db)[0], "diff_hi": ci(da - db)[1], "importance_rank": rank[feature], "you_games": int((~np.isnan(va)).sum()), "stranger_games": int((~np.isnan(vb)).sum()), "strangers_n": int(b.loc[~np.isnan(vb), "player"].nunique())})

    # Time-control mix and share of games with an increment, both sides of the matched sample.
    inc_a = float((a["increment_seconds"].fillna(0) > 0).mean())
    inc_b = float((b["increment_seconds"].fillna(0) > 0).mean())
    inc_row = {**base, "you_increment_share": inc_a, "strangers_increment_share": inc_b, "diff": inc_a - inc_b, **counts}
    mix_a, mix_b = control_mix(a), control_mix(b)
    controls = sorted(set(mix_a.index) | set(mix_b.index), key=lambda c: (-(mix_a.get(c, 0) + mix_b.get(c, 0)), c))
    mix_rows = [{**base, "control": c, "you_share": float(mix_a.get(c, 0.0)), "strangers_share": float(mix_b.get(c, 0.0))} for c in controls]
    return {"auc": auc_rows, "drop": drop_rows, "importance": imp_rows, "habits": habit_rows, "increment": inc_row, "mix": mix_rows}


def pair_aucs(a: pd.DataFrame, b: pd.DataFrame, seed: int, key: str) -> dict | None:
    """Logistic-regression AUC for each per-pair feature set, a vs b, stratified 5-fold (each game one row)."""
    rng = np.random.default_rng(seed)
    a, b = band_match(a, b, rng, key)
    if len(a) < MIN_MATCHED:
        return None
    frame = pd.concat([a, b], ignore_index=True)
    y = np.r_[np.ones(len(a), dtype=int), np.zeros(len(b), dtype=int)]
    folds = stratified_folds(y, seed)
    preds = {s: oof_predict(frame[cols].to_numpy(dtype=float), y, folds, "logistic") for s, cols in PAIR_SETS.items()}
    return {"n": len(a), **{s: auc_draws(y, {"p": p}, np.arange(len(y)), seed, n_boot=1)["p"][0] for s, p in preds.items()}}


def eligible_strangers(unit: Unit) -> list[str]:
    counts = unit.strangers.groupby("player").size()
    return sorted(p for p, n in counts.items() if n >= MIN_MATCHED)


def per_pair_test(unit: Unit, me: str) -> list[dict]:
    """You vs each stranger and stranger vs stranger, same procedure and sizes; summary per feature set."""
    eligible = eligible_strangers(unit)
    records = []
    rng = np.random.default_rng(seed_for("partners", unit.label))
    for p in eligible:
        res = pair_aucs(unit.mine, unit.strangers[unit.strangers["player"] == p], seed_for("pair", unit.label, me, p), unit.key)
        if res is not None:
            records.append({"kind": "you", "a": me, "b": p, **res})
    for p in eligible:
        others = [q for q in eligible if q != p]
        for q in sorted(rng.choice(others, min(N_PARTNERS, len(others)), replace=False)) if others else []:
            res = pair_aucs(unit.strangers[unit.strangers["player"] == p], unit.strangers[unit.strangers["player"] == q], seed_for("pair", unit.label, p, q), unit.key)
            if res is not None:
                records.append({"kind": "strangers", "a": p, "b": q, **res})
    pairs = pd.DataFrame(records, columns=["kind", "a", "b", "n", *PAIR_SETS])
    rows = []
    for s in PAIR_SETS:
        row = {"unit": unit.label, "fit": unit.fit, "time_class": unit.time_class, "features": s}
        for kind in ("you", "strangers"):
            v = pairs.loc[pairs["kind"] == kind, s].to_numpy(dtype=float)
            row[f"{kind}_mean"] = v.mean() if len(v) else np.nan
            row[f"{kind}_q25"], row[f"{kind}_q75"] = np.quantile(v, [0.25, 0.75]) if len(v) else (np.nan, np.nan)
            row[f"{kind}_share_above_{GOOD_AUC:g}"] = (v > GOOD_AUC).mean() if len(v) else np.nan
            row[f"{kind}_pairs"] = len(v)
        # Paired by stranger: your AUC against X minus X's mean AUC against its partners.
        a_vals = pairs[pairs["kind"] == "you"].set_index("b")[s]
        b_vals = pairs[pairs["kind"] == "strangers"].groupby("a")[s].mean()
        common = a_vals.index.intersection(b_vals.index)
        d = (a_vals.loc[common] - b_vals.loc[common]).to_numpy(dtype=float)
        est, lo, hi = mean_ci(d, common.to_numpy(), seed_for("pairdiff", unit.label, s)) if len(common) else (np.nan, np.nan, np.nan)
        row.update({"diff": est, "diff_lo": lo, "diff_hi": hi, "strangers_paired": len(common), "thin": len(common) < MIN_STRANGERS})
        rows.append(row)
    return rows


def vs_rest(unit: Unit, me: str, ids: dict[str, str]) -> tuple[dict, list[dict]]:
    """Each stranger vs the rest of the pool, and random same-sized subsamples of you vs the rest."""
    s = unit.strangers
    eligible = eligible_strangers(unit)
    each = []
    sizes = []
    for p in eligible:
        xa, xb = s[s["player"] == p], s[s["player"] != p]
        r = np.random.default_rng(seed_for("rest", unit.label, p))
        xa2, xb2 = band_match(xa, xb, r, unit.key, ratio=REST_RATIO)
        if len(xa2) < MIN_MATCHED:
            continue
        fr = pd.concat([xa2, xb2], ignore_index=True)
        yy = np.r_[np.ones(len(xa2), dtype=int), np.zeros(len(xb2), dtype=int)]
        pp = oof_predict(fr[STEP2_ALL].to_numpy(dtype=float), yy, stratified_folds(yy, SEED), "logistic")
        each.append({"unit": unit.label, "who": "stranger", "id": ids[p], "auc": auc_draws(yy, {"p": pp}, np.arange(len(yy)), SEED, n_boot=1)["p"][0], "games": len(xa2)})
        sizes.append(len(xa))
    # Your draws are the size of the median eligible stranger's sample, so both sides train on the same amount of data.
    draw_size = int(np.median(sizes)) if sizes else 0
    for d in range(N_ME_DRAWS if draw_size else 0):
        r = np.random.default_rng(seed_for("me-draw", unit.label, d))
        sub = unit.mine.loc[sorted(r.choice(unit.mine.index.to_numpy(), min(draw_size, len(unit.mine)), replace=False))]
        xa2, xb2 = band_match(sub, s, r, unit.key, ratio=REST_RATIO)
        if len(xa2) < MIN_MATCHED:
            continue
        fr = pd.concat([xa2, xb2], ignore_index=True)
        yy = np.r_[np.ones(len(xa2), dtype=int), np.zeros(len(xb2), dtype=int)]
        pp = oof_predict(fr[STEP2_ALL].to_numpy(dtype=float), yy, stratified_folds(yy, SEED + d), "logistic")
        each.append({"unit": unit.label, "who": "you", "id": f"draw {d + 1}", "auc": auc_draws(yy, {"p": pp}, np.arange(len(yy)), SEED, n_boot=1)["p"][0], "games": len(xa2)})
    frame = pd.DataFrame(each, columns=["unit", "who", "id", "auc", "games"])
    you_v = frame.loc[frame["who"] == "you", "auc"].to_numpy(dtype=float)
    str_v = frame.loc[frame["who"] == "stranger", "auc"].to_numpy(dtype=float)
    row = {"unit": unit.label, "fit": unit.fit, "time_class": unit.time_class}
    if len(you_v) and len(str_v):
        # Your interval is the spread between the overlapping draws (which games were drawn), not a sampling CI.
        ym = (float(you_v.mean()), *(float(q) for q in np.quantile(you_v, [0.025, 0.975])))
        sm = mean_ci(str_v, np.arange(len(str_v)), seed_for("reststr", unit.label))
        # Share of strangers less distinctive than you, bootstrapping strangers and your draws together.
        rng = np.random.default_rng(seed_for("pct", unit.label))
        yb = rng.choice(you_v, (N_BOOT, len(you_v))).mean(axis=1)
        sb = rng.choice(str_v, (N_BOOT, len(str_v)))
        share = float((str_v < you_v.mean()).mean())
        share_lo, share_hi = ci((sb < yb[:, None]).mean(axis=1))
        row.update({
            "you_auc": ym[0], "you_auc_lo": ym[1], "you_auc_hi": ym[2],
            "strangers_auc": sm[0], "strangers_auc_lo": sm[1], "strangers_auc_hi": sm[2],
            "strangers_q25": float(np.quantile(str_v, 0.25)), "strangers_q75": float(np.quantile(str_v, 0.75)),
            "diff": ym[0] - sm[0],
            "share_strangers_less_distinctive": share, "share_lo": share_lo, "share_hi": share_hi,
        })  # fmt: skip
    row.update({"you_draws": len(you_v), "strangers": len(str_v), "draw_games": draw_size, "thin": len(str_v) < MIN_STRANGERS})
    return row, each


# --- charts -----------------------------------------------------------------------------------


def chart_auc(results: Results, auc1: pd.DataFrame, years: pd.DataFrame, auc2: pd.DataFrame, groups: list[str], unit_of: dict[str, str]) -> None:
    import matplotlib.pyplot as plt

    fig, ax = charts.single("How well a model tells your games apart (AUC, 0.5 = chance)", size=(8.6, 3.8))
    model = HEADLINE_MODEL
    same = auc1[(auc1["test"].str.startswith("same")) & (auc1["model"] == model) & ~auc1["thin"]]
    unseen = years[(years["test_year"] == "all held-out") & (years["model"] == model) & ~years["thin"]]
    own = auc2[(auc2["model"] == model) & (auc2["fit"] == "time control")].rename(columns={"unit": "tc_group"})
    series = [
        (same, "vs your opponents, same years", charts.SERIES[0], -0.24),
        (unseen, "vs your opponents, unseen year", charts.SERIES[2], 0.0),
        (own, "vs strangers of your rating", charts.SERIES[1], 0.24),
    ]
    for frame, label, colour, offset in series:
        part = frame[frame["tc_group"].isin(groups)]
        charts.estimate(ax, part, "tc_group", y="auc", lo="auc_lo", hi="auc_hi", order=groups, colour=colour, label=label, offset=offset)
    thin = set(auc1.loc[auc1["thin"], "tc_group"])
    for g in groups:
        if g in thin:
            ax.text(groups.index(g), 0.41, "too few\ngames", ha="center", va="bottom", fontsize=7, color=charts.INK_MUTED)
        elif g not in set(own["tc_group"]):
            ax.text(groups.index(g) + 0.24, 0.41, "no own\nfit", ha="center", va="bottom", fontsize=7, color=charts.INK_MUTED)
    charts.zero_line(ax, 0.5)
    ax.set_ylim(0.4, 1.0)
    ax.set_ylabel("AUC (95% CI)")
    ax.tick_params(axis="x", labelrotation=30)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=3, fontsize=8)
    charts.footnote(fig, f"Boosting, all features; controls with fewer than {MIN_FIT_GAMES} games are not drawn. Unseen year only where a year has {MIN_YEAR_TEST_GAMES}+ games and the other years {MIN_YEAR_TRAIN_GAMES}+.")
    fig.tight_layout()
    results.figure(fig, "1_auc")
    plt.close(fig)


def chart_drop(results: Results, drop1: pd.DataFrame, drop2: pd.DataFrame, groups: list[str], unit_of: dict[str, str]) -> None:
    fig, axes = charts.group_grid(groups, panel=(2.9, 2.4), sharey=True, title="AUC lost when one feature group is left out")
    order = list(GROUPS)
    for g, ax in axes.items():
        p1 = drop1[(drop1["tc_group"] == g) & (drop1["model"] == HEADLINE_MODEL)]
        if p1.empty or bool(p1["thin"].iloc[0]):
            charts.thin_note(ax)
            continue
        p2 = drop2[(drop2["unit"] == unit_of.get(g, "")) & (drop2["model"] == HEADLINE_MODEL)]
        wide = pd.DataFrame({"feature_group": order})
        for name, part in (("vs opponents", p1), ("vs strangers", p2)):
            part = part.set_index("feature_group").reindex(order)
            wide[name] = part["auc_lost"].to_numpy()
            wide[f"{name}_lo"] = part["auc_lost_lo"].to_numpy()
            wide[f"{name}_hi"] = part["auc_lost_hi"].to_numpy()
        charts.side_bars(ax, wide, "feature_group", order=order, sides=("vs opponents", "vs strangers"))
        charts.zero_line(ax)
        if ax.get_subplotspec().is_first_col():
            ax.set_ylabel("AUC lost (95% CI)")
        if g != unit_of.get(g):
            ax.text(0.98, 0.95, "strangers: time-class fit", transform=ax.transAxes, ha="right", va="top", fontsize=6.5, color=charts.INK_MUTED)
    charts.legend(axes[groups[0]].figure, loc="upper right")
    charts.footnote(axes[groups[0]].figure, f"Boosting; AUC with all features minus AUC without the group, 95% CI (paired bootstrap). No engine analysis for strangers. Controls under {MIN_FIT_GAMES} games not drawn.")
    results.figure(axes[groups[0]].figure, "2_drop_group")


def chart_vs_rest(results: Results, rest: pd.DataFrame, each: pd.DataFrame, units: list[str]) -> None:
    fig, ax = charts.single("How distinctive is each player against the rest of the pool?", size=(8.0, 0.55 * len(units) + 1.4))
    rng = np.random.default_rng(SEED)
    for i, u in enumerate(units):
        r = rest[rest["unit"] == u]
        if r.empty or bool(r["thin"].iloc[0]) or pd.isna(r["you_auc"].iloc[0]):
            ax.text(0.75, i, "too few strangers", ha="center", va="center", fontsize=8, color=charts.INK_MUTED)
            continue
        v = each[(each["unit"] == u) & (each["who"] == "stranger")]["auc"].to_numpy(dtype=float)
        ax.scatter(v, i + rng.uniform(-0.18, 0.18, len(v)), s=9, color=charts.OPPONENT, alpha=0.55, linewidths=0, label="one stranger vs the rest")
        r = r.iloc[0]
        ax.errorbar([r["strangers_auc"]], [i + 0.3], xerr=[[r["strangers_auc"] - r["strangers_auc_lo"]], [r["strangers_auc_hi"] - r["strangers_auc"]]], fmt="D", color=charts.INK_SECONDARY, markersize=5, label="strangers' mean")
        ax.errorbar([r["you_auc"]], [i - 0.3], xerr=[[r["you_auc"] - r["you_auc_lo"]], [r["you_auc_hi"] - r["you_auc"]]], fmt="o", color=charts.PLAYER, markersize=7, elinewidth=2, label=f"you (mean and range of {N_ME_DRAWS} subsamples)")
    ax.set_yticks(range(len(units)), [u.split(" (")[0] for u in units])  # every fit here is matched on band and control
    ax.invert_yaxis()
    ax.set_xlim(0.5, 1.0)
    ax.set_xlabel("AUC of the player vs the rest of the pool (0.5 = indistinguishable)")
    ax.grid(axis="y", visible=False)
    handles, labels = ax.get_legend_handles_labels()
    seen = dict(zip(labels, handles))
    ax.legend(seen.values(), seen.keys(), loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8)
    charts.footnote(fig, "Logistic regression, engine-free features, matched on rating band and exact control; same sample size on both sides.")
    fig.tight_layout()
    results.figure(fig, "3_vs_rest")


def chart_habits(results: Results, habits: pd.DataFrame, features: list[str], units: list[str]) -> None:
    import matplotlib.pyplot as plt

    ncols = 4
    nrows = int(np.ceil(len(features) / ncols))
    fig, grid = plt.subplots(nrows, ncols, figsize=(11.5, 2.7 * nrows + 0.6), squeeze=False)
    fig.suptitle("Your habits vs strangers of your rating (same rating band and time control)", x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    short = [u.replace("all ", "").split(" (")[0] for u in units]
    for idx, ax in enumerate(grid.flat):
        if idx >= len(features):
            ax.set_visible(False)
            continue
        f = features[idx]
        part = habits[(habits["feature"] == f) & habits["unit"].isin(units)].copy()
        label, unit, _ = FEATURE_INFO[f]
        scale = 100.0 if unit.startswith("share") else 1.0
        for col in ("you", "you_lo", "you_hi", "strangers", "strangers_lo", "strangers_hi"):
            part[col] = part[col] * scale
        part["x"] = part["unit"].map(dict(zip(units, short)))
        charts.estimate(ax, part, "x", y="you", lo="you_lo", hi="you_hi", order=short, colour=charts.PLAYER, label="you", offset=-0.12)
        charts.estimate(ax, part, "x", y="strangers", lo="strangers_lo", hi="strangers_hi", order=short, colour=charts.OPPONENT, label="strangers", offset=0.12)
        ax.set_title(label, fontsize=8.5)
        ax.set_ylabel(("% of " + unit.split("share of ", 1)[1]) if unit.startswith("share") else unit, fontsize=7.5)
        ax.set_xlim(-0.5, len(short) - 0.5)
        ax.grid(axis="x", visible=False)
    charts.legend(fig, loc="upper right")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    charts.footnote(fig, f"The {len(features)} most important features of the time-class fits matched on rating band and exact control, in plain units; 95% CIs (your games / strangers as clusters).")
    results.figure(fig, "4_habits")


def chart_years(results: Results, years: pd.DataFrame, groups: list[str], pools: list[str]) -> None:
    # Most controls have no year with enough games, so each time class with several controls also gets a
    # panel for all its controls together ("bullet (all)"), drawn in the same row.
    panels: dict[str, str] = {}
    for i, g in enumerate(groups):
        panels[g] = g
        cls = g.split(" ", 1)[0]
        if f"all {cls}" in pools and (i + 1 == len(groups) or not groups[i + 1].startswith(cls + " ")):
            panels[f"{cls} (all)"] = f"all {cls}"
    fig, axes = charts.group_grid(list(panels), panel=(2.5, 2.2), sharey=False, title="Trained on the other years, tested on one year (AUC)")
    part_all = years[(years["model"] == HEADLINE_MODEL) & (years["test_year"] != "all held-out")]
    year_order = sorted(part_all["test_year"].unique())
    for name, ax in axes.items():
        g = panels[name]
        ax.set_ylim(0.45, 1.0)
        part = part_all[(part_all["tc_group"] == g) & ~part_all["thin"]]
        if part.empty:
            charts.thin_note(ax, "no year with enough games")
            ax.set_yticks([0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
            continue
        order = [y for y in year_order if y in set(part["test_year"])]
        charts.estimate(ax, part, "test_year", y="auc", lo="auc_lo", hi="auc_hi", order=order, colour=charts.PLAYER, label="unseen year")
        ref = float(part["auc_same_years"].iloc[0])
        ax.axhline(ref, color=charts.SERIES[1], linewidth=1.2, linestyle="--", label="same years (5-fold)")
        charts.zero_line(ax, 0.5)
        ax.set_xlim(-0.5, len(order) - 0.5)
        ax.grid(axis="x", visible=False)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    charts.legend(fig, loc="upper right")
    charts.footnote(fig, f"You vs your opponents in the same games, boosting with all features, 95% CI by game. Years with fewer than {MIN_YEAR_TEST_GAMES} games in a control are not drawn.")
    results.figure(fig, "5_unseen_year")


# --- main -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rebuild-features", action="store_true", help="recompute data/opponents/fingerprint_features.parquet from the raw archives")
    parser.add_argument("--download-log", type=Path, default=None, help="with --rebuild-features: use only opponents whose download finished according to this log")
    args = parser.parse_args()
    started = time.time()
    # One thread per fit: deterministic, and OpenMP's busy-waiting makes multi-threaded boosting
    # 50x slower when the machine is shared with other jobs.
    threadpool_limits(1)
    charts.style()

    data = load_data()
    me = data.username.lower()
    groups = data.tc_groups
    classes = list(dict.fromkeys(data.tc_class[g] for g in groups))
    opp_root = repo_root() / "data" / "opponents"
    features_path = opp_root / "fingerprint_features.parquet"
    results = Results(SLUG)

    feats1 = step1_features(data)
    if args.rebuild_features:
        rebuild_features(feats1, opp_root, features_path, data.username, args.download_log)

    # ---------------- step 1: you vs your opponents in the same games ----------------
    t0 = time.time()
    units1 = {g: feats1[feats1["tc_group"] == g] for g in groups}
    units1.update({f"all {c}": feats1[feats1["time_class"] == c] for c in classes})
    auc_rows, drop_rows, year_rows, opp_habit_rows = [], [], [], []
    for label, part in units1.items():
        out = step1_unit(label, part)
        auc_rows += out["auc"]
        drop_rows += out["drop"]
        five = {r["model"]: r["auc"] for r in out["auc"]}
        year_rows += step1_years(label, part, five)
        opp_habit_rows += habits_vs_opponents(label, part)
        print(f"  step 1 {label}: {time.time() - t0:.0f} s", flush=True)
    years = pd.DataFrame(year_rows)
    held = years[years["test_year"] == "all held-out"]
    unseen = held.assign(test="unseen year (trained on other years)")[["tc_group", "test", "model", "auc", "auc_lo", "auc_hi", "pairwise", "pairwise_lo", "pairwise_hi", "games", "thin"]]
    auc1 = pd.concat([pd.DataFrame(auc_rows), unseen], ignore_index=True)
    drop1 = pd.DataFrame(drop_rows)

    # ---------------- step 2: you vs strangers of your rating ----------------
    t0 = time.time()
    table = pd.read_parquet(features_path)
    table[CLOCK] = table[CLOCK].clip(upper=CLOCK_CLIP)  # tables written before the clip existed carry a few clock glitches
    strangers = table[table["player"] != me].copy()
    strangers["tc_group"] = tc_group(strangers["time_class"], strangers["base_seconds"], strangers["increment_seconds"].fillna(0))
    mine = feats1[feats1["is_me"]].drop(columns=["is_me"]).copy()
    # Same precision as the strangers' stored features: float64 values next to float32 ones would let
    # boosting split on rounding noise (1/3 in float32 is not 1/3 in float64) and inflate the AUC.
    mine[STEP2_ALL] = mine[STEP2_ALL].astype("float32")
    for frame in (strangers, mine):
        frame["band"] = rating_band(frame["rating"], BAND).astype("float64")
        frame["band_tc"] = (frame["band"].astype(str) + ":" + frame["tc_group"].astype(str)).where(frame["band"].notna())
    mine = mine[mine["band"].notna()]
    strangers = strangers.reset_index(drop=True)
    mine = mine.reset_index(drop=True)
    ids = {p: f"stranger {i + 1:03d}" for i, p in enumerate(sorted(strangers["player"].unique()))}  # no names in the outputs

    units: list[Unit] = []
    for g in groups:
        units.append(Unit(g, "time control", data.tc_class[g], mine[mine["tc_group"] == g], strangers[strangers["tc_group"] == g], "band"))
    for c in classes:
        m_c, s_c = mine[mine["time_class"] == c], strangers[strangers["time_class"] == c]
        units.append(Unit(f"all {c} (matched on control)", "time class, matched on band and control", c, m_c, s_c, "band_tc"))
        units.append(Unit(f"all {c} (band only)", "time class, matched on band only", c, m_c, s_c, "band"))

    auc2_rows, drop2_rows, imp_rows, habit_rows, inc_rows, mix_rows, pair_rows, rest_rows, each_rows = [], [], [], [], [], [], [], [], []
    fitted: list[str] = []
    for unit in units:
        res = pooled_fit(unit, me)
        if res is None:
            print(f"  step 2 {unit.label}: too few matched games, no fit", flush=True)
            continue
        fitted.append(unit.label)
        auc2_rows += res["auc"]
        drop2_rows += res["drop"]
        inc_rows.append(res["increment"])
        mix_rows += res["mix"]
        if unit.fit != "time class, matched on band only":
            imp_rows += res["importance"]
            habit_rows += res["habits"]
            pair_rows += per_pair_test(unit, me)
            row, each = vs_rest(unit, me, ids)
            rest_rows.append(row)
            each_rows += each
        print(f"  step 2 {unit.label}: {time.time() - t0:.0f} s", flush=True)
    auc2 = pd.DataFrame(auc2_rows)
    # Every fit has >= MIN_FIT_GAMES games a side by construction; its CI resamples strangers, so few strangers is what makes it thin.
    auc2["thin"] = auc2["strangers"] < MIN_STRANGERS
    unit_names = {u.label: (u.tc_group, u.matching) for u in units}
    drop2 = pd.DataFrame(drop2_rows)
    habits = pd.DataFrame(habit_rows)
    rest = pd.DataFrame(rest_rows)
    each = pd.DataFrame(each_rows)

    # Which step-2 fit answers for each control: its own where it has one, else its time class matched on control.
    unit_of = {g: (g if g in fitted else f"all {data.tc_class[g]} (matched on control)") for g in groups}

    # ---------------- who went in ----------------
    sample_rows = []
    for g in groups + [f"all {c}" for c in classes]:
        cls = g.removeprefix("all ") if g.startswith("all ") else data.tc_class[g]
        is_pool = g.startswith("all ")
        s_part = strangers[strangers["time_class"] == cls] if is_pool else strangers[strangers["tc_group"] == g]
        m_part = units1[g]
        fit_label = f"all {cls} (matched on control)" if is_pool else unit_of[g]
        matched = auc2[(auc2["unit"] == fit_label) & (auc2["model"] == HEADLINE_MODEL)]
        sample_rows.append({
            "tc_group": g, "your_games": int(m_part["game_uuid"].nunique()), "games_in_data": int(data.games["tc_group"].astype(str).isin([g] if not is_pool else [x for x in groups if data.tc_class[x] == cls]).sum()),
            "stranger_games": len(s_part), "strangers": int(s_part["player"].nunique()),
            "strangers_with_enough_games": int((s_part.groupby("player").size() >= MIN_MATCHED).sum()),
            "strangers_fit_group": unit_names[fit_label][0], "matched_games_per_side": int(matched["you_games"].iloc[0]) if len(matched) else 0,
            "thin": int(m_part["game_uuid"].nunique()) < MIN_FIT_GAMES,
        })  # fmt: skip
    sample = pd.DataFrame(sample_rows)

    # Habits for the chart: most important features (mean rank) across the time-class fits matched on control.
    matched_units = [f"all {c} (matched on control)" for c in classes if f"all {c} (matched on control)" in fitted]
    mean_rank = habits[habits["unit"].isin(matched_units)].groupby("feature")["importance_rank"].mean().sort_values(kind="stable")
    chart_features = list(mean_rank.index[:TOP_HABITS])
    habits["in_chart"] = habits["feature"].isin(chart_features)
    habits["thin"] = habits["you_games"] < MIN_FIT_GAMES

    # Your habits by year (you only), for the chart habits.
    year_habit_rows = []
    for label, part in units1.items():
        mine_part = part[part["is_me"]]
        for (yr,), ypart in mine_part.groupby(["year"]):
            for f in chart_features:
                est, lo, hi = mean_ci(plain_values(ypart, f), ypart["game_uuid"].to_numpy(), seed_for("yearhab", label, yr, f))
                n = int((~np.isnan(plain_values(ypart, f))).sum())
                year_habit_rows.append({"tc_group": label, "year": int(yr), **feature_columns(f), "you": est, "you_lo": lo, "you_hi": hi, "games": n, "thin": n < MIN_GROUP_GAMES})
    year_habits = pd.DataFrame(year_habit_rows)

    try:
        check, _ = self_check(data)
    except Exception as exc:  # the raw archive may be absent in a partial clone
        print(f"  self-check skipped: {type(exc).__name__}: {exc}")
        check = pd.DataFrame(columns=["feature", "max_abs_diff", "rows_checked"])

    # ---------------- tables ----------------
    results.table("0_sample", sample, f"Games per time-control group: yours (both sides kept, >= {MIN_OWN_MOVES} own moves), the strangers' games in that control, and which step-2 fit (tc_group, matched on band and control) answers for it; thin = fewer than {MIN_FIT_GAMES} games")
    results.table("0_self_check", check, f"Light replay used for the strangers' games vs the stored enrichment, on {SELF_CHECK_GAMES} of your games: largest absolute difference per feature")
    results.table("1_auc", auc1, f"You vs your opponents in the same games: AUC and pairwise accuracy (share of games where your row scores higher), same years (5-fold by game) and unseen year; thin = fewer than {MIN_FIT_GAMES} games")
    results.table("1b_vs_strangers", tidy_units(auc2, unit_names), f"You vs strangers of your rating: pooled AUC and accuracy per fit (one control, or a time class matched on band and control, or on band only); CIs resample strangers as clusters; thin = fewer than {MIN_STRANGERS} strangers")
    results.table("2_drop_group", drop1, "You vs your opponents: AUC lost when each feature group is left out (paired bootstrap by game) and AUC of the group alone")
    results.table("2b_drop_group_strangers", tidy_units(drop2, unit_names), "You vs strangers: AUC lost when each feature group is left out, per fit; band-only vs control-matched shows how much of the clock signal is the increment")
    results.table("2c_increment", tidy_units(pd.DataFrame(inc_rows), unit_names), "Share of games with an increment, you vs strangers, in each matched sample (with an increment players can spend more time per move and their clock stays higher)")
    results.table("2d_control_mix", tidy_units(pd.DataFrame(mix_rows), unit_names), "Time-control mix of each matched sample, you vs strangers")
    results.table("3_vs_rest", tidy_units(rest, unit_names), f"Each stranger vs the rest of the pool and {N_ME_DRAWS} same-sized subsamples of you vs the rest: mean AUC (yours with the 2.5-97.5% range of the draws, strangers' with a bootstrap CI), quartiles, share of strangers less distinctive than you")
    results.table("3b_vs_rest_each", tidy_units(each, unit_names), "Every 'vs the rest' AUC behind the chart (strangers anonymised)")
    results.table("3c_per_pair", tidy_units(pd.DataFrame(pair_rows), unit_names), "Per-pair test: you vs each stranger and stranger vs stranger (same sizes and procedure), per feature set; difference paired by stranger")
    results.table("4_habits", tidy_units(habits, unit_names), "Habits in plain units, you vs band-matched strangers per fit (seconds, shares, move numbers), with the feature's importance rank in that fit")
    results.table("4b_importance", tidy_units(pd.DataFrame(imp_rows), unit_names), "Most important features per step-2 fit: boosting permutation importance (AUC drop, held-out fold) and standardised logistic coefficient")
    results.table("4c_habits_vs_opponents", pd.DataFrame(opp_habit_rows), "Habits in plain units, you vs your opponents in the same games (difference bootstrapped by game); under10_buffer counts the clock beyond one increment")
    results.table("5_unseen_year", years, "You vs your opponents: trained on the other years, tested on one; 'all held-out' pools the test years with enough games")
    results.table("5b_habits_by_year", year_habits, "Your habits by year (the habits in the 4_habits chart), CI by game")

    # ---------------- charts ----------------
    chart_auc(results, auc1, years, auc2, groups, unit_of)
    chart_drop(results, drop1, drop2, groups, unit_of)
    rest_units = [u for u in [*[g for g in groups if g in fitted], *matched_units] if u in set(rest["unit"])]
    chart_vs_rest(results, rest, each, rest_units)
    chart_habits(results, habits, chart_features, matched_units)
    chart_years(results, years, groups, [f"all {c}" for c in classes if sum(data.tc_class[g] == c for g in groups) > 1])
    results.finish()
    print(f"done in {time.time() - started:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
