"""Stockfish evaluation of every position, stored once per position.

`evals.parquet` has one row per distinct position (FEN without move
counters). Runs are resumable: positions already in the table are skipped,
and the table is rewritten at checkpoints. One engine per worker process,
one thread each, hash cleared before every position, so results depend only
on the engine build and the node budget, not on scheduling.
"""

import multiprocessing
import os
import queue
import signal
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

def _log(message: str) -> None:
    """Progress output that appears immediately even when redirected to a file."""
    print(message, flush=True)


import chess
import chess.engine
import pandas as pd

EVALS_FILE = "evals.parquet"
DEFAULT_NODES = 1_000_000
MULTIPV = 2
EVAL_COLUMNS = [
    "position", "engine", "nodes_limit", "depth", "seldepth", "nodes",
    "score_cp", "mate_in", "wdl_win", "wdl_draw", "wdl_loss",
    "best_move", "second_move", "second_score_cp", "second_mate_in", "terminal",
]  # fmt: skip

def position_key(fen: str) -> str:
    """FEN without the halfmove and fullmove counters."""
    return " ".join(fen.split(" ")[:4])


def positions_to_evaluate(moves_enriched: pd.DataFrame) -> list[str]:
    """Every distinct position faced or reached in the enriched moves, sorted."""
    keys = pd.concat([moves_enriched["fen_before"], moves_enriched["fen_after"]]).map(position_key)
    return sorted(keys.unique())


def _white_pov(score: chess.engine.PovScore) -> tuple[float | None, int | None]:
    white = score.white()
    return white.score(), white.mate()


def _open_engine(command: str) -> chess.engine.SimpleEngine:
    engine = chess.engine.SimpleEngine.popen_uci(command)
    engine.configure({"Threads": 1, "Hash": 16, "UCI_ShowWDL": True})
    return engine


def evaluate(position: str, nodes: int, engine: chess.engine.SimpleEngine) -> dict:
    """One row of `evals` for a position."""
    engine_name = engine.id.get("name", "unknown engine")
    board = chess.Board(position + " 0 1")
    row = dict.fromkeys(EVAL_COLUMNS)
    row.update(position=position, engine=engine_name, nodes_limit=nodes, terminal=False)

    if board.is_game_over():
        # Nothing to search. Checkmate: mate_in is 0 and the WDL columns say who won
        # (the side to move has been mated). Anything else is a draw.
        row.update(depth=0, seldepth=0, nodes=0, terminal=True)
        if board.is_checkmate():
            white_mated = board.turn == chess.WHITE
            row.update(mate_in=0, wdl_win=0 if white_mated else 1000, wdl_draw=0, wdl_loss=1000 if white_mated else 0)
        else:
            row.update(score_cp=0.0, wdl_win=0, wdl_draw=1000, wdl_loss=0)
        return row

    # `game=object()` makes python-chess send ucinewgame first, clearing the hash.
    infos = engine.analyse(board, chess.engine.Limit(nodes=nodes), multipv=MULTIPV, game=object())
    best = infos[0]
    row["depth"], row["seldepth"], row["nodes"] = best.get("depth"), best.get("seldepth"), best.get("nodes")
    row["score_cp"], row["mate_in"] = _white_pov(best["score"])
    if "wdl" in best:
        wdl = best["wdl"].white()
        row.update(wdl_win=wdl.wins, wdl_draw=wdl.draws, wdl_loss=wdl.losses)
    if best.get("pv"):
        row["best_move"] = best["pv"][0].uci()
    if len(infos) > 1:
        second = infos[1]
        row["second_score_cp"], row["second_mate_in"] = _white_pov(second["score"])
        if second.get("pv"):
            row["second_move"] = second["pv"][0].uci()
    return row


def _worker(command: str, nodes: int, tasks: multiprocessing.Queue, results: multiprocessing.Queue) -> None:
    """Evaluate chunks from `tasks` until a None arrives. Closes the engine itself:
    python-chess's engine thread is not a daemon, and a multiprocessing child waits
    for its threads before any exit hook runs, so nothing else can stop it."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    # Being terminated (e.g. after Ctrl-C in the parent) must still quit the
    # engine, or the Stockfish process is orphaned.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine = _open_engine(command)
    try:
        while (chunk := tasks.get()) is not None:
            results.put([evaluate(p, nodes, engine) for p in chunk])
    finally:
        engine.quit()
        engine.close()


def _parallel_results(chunks: list[list[str]], command: str, nodes: int, workers: int) -> Iterator[list[dict]]:
    """Yield each chunk's rows as workers finish them (any order)."""
    ctx = multiprocessing.get_context("spawn")  # Forking a process with pandas/pyarrow threads can deadlock.
    tasks: multiprocessing.Queue = ctx.Queue()
    results: multiprocessing.Queue = ctx.Queue()
    processes = [ctx.Process(target=_worker, args=(command, nodes, tasks, results), daemon=True) for _ in range(min(workers, len(chunks)))]
    for process in processes:
        process.start()
    for chunk in chunks:
        tasks.put(chunk)
    for _ in processes:
        tasks.put(None)
    try:
        for _ in chunks:
            while True:
                try:
                    yield results.get(timeout=5)
                    break
                except queue.Empty:
                    if not any(p.is_alive() for p in processes):
                        raise RuntimeError("all engine workers exited before finishing") from None
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join()


def load_evals(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=EVAL_COLUMNS)


def save_evals(evals: pd.DataFrame, path: Path) -> None:
    evals = evals.sort_values("position", kind="stable").reset_index(drop=True)
    evals = evals.astype({"depth": "Int64", "seldepth": "Int64", "nodes": "Int64", "mate_in": "Int64", "second_mate_in": "Int64", "nodes_limit": "Int64", "wdl_win": "Int64", "wdl_draw": "Int64", "wdl_loss": "Int64", "score_cp": "float64", "second_score_cp": "float64", "terminal": "bool"})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    evals.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, path)


def run(
    positions: Iterable[str],
    path: Path,
    engine_command: str,
    nodes: int = DEFAULT_NODES,
    workers: int | None = None,
    checkpoint: int = 2000,
    chunk_size: int = 20,
    log: Callable[[str], None] = _log,
    evaluate_chunk: Callable[[list[str], int], list[dict]] | None = None,
) -> pd.DataFrame:
    """Evaluate positions not already in `path` (at this node budget), saving every `checkpoint` positions."""
    existing = load_evals(path)
    done = set(existing.loc[existing["nodes_limit"] == nodes, "position"]) if len(existing) else set()
    todo = [p for p in positions if p not in done]
    log(f"{len(todo)} positions to evaluate ({len(done)} already done), {nodes} nodes each")
    if not todo:
        return existing

    workers = workers or os.cpu_count() or 1
    chunks = [todo[i : i + chunk_size] for i in range(0, len(todo), chunk_size)]
    new_rows: list[dict] = []
    since_checkpoint = 0
    started = time.monotonic()

    def flush() -> pd.DataFrame:
        combined = pd.concat([existing, pd.DataFrame(new_rows, columns=EVAL_COLUMNS)], ignore_index=True)
        combined = combined.drop_duplicates("position", keep="last")
        save_evals(combined, path)
        return combined

    if evaluate_chunk is not None:
        results: Iterable[list[dict]] = (evaluate_chunk(chunk, nodes) for chunk in chunks)
    else:
        results = _parallel_results(chunks, engine_command, nodes, workers)
    try:
        for rows in results:
            new_rows.extend(rows)
            since_checkpoint += len(rows)
            if since_checkpoint >= checkpoint:
                flush()
                since_checkpoint = 0
                elapsed = time.monotonic() - started
                rate = len(new_rows) / elapsed
                log(f"  {len(new_rows)}/{len(todo)} done, {rate:.1f} positions/s, ~{(len(todo) - len(new_rows)) / rate / 60:.0f} min left")
    except BaseException:
        if new_rows:
            flush()
        raise
    finally:
        if isinstance(results, Iterator):
            results.close()  # Stops the workers (and their engines) if we're leaving early.
    return flush()


def with_evals(moves_enriched: pd.DataFrame, evals: pd.DataFrame) -> pd.DataFrame:
    """Moves joined to the evaluation of the position before and after each move (White's view)."""
    before = evals.set_index("position")[["score_cp", "mate_in", "wdl_win", "wdl_draw", "wdl_loss", "best_move", "second_score_cp", "second_mate_in"]]
    after = evals.set_index("position")[["score_cp", "mate_in", "wdl_win", "wdl_draw", "wdl_loss"]]
    out = moves_enriched.copy()
    out["position_before"] = out["fen_before"].map(position_key)
    out["position_after"] = out["fen_after"].map(position_key)
    out = out.join(before.add_suffix("_before"), on="position_before").join(after.add_suffix("_after"), on="position_after")
    out["is_best_move"] = out["uci"] == out["best_move_before"]
    return out
