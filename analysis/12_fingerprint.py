"""Question 12: style fingerprint.

Can a classifier pick the player's games out from other people's, and on
which features? Two steps.

Step 1 (no new data): one feature row per player-game for both sides of the
player's own games (you vs the opponent in the same game): clock-usage
profile, move-kind rates, opening choice by colour, book depth, game length
and the engine-based error distribution. Nothing that encodes the result,
the rating or the names. Regularised logistic regression and gradient
boosting, cross-validated by game (both rows of a game in the same fold)
and by year (train on other years, test on one), with and without the
clock / opening / engine feature groups.

Step 2 (the honest test): the same engine-free features for a sample of
games of the player's most frequent opponents against third parties
(downloaded by scripts/download_opponents.py into data/opponents/<name>/raw,
gitignored). (a) Is the player separable from a same-rated stranger?
(b) Is that separability larger than between two strangers of the same
rating band? Rating-band matched, game-level CV, with and without clock and
opening features. The compact feature table is written to
data/opponents/fingerprint_features.parquet (committed) and reused when it
exists, so the report is reproducible from the repository alone
(--rebuild-features recomputes it from the raw archives).

Run from the repo root: .venv/bin/python analysis/12_fingerprint.py
Options: --skip-step2, --rebuild-features, --max-wait-minutes N (how long to
wait for the download log /tmp/opponents2.log to say "done:"; default 40).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import zlib
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

from chess_analysis.config import load_settings
from chess_analysis.dataset import build_tables
from chess_analysis.enrich import PIECE_NAMES, _time_columns, parse_time_control, phase
from chess_analysis.engine import position_key
from chess_analysis.openings import game_openings, load_book
from chess_analysis.parse import parse_game
from chess_analysis.toolkit import (
    SIDE_COLOURS,
    TIME_CLASSES,
    Report,
    ci_column,
    cluster_bootstrap_mean,
    figure_style,
    load_data,
    rating_band,
    repo_root,
)

SLUG = "12_fingerprint"
SEED = 12
N_BOOT = 500
N_FOLDS = 5
MIN_OWN_MOVES = 8  # games where a side made fewer moves have no usable profile
CAP_PER_CLASS = 120  # opponent games sampled per time class (deterministic, seeded by name)
MIN_MATCHED = 30  # matched games per side for a pairwise classifier
MIN_YEAR_GAMES = 100  # games needed for a year to be a held-out test year
N_PARTNERS = 2  # stranger-vs-stranger pairs per stranger
N_ME_DRAWS = 20  # subsampled "me vs rest" draws in the pooled comparison
BAND = 100
BOOK_MAX_PLY = 40  # Lichess named lines never go deeper
# The same sentence in both modes so a plain run reproduces the committed report byte for byte.
SOURCE_NOTE = "Features from `{path}`, built from every opponent whose archive download completed (`--rebuild-features` recomputes it from the raw archives under `data/opponents/<name>/raw/`, which are not committed)."
CLOCK_CLIP = 1.5  # clock fractions of base above this are clock glitches (a few stranger games in odd time controls)
LOG_PATH = Path("/tmp/opponents2.log")

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
GROUPS = {"clock": CLOCK, "moves": MOVES, "opening": OPENING, "engine": ENGINE}
META = ["game_uuid", "color", "player", "time_class", "rating", "year", "rated", "base_seconds", "increment_seconds", "vs_player"]

FEATURE_SETS_STEP1 = {
    "all": CLOCK + MOVES + OPENING + ENGINE,
    "no clock": MOVES + OPENING + ENGINE,
    "no opening": CLOCK + MOVES + ENGINE,
    "no clock, no opening": MOVES + ENGINE,
    "no engine (step-2 set)": CLOCK + MOVES + OPENING,
    "clock only": CLOCK,
    "opening only": OPENING,
    "moves only": MOVES,
    "engine only": ENGINE,
}
FEATURE_SETS_STEP2 = {
    "all (engine-free)": CLOCK + MOVES + OPENING,
    "no clock": MOVES + OPENING,
    "no opening": CLOCK + MOVES,
    "no clock, no opening": MOVES,
}


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    if np.isnan(lo):
        return f"{est:{f}}"
    return f"{est:{f}} [{lo:{f}}, {hi:{f}}]"


# --- features -----------------------------------------------------------------


def add_move_kinds(moves: pd.DataFrame) -> pd.DataFrame:
    """En passant and castling side from the stored uci / fen_before (step-1 moves)."""
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
    for k in (10, 20, 40):
        f[f"left_m{k}"] = left.where(own_idx == k).groupby(key).max()
    f["left_end"] = left.groupby(key).last()
    f["under10"] = (m["clock_before_seconds"] < 10).groupby(key).mean()
    f["fast1"] = (m["time_spent_seconds"] < 1).groupby(key).mean()

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
    f["queen_early"] = ((m["piece"] == "queen") & (own_idx <= 10)).groupby(key).sum().astype(float)

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
        f["ep_ge01"] = (m["ep_loss"] >= 0.1).groupby(key).mean()
        f["ep_ge02"] = (m["ep_loss"] >= 0.2).groupby(key).mean()
        f["cp_mean"] = m["cp_loss"].groupby(key).mean()
        f["cp_ge300"] = (m["cp_loss"] >= 300).groupby(key).mean()
        f["best_share"] = m["is_best_move"].astype(float).groupby(key).mean()
    f = f.reset_index()
    f = f[f["n_moves"] >= MIN_OWN_MOVES]
    return f


def step1_features(data) -> pd.DataFrame:
    """Both sides of the player's own games, with the game meta columns."""
    moves = add_move_kinds(data.moves)
    openings = data.games.set_index("uuid")[["opening_eco", "book_ply"]]
    feats = game_features(moves, openings, with_engine=True)
    games = data.games.set_index("uuid")
    g = games.reindex(feats["game_uuid"])
    is_me = (feats["color"].to_numpy() == g["my_color"].to_numpy())
    rated = g["rated"].to_numpy(dtype=bool)
    # Opponent's pre-game rating proxy: post-game rating + the player's change (rated), post-game rating (unrated).
    opp_pre = np.where(rated, g["opponent_rating_after"].astype("float64").to_numpy() + g["my_rating_change"].astype("float64").to_numpy(), g["opponent_rating_after"].astype("float64").to_numpy())
    feats["player"] = np.where(is_me, data.username, "opp:" + g["opponent"].astype(str).str.lower().to_numpy())
    feats["time_class"] = g["time_class"].to_numpy()
    feats["rating"] = np.where(is_me, g["my_rating_before"].astype("float64").to_numpy(), opp_pre)
    feats["year"] = g["year"].to_numpy()
    feats["rated"] = rated
    feats["base_seconds"] = g["base_seconds"].to_numpy(dtype=float)
    feats["increment_seconds"] = g["increment_seconds"].to_numpy(dtype=float)
    feats["vs_player"] = True
    feats["is_me"] = is_me
    # Keep only games where both sides survived the move filter (paired design).
    both = feats.groupby("game_uuid")["color"].transform("size") == 2
    return feats[both].reset_index(drop=True)


# --- light replay for the opponents' archives -----------------------------------------


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


def complete_opponents(log_path: Path, root: Path) -> tuple[list[str], str]:
    """Opponents whose download finished, read from the download log; falls back to every raw dir."""
    dirs = sorted(p.parent.name for p in root.glob("*/raw") if any(p.glob("*.json")))
    if not log_path.exists():
        return dirs, f"no download log at {log_path}; using every opponent directory with saved months ({len(dirs)})"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    entries = [(int(mt.group(1)), mt.group(3)) for ln in lines if (mt := re.match(r"\[(\d+)/(\d+)\] (\S+)", ln))]
    done = any(ln.startswith("done:") for ln in lines)
    names: list[str] = []
    for i, (idx, name) in enumerate(entries):
        later = i + 1 < len(entries) and entries[i + 1][0] == idx + 1
        if (later or done) and (root / name / "raw").exists() and any((root / name / "raw").glob("*.json")):
            names.append(name)
    status = "has finished" if done else f"was still running ({len(entries)} of {entries[-1][0] if entries else '?'} started)"
    return names, f"The opponent download {status} ({log_path}): {len(names)} opponents with a complete archive used"


def wait_for_download(log_path: Path, max_minutes: float) -> None:
    if not log_path.exists():
        return
    started = time.time()
    while time.time() - started < max_minutes * 60:
        if "done:" in subprocess.run(["tail", "-n", "3", str(log_path)], capture_output=True, text=True).stdout:
            return
        print(f"  waiting for the opponent download ({(time.time() - started) / 60:.0f} min)...", flush=True)
        time.sleep(60)


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


def self_check(data, book: dict, n_games: int = 300) -> pd.DataFrame:
    """Light replay vs the stored enrichment on a sample of the player's own games: max |difference| per feature."""
    settings = load_settings()
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
    cols = CLOCK + MOVES + OPENING
    diff = (light.loc[common, cols].astype(float).fillna(-1) - stored.loc[common, cols].astype(float).fillna(-1)).abs().max()
    return pd.DataFrame({"feature": diff.index, "max_abs_diff": diff.to_numpy()}), len(common)


# --- classifiers and metrics ------------------------------------------------------------


def make_model(kind: str) -> object:
    if kind == "logistic":
        return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)), ("scale", StandardScaler()), ("lr", LogisticRegression(C=0.5, max_iter=2000))])
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.06, max_leaf_nodes=15, min_samples_leaf=20, l2_regularization=1.0, early_stopping=False, random_state=SEED)


def oof_predict(X: np.ndarray, y: np.ndarray, folds: list[tuple[np.ndarray, np.ndarray]], kind: str) -> np.ndarray:
    p = np.full(len(y), np.nan)
    for train, test in folds:
        model = make_model(kind)
        model.fit(X[train], y[train])
        p[test] = model.predict_proba(X[test])[:, 1]
    return p


def game_folds(groups: np.ndarray, n_folds: int = N_FOLDS) -> list[tuple[np.ndarray, np.ndarray]]:
    return list(GroupKFold(n_splits=n_folds).split(np.zeros(len(groups)), groups=groups))


def stratified_folds(y: np.ndarray, seed: int, n_folds: int = N_FOLDS) -> list[tuple[np.ndarray, np.ndarray]]:
    return list(StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=seed).split(np.zeros(len(y)), y))


def _weighted_auc(order_y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """AUC for each row of weights `w` (n_boot x n), given labels sorted by increasing score."""
    neg = w * (1 - order_y)
    pos = w * order_y
    cum_neg = np.cumsum(neg, axis=1) - neg  # negatives strictly below (ties ignored: scores are continuous)
    return (pos * cum_neg).sum(axis=1) / np.maximum(pos.sum(axis=1) * neg.sum(axis=1), 1e-12)


def auc_with_ci(y: np.ndarray, p: np.ndarray, clusters: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """ROC AUC with a CI from resampling clusters (games, or players where asked)."""
    order = np.argsort(p, kind="stable")
    oy = y[order].astype(float)
    base = _weighted_auc(oy[None, :], np.ones((1, len(y))))[0]
    codes, inv = np.unique(clusters[order], return_inverse=True)
    if len(codes) < 2:
        return float(base), np.nan, np.nan
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(codes), size=(n_boot, len(codes)))
    counts = np.stack([np.bincount(d, minlength=len(codes)) for d in draws])
    boot = _weighted_auc(oy[None, :], counts[:, inv].astype(float))
    lo, hi = np.nanquantile(boot, [0.025, 0.975])
    return float(base), float(lo), float(hi)


def mean_with_cluster_ci(values: np.ndarray, clusters: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    return cluster_bootstrap_mean(pd.Series(values, dtype="float64"), pd.Series(clusters), n_boot=n_boot, seed=seed)


def pairwise_accuracy(frame: pd.DataFrame, p: np.ndarray) -> tuple[float, float, float]:
    """Share of games where the player's row scores higher than the opponent's (step-1 paired design)."""
    t = pd.DataFrame({"g": frame["game_uuid"].to_numpy(), "me": frame["is_me"].to_numpy(), "p": p})
    wide = t.pivot(index="g", columns="me", values="p")
    correct = (wide[True] > wide[False]).astype(float).to_numpy()
    return mean_with_cluster_ci(correct, wide.index.to_numpy())


def run_step1_models(feats: pd.DataFrame, report_rows: list[dict], tc: str) -> dict[str, np.ndarray]:
    """Grouped-by-game CV for every feature set and both models; returns OOF predictions for the full set."""
    y = feats["is_me"].to_numpy(dtype=int)
    folds = game_folds(feats["game_uuid"].to_numpy())
    oof = {}
    for set_name, cols in FEATURE_SETS_STEP1.items():
        X = feats[cols].to_numpy(dtype=float)
        for kind in ("logistic", "boosting"):
            p = oof_predict(X, y, folds, kind)
            auc = auc_with_ci(y, p, feats["game_uuid"].to_numpy())
            acc = mean_with_cluster_ci(((p >= 0.5) == y).astype(float), feats["game_uuid"].to_numpy())
            pair = pairwise_accuracy(feats, p)
            report_rows.append({"time_class": tc, "features": set_name, "model": kind, "auc": auc[0], "auc_lo": auc[1], "auc_hi": auc[2], "acc": acc[0], "acc_lo": acc[1], "acc_hi": acc[2], "pair": pair[0], "pair_lo": pair[1], "pair_hi": pair[2], "games": int(feats["game_uuid"].nunique())})
            if set_name == "all":
                oof[kind] = p
    return oof


def run_step1_years(feats: pd.DataFrame, tc: str) -> pd.DataFrame:
    """Leave-one-year-out with the full feature set: train on the other years, test on one."""
    y = feats["is_me"].to_numpy(dtype=int)
    X = feats[FEATURE_SETS_STEP1["all"]].to_numpy(dtype=float)
    years = feats["year"].to_numpy()
    rows = []
    counts = feats.groupby("year")["game_uuid"].nunique()
    test_years = [yr for yr, n in counts.items() if n >= MIN_YEAR_GAMES]
    pooled = {kind: np.full(len(y), np.nan) for kind in ("logistic", "boosting")}
    for yr in test_years:
        test = np.flatnonzero(years == yr)
        train = np.flatnonzero(years != yr)
        row = {"time_class": tc, "test year": int(yr), "games": int(counts[yr])}
        for kind in ("logistic", "boosting"):
            model = make_model(kind)
            model.fit(X[train], y[train])
            p = model.predict_proba(X[test])[:, 1]
            pooled[kind][test] = p
            auc = auc_with_ci(y[test], p, feats["game_uuid"].to_numpy()[test], n_boot=300)
            row[f"{kind}_auc"], row[f"{kind}_lo"], row[f"{kind}_hi"] = auc
            row[f"{kind}_pair"], row[f"{kind}_pair_lo"], row[f"{kind}_pair_hi"] = pairwise_accuracy(feats.iloc[test], p)
        rows.append(row)
    table = pd.DataFrame(rows)
    mask = ~np.isnan(pooled["logistic"])
    if mask.any():
        row = {"time_class": tc, "test year": "all held-out", "games": int(feats.loc[mask, "game_uuid"].nunique())}
        for kind in ("logistic", "boosting"):
            auc = auc_with_ci(y[mask], pooled[kind][mask], feats["game_uuid"].to_numpy()[mask])
            row[f"{kind}_auc"], row[f"{kind}_lo"], row[f"{kind}_hi"] = auc
            row[f"{kind}_pair"], row[f"{kind}_pair_lo"], row[f"{kind}_pair_hi"] = pairwise_accuracy(feats[mask], pooled[kind][mask])
        table = pd.concat([table, pd.DataFrame([row])], ignore_index=True)
    return table


def importances(X: pd.DataFrame, y: np.ndarray, folds: list[tuple[np.ndarray, np.ndarray]], top: int = 12) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Standardised logistic coefficients (fit on everything) and boosting permutation importance (held-out fold)."""
    lr = make_model("logistic").fit(X.to_numpy(dtype=float), y)
    coef = pd.Series(lr.named_steps["lr"].coef_[0], index=X.columns)
    coef_table = coef.reindex(coef.abs().sort_values(ascending=False).index)[:top].rename("coef").reset_index().rename(columns={"index": "feature"})
    train, test = folds[0]
    hgb = make_model("boosting").fit(X.to_numpy(dtype=float)[train], y[train])
    perm = permutation_importance(hgb, X.to_numpy(dtype=float)[test], y[test], scoring="roc_auc", n_repeats=5, random_state=SEED, n_jobs=1)
    perm_table = pd.DataFrame({"feature": X.columns, "auc_drop": perm.importances_mean, "sd": perm.importances_std}).sort_values("auc_drop", ascending=False).head(top).reset_index(drop=True)
    return coef_table, perm_table


def side_means(feats: pd.DataFrame, cols: list[str], n_boot: int = N_BOOT) -> pd.DataFrame:
    """Player vs opponents per-game means of features in the same games, with the paired difference (bootstrap by game)."""
    rows = []
    mine = feats[feats["is_me"]].set_index("game_uuid")
    theirs = feats[~feats["is_me"]].set_index("game_uuid")
    common = mine.index.intersection(theirs.index)
    mine, theirs = mine.loc[common], theirs.loc[common]
    for col in cols:
        a = mine[col].to_numpy(dtype=float)
        b = theirs[col].to_numpy(dtype=float)
        ok = ~np.isnan(a) & ~np.isnan(b)
        pa = mean_with_cluster_ci(a[ok], common.to_numpy()[ok], n_boot=n_boot)
        pb = mean_with_cluster_ci(b[ok], common.to_numpy()[ok], n_boot=n_boot)
        pd_ = mean_with_cluster_ci(a[ok] - b[ok], common.to_numpy()[ok], n_boot=n_boot)
        rows.append({"feature": col, "you": pa[0], "you_lo": pa[1], "you_hi": pa[2], "opp": pb[0], "opp_lo": pb[1], "opp_hi": pb[2], "diff": pd_[0], "diff_lo": pd_[1], "diff_hi": pd_[2], "games": int(ok.sum())})
    return pd.DataFrame(rows)


# --- step 2 helpers -----------------------------------------------------------------------


def band_match(a: pd.DataFrame, b: pd.DataFrame, rng: np.random.Generator, ratio: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Subsample both frames so that their rating-band histograms agree (b may keep up to `ratio` x a's count per band)."""
    ia, ib = [], []
    for band in sorted(set(a["band"].dropna()) & set(b["band"].dropna())):
        xa = a.index[a["band"] == band].to_numpy()
        xb = b.index[b["band"] == band].to_numpy()
        ka = min(len(xa), len(xb))
        kb = min(len(xb), ratio * ka)
        ia.extend(sorted(rng.choice(xa, ka, replace=False)))
        ib.extend(sorted(rng.choice(xb, kb, replace=False)))
    return a.loc[ia], b.loc[ib]


def pair_aucs(a: pd.DataFrame, b: pd.DataFrame, seed: int) -> dict[str, float] | None:
    """Logistic-regression AUC for each step-2 feature set, a vs b, stratified 5-fold by game."""
    rng = np.random.default_rng(seed)
    a, b = band_match(a, b, rng)
    if len(a) < MIN_MATCHED or len(b) < MIN_MATCHED:
        return None
    frame = pd.concat([a, b], ignore_index=True)
    y = np.r_[np.ones(len(a), dtype=int), np.zeros(len(b), dtype=int)]
    folds = stratified_folds(y, seed)
    out = {"n": int(len(a))}
    for set_name, cols in FEATURE_SETS_STEP2.items():
        p = oof_predict(frame[cols].to_numpy(dtype=float), y, folds, "logistic")
        out[set_name] = auc_with_ci(y, p, np.arange(len(y)), n_boot=1)[0]
    return out


def tc_mix(frame: pd.DataFrame, top: int = 3) -> tuple[str, float]:
    """'3|2 72%, 3|0 20%, 5|0 4%' for the most common time controls, and the share of games with an increment."""
    base = frame["base_seconds"].astype(float)
    inc = frame["increment_seconds"].fillna(0).astype(float)
    labels = pd.Series([f"{b / 60:g}|{i:g}" for b, i in zip(base, inc)], index=frame.index)
    shares = labels.value_counts(normalize=True)
    return ", ".join(f"{k} {v:.0%}" for k, v in shares.head(top).items()), float((inc > 0).mean())


def percentile_with_ci(you_v: np.ndarray, str_v: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """Share of strangers whose 'vs the rest' AUC is below the mean of your draws, bootstrapped over strangers and draws."""
    est = float((str_v < you_v.mean()).mean())
    rng = np.random.default_rng(seed)
    yb = rng.choice(you_v, (n_boot, len(you_v))).mean(axis=1)
    sb = rng.choice(str_v, (n_boot, len(str_v)))
    boot = (sb < yb[:, None]).mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return est, float(lo), float(hi)


def pooled_folds(frame: pd.DataFrame, me: str, rng: np.random.Generator) -> list[tuple[np.ndarray, np.ndarray]]:
    """Folds where every stranger's games sit in one fold and the player's games are split at random."""
    players = frame["player"].to_numpy()
    strangers = np.array(sorted(set(players) - {me}))
    assign = dict(zip(strangers, rng.permutation(len(strangers)) % N_FOLDS))
    fold = np.array([assign.get(p, -1) for p in players])
    mine = fold == -1
    fold[mine] = rng.integers(0, N_FOLDS, mine.sum())
    return [(np.flatnonzero(fold != k), np.flatnonzero(fold == k)) for k in range(N_FOLDS)]


# --- main -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-step2", action="store_true")
    parser.add_argument("--rebuild-features", action="store_true", help="recompute data/opponents/fingerprint_features.parquet from the raw archives")
    parser.add_argument("--max-wait-minutes", type=float, default=40)
    args = parser.parse_args()
    started = time.time()
    figure_style()
    import matplotlib.pyplot as plt

    data = load_data()
    root = repo_root()
    opp_root = root / "data" / "opponents"
    features_path = opp_root / "fingerprint_features.parquet"
    report = Report(SLUG, "Style fingerprint: can a classifier pick your games out?")
    report.text(
        f"{len(data.games):,} standard live games and {len(data.moves):,} analysed moves from `load_data()`, "
        + ", ".join(f"{tc} {n:,}" for tc, n in data.games['time_class'].value_counts().reindex(TIME_CLASSES).fillna(0).astype(int).items()) + ". "
        "Every number is estimate [95% CI], bootstrapped over games unless stated. "
        "Context: this is the behavioural-stylometry question of McIlroy-Young et al. (2021) and the Maia project, "
        "asked of one club player with features that need no engine (plus the engine-based error profile in step 1)."
    )

    # ---------------- step 1 ----------------
    t0 = time.time()
    feats1 = step1_features(data)
    n_feat = len(FEATURE_SETS_STEP1["all"])
    report.heading("1. Features")
    report.text(
        f"One row per player-game (both sides of each of your games): {n_feat} features in four groups. "
        f"**clock** ({len(CLOCK)}): time spent per move as a fraction of the base time by phase, its spread and maximum, "
        "fraction of base left after your 10th / 20th / 40th / last move, share of moves started under 10 s, share of moves under 1 s. "
        f"**moves** ({len(MOVES)}): number of own moves, game length in plies, capture and check rates, whether / when (own move number) / which side you castled, "
        "share of moves by piece (king moves exclude castling), promotions, en-passant captures, queen moves in the first 10. "
        f"**opening** ({len(OPENING)}): first move as White (e4 / d4 / other) or reply as Black (to e4: e5 / c5 / other; to d4: d5 / Nf6 / other; other), "
        "ECO letter of the deepest named line, book depth (plies), whether you were the one to leave the named lines. "
        f"**engine** ({len(ENGINE)}; step 1 only): mean `ep_loss`, share of moves losing >= 0.1 and >= 0.2 ep, mean `cp_loss`, share >= 300 cp, share of engine-best moves. "
        f"Excluded on purpose: result, ratings, names, colour, time control, termination, final position evaluation. Games where a side made fewer than {MIN_OWN_MOVES} moves are dropped; "
        f"step 1 keeps {feats1['game_uuid'].nunique():,} games ({len(feats1):,} rows). "
        f"Clock fractions of the base time are clipped at {CLOCK_CLIP} (a handful of strangers' games in odd time controls carry clock glitches; none of yours is affected). "
        "Two reading notes: the `B_*` opening features on a Black row encode the opponent's first move as well as the reply (`B_other` means 'White opened with neither e4 nor d4', not a choice of the Black player); "
        "and `game_plies`, `eco_*`, `book_ply` describe the game, so in step 1 they are identical for both rows of a game and can only matter through interactions with the other features."
    )

    report.heading("2. Step 1: you vs your opponent in the same games")
    report.text(
        "Both rows of a game sit in the same cross-validation fold (5-fold grouped by game), so the model never sees the other half of a test game. "
        "AUC is the probability that a random row of yours scores above a random opponent row; accuracy is at a 0.5 threshold; "
        "'pairwise' is the share of games in which your row scores above the opponent's (the paired design's natural test; chance 0.5). "
        "Logistic regression: median imputation, standardisation, L2 (C = 0.5). Boosting: scikit-learn HistGradientBoosting (200 trees, 15 leaves), missing values handled natively."
    )
    rows1: list[dict] = []
    oof_all = {}
    year_tables = []
    imp_tables = {}
    sides = {}
    for tc in TIME_CLASSES:
        part = feats1[feats1["time_class"] == tc].reset_index(drop=True)
        oof_all[tc] = (part, run_step1_models(part, rows1, tc))
        year_tables.append(run_step1_years(part, tc))
        folds = game_folds(part["game_uuid"].to_numpy())
        imp_tables[tc] = importances(part[FEATURE_SETS_STEP1["all"]], part["is_me"].to_numpy(dtype=int), folds)
        top = list(dict.fromkeys([*imp_tables[tc][1]["feature"].head(6), *imp_tables[tc][0]["feature"].head(6)]))
        sides[tc] = side_means(part, top)
    table1 = pd.DataFrame(rows1)
    table1["AUC"] = ci_column(table1, "auc", "auc_lo", "auc_hi")
    table1["accuracy"] = ci_column(table1, "acc", "acc_lo", "acc_hi")
    table1["pairwise"] = ci_column(table1, "pair", "pair_lo", "pair_hi")
    for tc in TIME_CLASSES:
        part = table1[table1["time_class"] == tc]
        report.heading(f"{tc}: {int(part['games'].iloc[0]):,} games", level=3)
        report.table(part[["features", "model", "AUC", "accuracy", "pairwise"]])
    full = table1[(table1["features"] == "all")]
    best = full.sort_values("auc", ascending=False).iloc[0]
    no_clock = table1[(table1["features"] == "no clock") & (table1["model"] == "boosting")].set_index("time_class")
    no_open = table1[(table1["features"] == "no opening") & (table1["model"] == "boosting")].set_index("time_class")
    neither = table1[(table1["features"] == "no clock, no opening") & (table1["model"] == "boosting")].set_index("time_class")
    engine_only = table1[(table1["features"] == "engine only") & (table1["model"] == "boosting")].set_index("time_class")
    clock_only = table1[(table1["features"] == "clock only") & (table1["model"] == "boosting")].set_index("time_class")
    boost_all = full[full["model"] == "boosting"].set_index("time_class")
    logit_all = full[full["model"] == "logistic"].set_index("time_class")
    report.text(
        "Headline: with everything, boosting separates you from your opponents with AUC "
        + "; ".join(f"{tc} {fmt(*boost_all.loc[tc, ['auc', 'auc_lo', 'auc_hi']])}" for tc in TIME_CLASSES)
        + " (logistic regression "
        + " / ".join(f"{logit_all.loc[tc, 'auc']:.3f}" for tc in TIME_CLASSES)
        + f"; the best single number is {best['time_class']} {best['model']}). Dropping the clock group costs "
        + ", ".join(f"{boost_all.loc[tc, 'auc'] - no_clock.loc[tc, 'auc']:+.3f}" for tc in TIME_CLASSES)
        + " AUC (bullet / blitz / rapid), dropping the opening group "
        + ", ".join(f"{boost_all.loc[tc, 'auc'] - no_open.loc[tc, 'auc']:+.3f}" for tc in TIME_CLASSES)
        + "; with neither (moves + engine only) AUC is "
        + ", ".join(f"{neither.loc[tc, 'auc']:.3f}" for tc in TIME_CLASSES)
        + ". The engine error profile alone gives "
        + ", ".join(f"{engine_only.loc[tc, 'auc']:.3f}" for tc in TIME_CLASSES)
        + " and the clock profile alone "
        + ", ".join(f"{clock_only.loc[tc, 'auc']:.3f}" for tc in TIME_CLASSES)
        + "."
    )

    # Figure: AUC by feature set.
    fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=True)
    for ax, tc in zip(axes, TIME_CLASSES):
        part = table1[table1["time_class"] == tc]
        sets = list(FEATURE_SETS_STEP1)
        for j, (kind, colour) in enumerate((("logistic", SIDE_COLOURS["opponent"]), ("boosting", SIDE_COLOURS["player"]))):
            p = part[part["model"] == kind].set_index("features").reindex(sets)
            x = np.arange(len(sets)) + (j - 0.5) * 0.35
            ax.bar(x, p["auc"], width=0.35, color=colour, label=kind)
            ax.errorbar(x, p["auc"], yerr=[p["auc"] - p["auc_lo"], p["auc_hi"] - p["auc"]], fmt="none", ecolor="black", lw=0.8)
        ax.axhline(0.5, color="grey", lw=0.8, ls="--")
        ax.set_xticks(range(len(sets)))
        ax.set_xticklabels(sets, rotation=60, ha="right", fontsize=8)
        ax.set_title(tc)
        ax.set_ylim(0.45, 1.0)
    axes[0].set_ylabel("AUC (grouped 5-fold by game)")
    axes[0].legend(loc="upper right")
    report.figure(fig, "step1_auc_by_feature_set", "Step 1: you vs your opponents in the same games, AUC by feature set and model, 95% game-bootstrap CIs. Dashed line = chance.")
    plt.close(fig)

    report.heading("Train on other years, test on one year", level=3)
    report.text(f"Full feature set; a year is a test year when it has at least {MIN_YEAR_GAMES} games in the time class. 'all held-out' pools the held-out predictions. Pairwise = share of games where your row scores higher.")
    yt = pd.concat(year_tables, ignore_index=True)
    yt["logistic AUC"] = ci_column(yt, "logistic_auc", "logistic_lo", "logistic_hi")
    yt["boosting AUC"] = ci_column(yt, "boosting_auc", "boosting_lo", "boosting_hi")
    yt["pairwise (logistic)"] = ci_column(yt, "logistic_pair", "logistic_pair_lo", "logistic_pair_hi")
    yt["pairwise (boosting)"] = ci_column(yt, "boosting_pair", "boosting_pair_lo", "boosting_pair_hi")
    report.table(yt[["time_class", "test year", "games", "logistic AUC", "boosting AUC", "pairwise (logistic)", "pairwise (boosting)"]])
    held = yt[yt["test year"] == "all held-out"].set_index("time_class")
    report.text(
        "Year-out AUC (boosting): "
        + ", ".join(f"{tc} {fmt(*held.loc[tc, ['boosting_auc', 'boosting_lo', 'boosting_hi']])}" for tc in TIME_CLASSES if tc in held.index)
        + " vs the game-grouped "
        + ", ".join(f"{boost_all.loc[tc, 'auc']:.3f}" for tc in TIME_CLASSES)
        + ": the fingerprint transfers across years where the two are close ("
        + ", ".join(f"{tc}" for tc in TIME_CLASSES if tc in held.index and boost_all.loc[tc, "auc"] - held.loc[tc, "boosting_auc"] < 0.08)
        + "); the gap is how much of it is year-specific (rating level, time-control mix, repertoire of the time), and it is large in "
        + ", ".join(f"{tc}" for tc in TIME_CLASSES if tc in held.index and boost_all.loc[tc, "auc"] - held.loc[tc, "boosting_auc"] >= 0.08)
        + "."
    )

    report.heading("Which features carry it", level=3)
    report.text(
        "Left: the 12 largest standardised logistic coefficients (positive = more of it marks your row; fit on all games). "
        "Right: boosting permutation importance (AUC drop when the feature is shuffled in a held-out fold, mean of 5 shuffles). "
        "Below each: your per-game mean vs the opponents' in the same games for the top features, paired difference bootstrapped by game. "
        "Game-level features (`game_plies`, `eco_*`, `book_ply`) are identical for both rows of a game, so their difference is exactly zero: they rank only through interactions "
        "(e.g. 'a C-code game *and* this castling timing'). The `B_*` features on your Black rows also encode what the opponent opened with (`B_other` = White played neither e4 nor d4), not only your reply."
    )
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.5))
    for ax, tc in zip(axes, TIME_CLASSES):
        coef_table, perm_table = imp_tables[tc]
        merged = coef_table.merge(perm_table, on="feature", how="outer")
        report.heading(tc, level=4)
        report.text("Logistic coefficients (standardised):")
        report.table(coef_table, floatfmt=".2f")
        report.text("Boosting permutation importance (AUC drop):")
        report.table(perm_table, floatfmt=".3f")
        sm = sides[tc].copy()
        sm["you"] = ci_column(sm, "you", "you_lo", "you_hi", ".3f")
        sm["opponents"] = ci_column(sm, "opp", "opp_lo", "opp_hi", ".3f")
        sm["you − opp"] = ci_column(sm, "diff", "diff_lo", "diff_hi", ".3f")
        report.text("Per-game means, you vs opponents in the same games:")
        report.table(sm[["feature", "you", "opponents", "you − opp", "games"]])
        pt = perm_table.head(10).iloc[::-1]
        ax.barh(pt["feature"], pt["auc_drop"], xerr=pt["sd"], color=SIDE_COLOURS["player"])
        ax.set_title(tc)
        ax.set_xlabel("AUC drop when shuffled")
    fig.tight_layout()
    report.figure(fig, "step1_importance", "Step 1: boosting permutation importance of the ten most important features (held-out fold, 5 shuffles, bars = SD).")
    plt.close(fig)
    print(f"step 1 done in {time.time() - t0:.0f} s", flush=True)

    # ---------------- step 2 ----------------
    report.heading("3. Step 2: you vs same-rated strangers (opponents' games against third parties)")
    if args.skip_step2:
        report.text("Skipped (--skip-step2).")
    else:
        step2(report, data, feats1, opp_root, features_path, args, plt)

    report.heading("What this means for the player")
    report.text(summary_paragraph(table1, held, STEP2_SUMMARY))
    report.heading("Caveats")
    caveats = [
        "Step 1 is not the honest test: your opponent in a game is one specific other person, and the two rows share the game (same length, same opening line), so the classifier learns 'you vs the people you happened to play' and the position features are partly shared. Step 2 is the test that matters.",
        f"One opponent ({data.games['opponent'].str.lower().value_counts().index[0]}, {data.games['opponent'].str.lower().value_counts().iloc[0]} games) is over-represented in step 1's opponent class; the classifier may partly learn that one person.",
        "Engine features exist only for your own games (step 1); step 2 is engine-free for everyone, including you, so the two steps are not directly comparable in level.",
        "Step 2 uses the strangers' post-game rating in their third-party games as the matching variable (pre-game ratings are not in the archive); it is a matching variable, never a predictor, and the +/- 10 points of result noise are small against 100-point bands. Your own games are matched on `my_rating_before`.",
        "Strangers are sampled from their whole chess.com history (a deterministic random sample per time class), so a stranger's games span years and ratings, while yours are matched to their bands only; time controls within a time class differ between players (60 vs 60+1, 180 vs 180+2), which fraction-of-base normalisation only partly removes.",
        "Time-control mix is a confound for the clock features in step 2's band-matched fits: "
        + "; ".join(f"{tc}: you {m[0]}, strangers {m[1]}" + (" (confounded)" if m[2] else " (similar mix)") for tc, m in STEP2_SUMMARY.get("mixes", {}).items())
        + ". Time spent per move includes the increment, so where the increment shares differ `t_*`, `left_*` and `fast1` differences partly measure who plays with an increment. The fit matched on rating band and time control (section 3) is the check; its clock-feature means are the ones to quote as pacing habits.",
        f"The 'you (subsample) vs rest' baseline uses {N_ME_DRAWS} random {CAP_PER_CLASS}-game draws from your games; the draws overlap (most in rapid, where you have the fewest games), so their mean's CI is the spread between draws, not between independent samples. The percentile CIs bootstrap strangers and draws together.",
        "A stranger's sampled games span their whole chess.com history (often 5-10 years), so stranger-vs-stranger pairs may be separable partly by era (time-control fashions, premove habits, site changes) rather than by style; your games span 2021-2026. Both sides of the comparison carry this, but not necessarily equally.",
        "The opponent pool is your most frequent opponents, i.e. people who played in your rating range at the time, not a random sample of chess.com; some may be friends with their own idiosyncrasies.",
        "AUC bootstrap CIs resample games; in the pooled step-2 fit stranger rows are resampled by player (a stranger's games are not independent of each other). Per-pair AUCs rest on 30-120 games per side and are individually noisy; only their distribution is interpreted.",
        "Logistic regression needs imputation of missing phase / clock features (median); boosting handles them natively. 'Permutation importance' spreads credit across correlated features (the clock features are highly correlated).",
        "Nothing here says the features are *causes* of results; a fingerprint is a description of habits.",
    ]
    if not STEP2_SUMMARY.get("mixes"):
        caveats = [c for c in caveats if not c.startswith("Time-control mix is a confound")]
    report.text("\n".join(f"- {c}" for c in caveats))
    path = report.write()
    print(f"wrote {path} in {time.time() - started:.0f} s")


def step2(report: Report, data, feats1: pd.DataFrame, opp_root: Path, features_path: Path, args, plt) -> None:
    global STEP2_SUMMARY
    t0 = time.time()
    settings = load_settings()
    me = data.username.lower()
    book = load_book(settings.openings_dir)
    have_raw = any(opp_root.glob("*/raw/*.json"))
    manifest: pd.DataFrame | None = None
    source_note = ""
    if features_path.exists() and not args.rebuild_features:
        table = pd.read_parquet(features_path)
        table[CLOCK] = table[CLOCK].clip(upper=CLOCK_CLIP)  # tables written before the clip existed carry a few clock glitches
        source_note = SOURCE_NOTE.format(path=features_path.relative_to(repo_root()))
    elif have_raw:
        wait_for_download(LOG_PATH, args.max_wait_minutes)
        names, log_note = complete_opponents(LOG_PATH, opp_root)
        print(f"  {log_note}", flush=True)
        frames, infos = [], []
        for i, name in enumerate(names, 1):
            feats, info = sample_opponent(name, opp_root / name / "raw", me, book)
            infos.append(info)
            if feats is not None:
                frames.append(feats)
            if i % 20 == 0:
                print(f"  replayed {i}/{len(names)} opponents ({time.time() - t0:.0f} s)", flush=True)
        manifest = pd.DataFrame(infos)
        mine = feats1[feats1["is_me"]].drop(columns=["is_me"])
        table = pd.concat([mine, *frames], ignore_index=True)
        cols = META + CLOCK + MOVES + OPENING + ENGINE
        for c in ENGINE:
            table[c] = table[c].astype("float32")
        table = table[cols]
        for c in CLOCK + MOVES + OPENING:
            table[c] = table[c].astype("float32")
        table["rating"] = table["rating"].astype("float32")
        table["year"] = table["year"].astype("int16")
        # manifest columns travel inside the parquet as attributes of the player rows.
        table = table.merge(manifest.rename(columns={"opponent": "player"}), on="player", how="left")
        features_path.parent.mkdir(parents=True, exist_ok=True)
        table.to_parquet(features_path, index=False, compression="zstd")
        print(f"  features written to {features_path} in {time.time() - t0:.0f} s", flush=True)
        source_note = SOURCE_NOTE.format(path=features_path.relative_to(repo_root()))
    else:
        report.text("No opponent archives and no cached feature table: step 2 skipped.")
        return
    avail_cols = [c for c in table.columns if c.endswith("_available") or c.endswith("_sampled")]
    manifest = table.loc[table["player"] != me, ["player", *avail_cols]].drop_duplicates("player").reset_index(drop=True) if avail_cols else None

    strangers = table[table["player"] != me].copy()
    mine = table[table["player"] == me].copy()
    report.text(
        f"{source_note} {strangers['player'].nunique()} opponents, {len(strangers):,} of their games against third parties (standard rules, bullet / blitz / rapid, games against you excluded), "
        f"up to {CAP_PER_CLASS} per time class per opponent (a deterministic random sample seeded by the name from their whole archive) "
        f"and {len(mine):,} of your games. Everyone's features are the engine-free set ({len(CLOCK + MOVES + OPENING)} features); the engine group is dropped for you too. "
        "The strangers' games are replayed with a light python-chess pass (no legal-move counts, no engine) that reproduces the stored enrichment exactly on your own games (check below)."
    )
    # Self check.
    try:
        check, n_checked = self_check(data, book)
        worst = check.sort_values("max_abs_diff", ascending=False).head(3)
        report.text(f"Self-check on {n_checked} of your player-game rows: largest |light replay − stored enrichment| per feature: " + ", ".join(f"{r.feature} {r.max_abs_diff:.4f}" for r in worst.itertuples()) + ".")
    except Exception as exc:  # the raw archive of the player may be absent in a clone
        report.text(f"Self-check skipped ({type(exc).__name__}: {exc}).")

    # Manifest.
    counts = strangers.groupby(["player", "time_class"]).size().unstack(fill_value=0).reindex(columns=TIME_CLASSES, fill_value=0)
    report.heading("Who went in", level=3)
    summary = pd.DataFrame({"time_class": TIME_CLASSES, "opponents with games": [(counts[tc] > 0).sum() for tc in TIME_CLASSES], "stranger games": [int(counts[tc].sum()) for tc in TIME_CLASSES], "your games": [int((mine["time_class"] == tc).sum()) for tc in TIME_CLASSES], "opponents capped": [int((manifest[f"{tc}_available"] > CAP_PER_CLASS).sum()) if manifest is not None else -1 for tc in TIME_CLASSES]})
    report.table(summary)
    if manifest is not None:
        full = manifest.copy()
        for tc in TIME_CLASSES:
            full[tc] = full.apply(lambda r: f"{int(r[f'{tc}_sampled'])}/{int(r[f'{tc}_available'])}", axis=1)
        full = full.sort_values("player")
        report.text("<details><summary>All opponents: sampled/available games per time class</summary>\n\n" + "\n".join(f"- {r.player}: bullet {r.bullet}, blitz {r.blitz}, rapid {r.rapid}" for r in full.itertuples()) + "\n\n</details>")

    for frame in (strangers, mine):
        frame["band"] = rating_band(frame["rating"], BAND).astype("float64")
    mine = mine[mine["band"].notna()]

    # (a) and (b).
    report.heading("(a) You vs each stranger, (b) stranger vs stranger", level=3)
    report.text(
        f"For every stranger with at least {MIN_MATCHED} games in the time class, your games and theirs are matched band by band (100-point bands; per band the same number from each side, "
        f"random draws with a fixed seed), then a logistic regression is cross-validated 5-fold by game (each game is one row here). The same is done for pairs of strangers "
        f"({N_PARTNERS} random partners per stranger). Reported: the distribution of per-pair AUCs; the honest comparison is you-vs-stranger against stranger-vs-stranger under identical sample sizes and procedure."
    )
    dist_rows = []
    pair_records = []
    rng = np.random.default_rng(SEED)
    for tc in TIME_CLASSES:
        s_tc = strangers[strangers["time_class"] == tc]
        m_tc = mine[mine["time_class"] == tc]
        eligible = [p for p, n in s_tc.groupby("player").size().items() if n >= MIN_MATCHED]
        for p in eligible:
            res = pair_aucs(m_tc, s_tc[s_tc["player"] == p], seed=zlib.crc32(f"{tc}:{me}:{p}".encode()) % (2**31))
            if res is not None:
                pair_records.append({"time_class": tc, "kind": "you vs stranger", "a": me, "b": p, **res})
        for p in eligible:
            others = [q for q in eligible if q != p]
            partners = rng.choice(others, min(N_PARTNERS, len(others)), replace=False) if others else []
            for q in partners:
                res = pair_aucs(s_tc[s_tc["player"] == p], s_tc[s_tc["player"] == q], seed=zlib.crc32(f"{tc}:{p}:{q}".encode()) % (2**31))
                if res is not None:
                    pair_records.append({"time_class": tc, "kind": "stranger vs stranger", "a": p, "b": q, **res})
    pairs = pd.DataFrame(pair_records)
    sets2 = list(FEATURE_SETS_STEP2)
    # Distribution table and the difference with a bootstrap over strangers.
    for tc in TIME_CLASSES:
        part = pairs[pairs["time_class"] == tc]
        for set_name in sets2:
            row = {"time_class": tc, "features": set_name}
            for kind, label in (("you vs stranger", "you"), ("stranger vs stranger", "strangers")):
                v = part.loc[part["kind"] == kind, set_name].to_numpy(dtype=float)
                row[f"{label}_n"] = len(v)
                row[f"{label}_mean"] = v.mean() if len(v) else np.nan
                row[f"{label}_median"] = np.median(v) if len(v) else np.nan
                row[f"{label}_q25"], row[f"{label}_q75"] = (np.quantile(v, [0.25, 0.75]) if len(v) else (np.nan, np.nan))
                row[f"{label}_gt07"] = (v > 0.7).mean() if len(v) else np.nan
            # Bootstrap over strangers: each stranger contributes its you-vs-X AUC and the X-vs-Y AUCs where it is 'a'.
            a_vals = part[part["kind"] == "you vs stranger"].set_index("b")[set_name]
            b_vals = part[part["kind"] == "stranger vs stranger"].groupby("a")[set_name].mean()
            common = a_vals.index.intersection(b_vals.index)
            if len(common) >= 5:
                d = (a_vals.loc[common] - b_vals.loc[common]).to_numpy()
                est, lo, hi = mean_with_cluster_ci(d, common.to_numpy())
                row["diff"], row["diff_lo"], row["diff_hi"], row["diff_n"] = est, lo, hi, len(common)
            else:
                row["diff"], row["diff_lo"], row["diff_hi"], row["diff_n"] = np.nan, np.nan, np.nan, len(common)
            dist_rows.append(row)
    dist = pd.DataFrame(dist_rows)
    show = dist.copy()
    show["you vs stranger: mean (IQR)"] = show.apply(lambda r: f"{r['you_mean']:.3f} ({r['you_q25']:.3f}–{r['you_q75']:.3f}), n={int(r['you_n'])}" if r["you_n"] else "", axis=1)
    show["stranger vs stranger: mean (IQR)"] = show.apply(lambda r: f"{r['strangers_mean']:.3f} ({r['strangers_q25']:.3f}–{r['strangers_q75']:.3f}), n={int(r['strangers_n'])}" if r["strangers_n"] else "", axis=1)
    show["share AUC > 0.7 (you / strangers)"] = show.apply(lambda r: f"{r['you_gt07']:.2f} / {r['strangers_gt07']:.2f}" if r["you_n"] and r["strangers_n"] else "", axis=1)
    show["you − strangers (paired by stranger)"] = ci_column(show, "diff", "diff_lo", "diff_hi")
    report.table(show[["time_class", "features", "you vs stranger: mean (IQR)", "stranger vs stranger: mean (IQR)", "share AUC > 0.7 (you / strangers)", "you − strangers (paired by stranger)"]])

    full2 = dist[dist["features"] == sets2[0]].set_index("time_class")
    nc2 = dist[dist["features"] == "no clock"].set_index("time_class")
    no2 = dist[dist["features"] == "no opening"].set_index("time_class")
    nn2 = dist[dist["features"] == "no clock, no opening"].set_index("time_class")
    headline = []
    for tc in TIME_CLASSES:
        if tc in full2.index and full2.loc[tc, "you_n"] > 0:
            headline.append(
                f"{tc}: you vs a same-rated stranger AUC {full2.loc[tc, 'you_mean']:.3f} on average (n = {int(full2.loc[tc, 'you_n'])} strangers) vs {full2.loc[tc, 'strangers_mean']:.3f} between two strangers, "
                f"paired difference {fmt(full2.loc[tc, 'diff'], full2.loc[tc, 'diff_lo'], full2.loc[tc, 'diff_hi'])}; without clock features {fmt(nc2.loc[tc, 'diff'], nc2.loc[tc, 'diff_lo'], nc2.loc[tc, 'diff_hi'])}, "
                f"without opening features {fmt(no2.loc[tc, 'diff'], no2.loc[tc, 'diff_lo'], no2.loc[tc, 'diff_hi'])}, with neither {fmt(nn2.loc[tc, 'diff'], nn2.loc[tc, 'diff_lo'], nn2.loc[tc, 'diff_hi'])}"
            )
    report.text("Headline: " + ". ".join(headline) + ".")

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharey=True)
    bins = np.linspace(0.3, 1.0, 29)
    for ax, tc in zip(axes, TIME_CLASSES):
        part = pairs[pairs["time_class"] == tc]
        for kind, colour in (("stranger vs stranger", SIDE_COLOURS["opponent"]), ("you vs stranger", SIDE_COLOURS["player"])):
            v = part.loc[part["kind"] == kind, sets2[0]]
            if len(v):
                ax.hist(v, bins=bins, density=True, alpha=0.6, color=colour, label=f"{kind} (n={len(v)})")
        ax.axvline(0.5, color="grey", ls="--", lw=0.8)
        ax.set_title(tc)
        ax.set_xlabel("per-pair AUC (all engine-free features)")
    axes[0].set_ylabel("density")
    axes[0].legend(fontsize=8)
    report.figure(fig, "step2_pair_auc", "Step 2: distribution of per-pair AUCs, you vs each same-rated stranger (blue) and stranger vs stranger (orange), identical sizes and procedure.")
    plt.close(fig)

    # Pooled: you vs all strangers, and the fair baseline (120-game subsamples of you vs rest, each stranger vs rest).
    report.heading("Pooled: you vs all strangers at once", level=3)
    report.text(
        "All your games vs all strangers' games in the time class, band-matched collectively (equal band histograms), folds that keep every stranger's games together "
        "(a test stranger is never seen in training) and split yours at random; AUC CIs resample strangers as clusters and your games individually. "
        f"Because a stranger has at most {CAP_PER_CLASS} games per class and you have thousands, the fair baseline is: (i) each stranger vs the rest (band-matched, up to 3:1) and "
        f"(ii) {N_ME_DRAWS} random {CAP_PER_CLASS}-game subsamples of you vs the rest, both logistic regression, all engine-free features."
    )
    pooled_rows = []
    pooled_imp = {}
    tcm_rows = []
    tcm_samples = {}
    rng = np.random.default_rng(SEED + 1)
    rng_tc = np.random.default_rng(SEED + 2)  # separate stream: the time-control-matched check must not move the main numbers
    for tc in TIME_CLASSES:
        s_tc = strangers[strangers["time_class"] == tc]
        m_tc = mine[mine["time_class"] == tc]
        a, b = band_match(m_tc, s_tc, rng)
        if len(a) < MIN_MATCHED:
            continue
        frame = pd.concat([a, b], ignore_index=True)
        y = np.r_[np.ones(len(a), dtype=int), np.zeros(len(b), dtype=int)]
        folds = pooled_folds(frame, me, rng)
        clusters = np.where(frame["player"] == me, frame["game_uuid"], frame["player"]).astype(str)
        for set_name, cols in FEATURE_SETS_STEP2.items():
            for kind in ("logistic", "boosting"):
                p = oof_predict(frame[cols].to_numpy(dtype=float), y, folds, kind)
                auc = auc_with_ci(y, p, clusters)
                acc = mean_with_cluster_ci(((p >= 0.5) == y).astype(float), clusters)
                pooled_rows.append({"time_class": tc, "features": set_name, "model": kind, "auc": auc[0], "auc_lo": auc[1], "auc_hi": auc[2], "acc": acc[0], "acc_lo": acc[1], "acc_hi": acc[2], "you": len(a), "strangers": len(b), "n_strangers": int(b["player"].nunique())})
        pooled_imp[tc] = (importances(frame[FEATURE_SETS_STEP2[sets2[0]]], y, folds), a, b)
        # Same rating band *and* same time control (e.g. 3|2 vs 3|2 only): the check that the clock tells are not the increment.
        tc_key = lambda fr: (fr["band"].astype(str) + ":" + fr["base_seconds"].astype(float).astype(str) + "+" + fr["increment_seconds"].fillna(0).astype(float).astype(str)).where(fr["band"].notna())  # noqa: E731
        a2, b2 = band_match(m_tc.assign(band=tc_key(m_tc)), s_tc.assign(band=tc_key(s_tc)), rng_tc)
        if len(a2) >= MIN_MATCHED:
            frame2 = pd.concat([a2, b2], ignore_index=True)
            y2 = np.r_[np.ones(len(a2), dtype=int), np.zeros(len(b2), dtype=int)]
            folds2 = pooled_folds(frame2, me, rng_tc)
            clusters2 = np.where(frame2["player"] == me, frame2["game_uuid"], frame2["player"]).astype(str)
            for set_name in (sets2[0], "no clock"):
                for kind in ("logistic", "boosting"):
                    p = oof_predict(frame2[FEATURE_SETS_STEP2[set_name]].to_numpy(dtype=float), y2, folds2, kind)
                    auc = auc_with_ci(y2, p, clusters2)
                    tcm_rows.append({"time_class": tc, "features": set_name, "model": kind, "auc": auc[0], "auc_lo": auc[1], "auc_hi": auc[2], "you": len(a2), "strangers": len(b2), "n_strangers": int(b2["player"].nunique())})
            tcm_samples[tc] = (a2, b2)
        # Baselines.
        base_vals = []
        eligible = [p for p, n in s_tc.groupby("player").size().items() if n >= MIN_MATCHED]
        for p in eligible:
            xa = s_tc[s_tc["player"] == p]
            xb = s_tc[s_tc["player"] != p]
            r2 = np.random.default_rng(zlib.crc32(f"rest:{tc}:{p}".encode()) % (2**31))
            xa2, xb2 = band_match(xa, xb, r2, ratio=3)
            if len(xa2) < MIN_MATCHED:
                continue
            fr = pd.concat([xa2, xb2], ignore_index=True)
            yy = np.r_[np.ones(len(xa2), dtype=int), np.zeros(len(xb2), dtype=int)]
            pp = oof_predict(fr[FEATURE_SETS_STEP2[sets2[0]]].to_numpy(dtype=float), yy, stratified_folds(yy, SEED), "logistic")
            base_vals.append({"time_class": tc, "kind": "stranger vs rest", "who": p, "auc": auc_with_ci(yy, pp, np.arange(len(yy)), n_boot=1)[0], "n": len(xa2)})
        for d in range(N_ME_DRAWS):
            r2 = np.random.default_rng(zlib.crc32(f"me-draw:{tc}:{d}".encode()) % (2**31))
            sub = m_tc.loc[sorted(r2.choice(m_tc.index.to_numpy(), min(CAP_PER_CLASS, len(m_tc)), replace=False))]
            xa2, xb2 = band_match(sub, s_tc, r2, ratio=3)
            if len(xa2) < MIN_MATCHED:
                continue
            fr = pd.concat([xa2, xb2], ignore_index=True)
            yy = np.r_[np.ones(len(xa2), dtype=int), np.zeros(len(xb2), dtype=int)]
            pp = oof_predict(fr[FEATURE_SETS_STEP2[sets2[0]]].to_numpy(dtype=float), yy, stratified_folds(yy, SEED + d), "logistic")
            base_vals.append({"time_class": tc, "kind": "you (subsample) vs rest", "who": f"draw {d}", "auc": auc_with_ci(yy, pp, np.arange(len(yy)), n_boot=1)[0], "n": len(xa2)})
        pooled_imp[tc] = (*pooled_imp[tc], pd.DataFrame(base_vals))
    pooled = pd.DataFrame(pooled_rows)
    if len(pooled):
        pooled["AUC"] = ci_column(pooled, "auc", "auc_lo", "auc_hi")
        pooled["accuracy"] = ci_column(pooled, "acc", "acc_lo", "acc_hi")
        pooled["games (you / strangers)"] = pooled.apply(lambda r: f"{int(r['you']):,} / {int(r['strangers']):,} ({int(r['n_strangers'])} people)", axis=1)
        report.table(pooled[["time_class", "features", "model", "AUC", "accuracy", "games (you / strangers)"]])
        base_rows = []
        for tc in TIME_CLASSES:
            if tc not in pooled_imp:
                continue
            bv = pooled_imp[tc][3]
            for kind in ("you (subsample) vs rest", "stranger vs rest"):
                v = bv.loc[bv["kind"] == kind, "auc"].to_numpy(dtype=float)
                if len(v):
                    m_ci = mean_with_cluster_ci(v, bv.loc[bv["kind"] == kind, "who"].to_numpy())
                    base_rows.append({"time_class": tc, "who vs the rest": kind, "n": len(v), "mean AUC": fmt(*m_ci), "median": np.median(v), "q25": np.quantile(v, 0.25), "q75": np.quantile(v, 0.75), "share > 0.7": (v > 0.7).mean()})
        base = pd.DataFrame(base_rows)
        report.text(
            "Fair baseline at equal sample size (logistic regression, all engine-free features). The CI of the mean resamples strangers (one AUC each) or your draws "
            f"(the {N_ME_DRAWS} draws overlap slightly, more so in rapid, so that CI is the spread between draws, not fresh samples):"
        )
        report.table(base)
        # Where your subsampled AUC sits in the strangers' distribution.
        pct_rows = []
        for tc in TIME_CLASSES:
            if tc not in pooled_imp:
                continue
            bv = pooled_imp[tc][3]
            you_v = bv.loc[bv["kind"] == "you (subsample) vs rest", "auc"].to_numpy(dtype=float)
            str_v = bv.loc[bv["kind"] == "stranger vs rest", "auc"].to_numpy(dtype=float)
            if len(you_v) and len(str_v):
                pct_est, pct_lo, pct_hi = percentile_with_ci(you_v, str_v, seed=SEED + TIME_CLASSES.index(tc))
                pct_rows.append({"time_class": tc, "you_mean": you_v.mean(), "str_mean": str_v.mean(), "percentile": pct_est, "pct_lo": pct_lo, "pct_hi": pct_hi, "n_strangers": len(str_v)})
        pct = pd.DataFrame(pct_rows)
        if len(pct):
            below_all = bool((pct["you_mean"] < pct["str_mean"]).all())
            report.text(
                f"Your {CAP_PER_CLASS}-game subsamples are picked out of the pool with mean AUC "
                + ", ".join(f"{r.time_class} {r.you_mean:.3f} (stranger mean {r.str_mean:.3f}; {r.percentile:.0%} [{r.pct_lo:.0%}, {r.pct_hi:.0%}] of the {r.n_strangers} strangers are *less* distinctive than you)" for r in pct.itertuples())
                + " (percentile CIs bootstrap strangers and your draws). "
                "This and the per-pair test above answer different questions, which is why they can disagree: the per-pair test matches you to one stranger's rating bands and asks whether that one person and you can be told apart, "
                "and almost any two people can; 'vs the rest' asks how unusual a player is against the whole pool, where common ingredients (the most popular first moves and replies, the usual pacing) are "
                "harder to pick out than rare ones even when they are held with unusual consistency. "
                + ("A fingerprint made of common ingredients, held consistently, is what puts you below the stranger mean in every class here." if below_all else "")
            )
        report.heading("Which engine-free features carry it (pooled fit)", level=4)
        side_rows = []
        side_tables = {}
        mixes = {}
        for tc in TIME_CLASSES:
            if tc not in pooled_imp:
                continue
            (coef_table, perm_table), a, b, _ = pooled_imp[tc]
            report.text(f"**{tc}** — logistic coefficients (standardised):")
            report.table(coef_table, floatfmt=".2f")
            report.text(f"**{tc}** — boosting permutation importance (AUC drop):")
            report.table(perm_table, floatfmt=".3f")
            top = list(dict.fromkeys([*perm_table["feature"].head(6), *coef_table["feature"].head(6)]))
            rows = []
            for col in top:
                va, vb = a[col].to_numpy(dtype=float), b[col].to_numpy(dtype=float)
                pa = mean_with_cluster_ci(va[~np.isnan(va)], a["game_uuid"].to_numpy()[~np.isnan(va)])
                pb = mean_with_cluster_ci(vb[~np.isnan(vb)], b["player"].to_numpy()[~np.isnan(vb)])
                rows.append({"feature": col, "you": fmt(*pa), "strangers (same bands)": fmt(*pb), "you_mean": pa[0], "str_mean": pb[0]})
            side_tables[tc] = pd.DataFrame(rows)
            report.text(f"**{tc}** — per-game means, you vs the band-matched strangers (CIs: your games / strangers as clusters):")
            report.table(side_tables[tc][["feature", "you", "strangers (same bands)"]])
            mix_a, inc_a = tc_mix(a)
            mix_b, inc_b = tc_mix(b)
            mixes[tc] = (mix_a, mix_b, abs(inc_a - inc_b) >= 0.1)
            clock_tells = [c for c in top if c in CLOCK]
            tells_txt = " (" + ", ".join(f"`{c}`" for c in clock_tells) + ")" if clock_tells else ""
            if abs(inc_a - inc_b) < 0.1:
                verdict = f"the mix is nearly the same on both sides, so the clock tells here{tells_txt} are pacing, not time control; the check matched on time control is below."
            else:
                verdict = f"time spent per move includes the increment and the clock features are fractions of the base time, so the clock tells here{tells_txt} are partly the time-control mix, not only pacing; the check matched on time control is below."
            report.text(f"Time-control mix in this matched sample: you {mix_a} ({inc_a:.0%} of games with an increment); strangers {mix_b} ({inc_b:.0%} with an increment); {verdict}")
            side_rows.append((tc, perm_table))
        fig, axes = plt.subplots(1, 3, figsize=(12, 4.5))
        for ax, (tc, perm_table) in zip(axes, side_rows):
            pt = perm_table.head(10).iloc[::-1]
            ax.barh(pt["feature"], pt["auc_drop"], xerr=pt["sd"], color=SIDE_COLOURS["player"])
            ax.set_title(tc)
            ax.set_xlabel("AUC drop when shuffled")
        fig.tight_layout()
        report.figure(fig, "step2_importance", "Step 2 pooled fit (you vs all band-matched strangers): boosting permutation importance of the ten most important engine-free features.")
        plt.close(fig)
        # Matched on time control too.
        tcm = pd.DataFrame(tcm_rows)
        if len(tcm):
            report.heading("Matched on rating band and time control", level=4)
            report.text(
                "The same pooled design, but each of your games is matched to a stranger's game in the same 100-point band *and* the same time control (3|2 with 3|2, 1|0 with 1|0), "
                "so increments cannot explain a clock tell. Where the strangers mostly play other controls than you (blitz: 3|0 and 5|0 against your 3|2) fewer games survive; bullet and rapid lose almost none."
            )
            tcm["AUC"] = ci_column(tcm, "auc", "auc_lo", "auc_hi")
            tcm["games (you / strangers)"] = tcm.apply(lambda r: f"{int(r['you']):,} / {int(r['strangers']):,} ({int(r['n_strangers'])} people)", axis=1)
            report.table(tcm[["time_class", "features", "model", "AUC", "games (you / strangers)"]])
            tcm_side_rows = []
            for tc, (a2, b2) in tcm_samples.items():
                clock_tells = [c for c in side_tables.get(tc, pd.DataFrame({"feature": []}))["feature"] if c in CLOCK]
                for col in clock_tells:
                    va, vb = a2[col].to_numpy(dtype=float), b2[col].to_numpy(dtype=float)
                    pa = mean_with_cluster_ci(va[~np.isnan(va)], a2["game_uuid"].to_numpy()[~np.isnan(va)])
                    pb = mean_with_cluster_ci(vb[~np.isnan(vb)], b2["player"].to_numpy()[~np.isnan(vb)])
                    tcm_side_rows.append({"time_class": tc, "feature": col, "you": fmt(*pa), "strangers (same band and time control)": fmt(*pb), "you_mean": pa[0], "str_mean": pb[0]})
            tcm_sides = pd.DataFrame(tcm_side_rows)
            if len(tcm_sides):
                report.text("The clock tells from the band-matched tables, re-measured in this sample (CIs: your games / strangers as clusters):")
                report.table(tcm_sides[["time_class", "feature", "you", "strangers (same band and time control)"]])
            tcm_all = tcm[(tcm["features"] == sets2[0]) & (tcm["model"] == "boosting")].set_index("time_class")
            tcm_nc = tcm[(tcm["features"] == "no clock") & (tcm["model"] == "boosting")].set_index("time_class")
            pooled_b = pooled[(pooled["features"] == sets2[0]) & (pooled["model"] == "boosting")].set_index("time_class")
            report.text(
                "Boosting AUC matched on time control as well: "
                + ", ".join(f"{tc} {fmt(*tcm_all.loc[tc, ['auc', 'auc_lo', 'auc_hi']])} (band-only {pooled_b.loc[tc, 'auc']:.3f}; without clock features {tcm_nc.loc[tc, 'auc']:.3f})" for tc in TIME_CLASSES if tc in tcm_all.index and tc in pooled_b.index)
                + "."
            )
        else:
            tcm_sides = pd.DataFrame()
    STEP2_SUMMARY = {
        "dist": dist,
        "pooled": pooled,
        "pct": pct if len(pooled) else pd.DataFrame(),
        "sides": side_tables if len(pooled) else {},
        "mixes": mixes if len(pooled) else {},
        "tcm": tcm if len(pooled) else pd.DataFrame(),
        "tcm_sides": tcm_sides if len(pooled) else pd.DataFrame(),
        "n_strangers": strangers["player"].nunique(),
    }
    print(f"step 2 done in {time.time() - t0:.0f} s", flush=True)


STEP2_SUMMARY: dict = {}


def summary_paragraph(table1: pd.DataFrame, held: pd.DataFrame, s2: dict) -> str:
    boost_all = table1[(table1["features"] == "all") & (table1["model"] == "boosting")].set_index("time_class")
    drops = {}
    for group, set_name in (("clock", "no clock"), ("opening", "no opening")):
        without = table1[(table1["features"] == set_name) & (table1["model"] == "boosting")].set_index("time_class")
        drops[group] = [boost_all.loc[tc, "auc"] - without.loc[tc, "auc"] for tc in TIME_CLASSES]
    biggest = max(drops, key=lambda g: sum(drops[g]))
    smallest = min(drops, key=lambda g: sum(drops[g]))
    txt = (
        "Against the people you actually played, your games are recognisable: boosting AUC "
        + " / ".join(f"{boost_all.loc[tc, 'auc']:.2f}" for tc in TIME_CLASSES)
        + " (bullet / blitz / rapid) and "
        + " / ".join(f"{held.loc[tc, 'boosting_auc']:.2f}" for tc in TIME_CLASSES if tc in held.index)
        + f" when the test year was never seen; the {biggest} group is the biggest single tell (dropping it costs "
        + " / ".join(f"{d:.2f}" for d in drops[biggest])
        + f" AUC, dropping the {smallest} group "
        + " / ".join(f"{d:.2f}" for d in drops[smallest])
        + "), while the engine error profile alone is near chance: at this level your mistakes look like everyone else's, your habits do not."
    )
    if s2 and len(s2.get("dist", [])):
        dist = s2["dist"]
        full = dist[dist["features"] == list(FEATURE_SETS_STEP2)[0]].set_index("time_class")
        nn = dist[dist["features"] == "no clock, no opening"].set_index("time_class")
        parts, excluded = [], []
        for tc in TIME_CLASSES:
            if tc in full.index and full.loc[tc, "you_n"] > 0:
                parts.append(f"{tc} {full.loc[tc, 'you_mean']:.2f} vs {full.loc[tc, 'strangers_mean']:.2f} (difference {fmt(full.loc[tc, 'diff'], full.loc[tc, 'diff_lo'], full.loc[tc, 'diff_hi'], '.2f')})")
                if pd.notna(full.loc[tc, "diff_lo"]) and (full.loc[tc, "diff_lo"] > 0 or full.loc[tc, "diff_hi"] < 0):
                    excluded.append(f"{tc} ({'more' if full.loc[tc, 'diff'] > 0 else 'less'} distinctive)")
        verdict = (
            "In no time class does the CI of the paired difference exclude zero: you are exactly as recognisable as an average stranger of your rating, no more and no less."
            if not excluded
            else "The paired difference's CI excludes zero in " + ", ".join(excluded) + "; elsewhere you are as recognisable as an average stranger of your rating."
        )
        txt += (
            f" The honest test against {s2['n_strangers']} same-rated strangers' own games says how special that is: a same-sized classifier separates you from a stranger with mean AUC "
            + "; ".join(parts)
            + ". "
            + verdict
            + " Everybody has a fingerprint; without clock and opening features the per-pair AUCs fall to "
            + " / ".join(f"{nn.loc[tc, 'you_mean']:.2f}" for tc in TIME_CLASSES if tc in nn.index)
            + " for you and "
            + " / ".join(f"{nn.loc[tc, 'strangers_mean']:.2f}" for tc in TIME_CLASSES if tc in nn.index)
            + " between strangers: opening choice and clock pacing carry most of anyone's fingerprint at this level, move-kind habits (castling, captures, piece use) the rest, and the engine sees nothing."
        )
        pct = s2.get("pct", pd.DataFrame())
        if len(pct):
            below_all = bool((pct["you_mean"] < pct["str_mean"]).all())
            txt += (
                " The pooled 'vs the rest' comparison at equal sample size asks a different question, how unusual you are against the whole pool rather than against one person, and there "
                + ", ".join(f"{r.percentile:.0%} [{r.pct_lo:.0%}, {r.pct_hi:.0%}] of strangers are less distinctive than you in {r.time_class}" for r in pct.itertuples())
                + (": your fingerprint is made of common ingredients held consistently, so one stranger can be told from you but you are not an outlier in the pool." if below_all else ".")
            )
        sides = s2.get("sides", {})
        if "bullet" in sides and len(sides["bullet"]):
            tells = sides["bullet"].head(4)
            txt += (
                " Your own tells (bullet, pooled fit): "
                + "; ".join(f"`{r.feature}` {r.you_mean:.3f} vs {r.str_mean:.3f} for strangers" for r in tells.itertuples())
                + ". These are habits a regular opponent can learn (one first move as White, one reply to e4 and to d4, an early short castle); they are not a leak in your results, since Q8 found the repertoire neither gains nor loses against expectation."
            )
        tcm = s2.get("tcm", pd.DataFrame())
        pooled = s2.get("pooled", pd.DataFrame())
        if len(tcm) and len(pooled):
            first = list(FEATURE_SETS_STEP2)[0]
            t_all = tcm[(tcm["features"] == first) & (tcm["model"] == "boosting")].set_index("time_class")
            t_nc = tcm[(tcm["features"] == "no clock") & (tcm["model"] == "boosting")].set_index("time_class")
            p_all = pooled[(pooled["features"] == first) & (pooled["model"] == "boosting")].set_index("time_class")
            p_nc = pooled[(pooled["features"] == "no clock") & (pooled["model"] == "boosting")].set_index("time_class")
            tcs = [tc for tc in TIME_CLASSES if tc in t_all.index and tc in p_all.index]
            drop_tcm = [t_all.loc[tc, "auc"] - t_nc.loc[tc, "auc"] for tc in tcs]
            drop_band = [p_all.loc[tc, "auc"] - p_nc.loc[tc, "auc"] for tc in tcs]
            confounded = [tc for tc in tcs if s2.get("mixes", {}).get(tc, (None, None, False))[2]]
            txt += (
                " The clock tells are partly the time-control mix"
                + (f" in {' and '.join(confounded)} (you play mostly with an increment there, the strangers mostly without, and time per move includes the increment)" if confounded else "")
                + ("; in " + " and ".join(tc for tc in tcs if tc not in confounded) + " both sides play similar controls, so the clock tells there are pacing" if confounded and len(confounded) < len(tcs) else "")
                + ". Matched on time control as well, the pooled boosting AUC is "
                + " / ".join(f"{t_all.loc[tc, 'auc']:.2f}" for tc in tcs)
                + " and dropping the clock group costs "
                + " / ".join(f"{d:+.2f}" for d in drop_tcm)
                + " AUC against "
                + " / ".join(f"{d:+.2f}" for d in drop_band)
                + " in the band-only pooled fit"
                + (", so the clock group is a smaller tell than the band-only tables suggest." if sum(drop_tcm) < sum(drop_band) else ", so the clock tell survives matching on time control.")
            )
    return txt


if __name__ == "__main__":
    sys.exit(main())
