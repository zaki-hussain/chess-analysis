# Analysis plan

What we want to learn from zakihussa1n's games, and where each answer
lives. Data as of 2026-11-01: 6,081 games (6,047 standard rules), 365,561
analysed moves, Aug 2021 to Oct 2026.

**Results live in [`presentation/README.md`](presentation/README.md)**: one
section per question, a chart per sub-question, every number checked against
the CSV it comes from. This file holds goals, status and the decisions that
matter for future work; it deliberately repeats no result numbers, so they
cannot drift from the data.

Status key: **done** · **next** · **planned** · **exploratory**.

## Part A: Foundations (all done)

| | What | Where |
|---|---|---|
| F1 | Engine-free enrichment: position per move, phase, move kind, time spent, both clocks, sessions, local time, rating-reliability proxies | `enrich.py` → `moves_enriched`, `my_games` |
| F2 | Stockfish 17.1 on every distinct position, 1M nodes, MultiPV 2 (316,076 positions, run on EC2) | `engine.py` → `evals` |
| F3 | Move quality from the mover's view: cp loss, best/second move, criticality, missed/allowed mates, labels | `analysed.py` → `moves_analysed` |
| F4 | Opening names and book depth (Lichess list, CC0) | `openings.py` → `my_games.opening_*`, `moves_enriched.in_book` |
| F5 | Syzygy 3-4-5 tablebases: exact result for the 9,940 moves in ≤5-piece positions | `tablebase.py` → `tb_*` columns |
| F6 | Win-probability model on our own games: expected points per move, P(win/draw/loss), criticality in expected points; checked overall and per time control | `calibrate.py` → `expected_points`, `presentation/data/00_calibration/` (appendix A) |
| F7 | Analysis toolkit: data loading with time-control groups, clustered bootstraps, you-vs-opponents comparisons, Elo-equivalents, CSV/SVG output, one chart style | `toolkit.py`, `charts.py`, `clocks.py`, `sessions.py`; conventions in `analysis/README.md` |

Notes:
- F6 is the currency for everything below: **`ep_loss`** (calibrated
  expected points lost by a move) is the main quality measure; `cp_loss` is
  the familiar secondary one. Post-game ratings leak the result; the model
  uses pre-game ratings (the opponent's estimated from your rating change).
- Every question is split by **exact time control** (`data.tc_groups`):
  bullet 1+0, 1+1, 2+1; blitz 3+0, 3+2, 5+0, 5+2, 5+5; rapid 10+0. A control
  needs 50 games to be reported (`MIN_GROUP_GAMES`); 0.5+0 (19 games) and
  30+0 (1 game) are listed, not analysed. Rating pools are per time class
  (bullet/blitz/rapid), so ratings and Elo expectations come from the class.
- Low-clock measures come in seconds left and in **clock buffer** (clock
  minus one increment), so 1+0 and 1+1 are comparable.
- Phase is the material heuristic; `book_ply` (median 4) is the alternative
  end-of-opening marker. Daily games (18) have no clocks; variants (34) are
  excluded.
- The F6 model is fitted on all standard games, including the thin controls
  and daily; its per-control check (appendix A) shows it is calibrated in
  every reported control.

## Part B: Questions

Every question is **done**: script `analysis/<nn>_<slug>.py` → tables in
`presentation/data/<nn>_<slug>/` (described in `_index.csv`) and charts in
`presentation/figures/<nn>_<slug>/` → section in `presentation/README.md`.
Runtimes are on this 4-core container, run alone.

### 1. Where points are won and lost — done

- Goal: decompose every game into expected points you gave away and points
  they gave away; who made the last decisive mistake; how often you convert
  an advantage and how often you save a lost position.
- Script `analysis/01_decomposition.py` (~35 s). Also writes
  `data/zakihussa1n/games_decomposed.parquet` (one row per game), which Q2,
  Q5, Q6, Q7 and Q8 read: keep it byte-identical when changing the script.
- Decisions: decisive blunder judged on the loser's own moves (lost below
  10% after their last move, slide starting from their last playable
  position ≥ 20%); conversion from ≥ 80%, save from ≤ 20% (other thresholds
  in `5c_threshold_sensitivity.csv`); termination cells pooled by time class
  as well, as most per-control cells are thin.

### 2. Rating trajectory, and performance vs stronger/weaker opponents (Q11) — done

- Goal: your blitz went from 252 to 1,050 and bullet from 160 to 1,300.
  Which things improved, and against the field or with it? Do you over- or
  under-perform against stronger/weaker opponents (Q11, folded in here), and
  how do upsets happen?
- Script `analysis/02_trajectory.py` (~20 s).
- Decisions: rating terciles per pool; score vs rating gap uses the
  estimated opponent pre-game rating (`opponent_rating_pre`); the main-time-
  control sensitivity was dropped because every control is now reported on
  its own. Outside 1+0, 3+2 and 10+0 most tercile, band and gap cells are
  thin, so score vs gap is also pooled by class.

### 3. Time pressure — done

- Goal: error vs clock left (seconds, buffer beyond the increment, share
  of base), overall and in endgames; whether you degrade more or less than
  opponents in the same games; what rating your low-clock play resembles.
- Script `analysis/03_time_pressure.py` (~1.5 min), machinery in
  `src/chess_analysis/clocks.py` (shared with Q4).
- Decisions: the adjusted gap holds stakes, phase, material and the
  opponent's clock fixed; blitz and rapid have few low-clock moves, so
  pooled "all blitz" rows (with a control fixed effect) are added. The
  rating equivalent uses centipawns, since expected points are flat in
  rating by construction.

### 4. Clock allocation and losses on time — done

- Goal: where extra seconds buy accuracy and where they don't; whether you
  spend time in the right places; what losses on time cost, and whether a
  flag can be seen coming.
- Script `analysis/04_clock_allocation.py` (~2.5 min, including a
  deterministic refit of the F6 model to value flags with level clocks).
- Decisions: seconds-spent buckets are multiples of each control's per-move
  budget, (base + 40 × increment) / 40; `ep_loss` rises mechanically with
  thinking time through the model's clock term, so "do longer thinks pay"
  is charted in centipawns.

### 5. Sessions: fatigue, warm-up, tilt — done

- Goal: does play degrade over a session (fatigue), start poorly (warm-up),
  or collapse after losses (tilt)? Do breaks matter?
- Script `analysis/05_sessions.py` (~40 s), machinery in
  `src/chess_analysis/sessions.py` (shared with Q6).
- Decisions: the stopping rule (you stop more after losses) bends every
  curve by game number, so fatigue is read from game k minus game 1 of the
  same session and from within-session regressions. Game-level results use
  each game's own control; session-level descriptions use the session's
  main control; CIs resample whole sessions. Session gap from `config.toml`.

### 6. Time of day and day of week — done

- Goal: when do you play best, after controlling for session position,
  rating, year and time control?
- Script `analysis/06_time_of_day.py` (~40 s).
- Decisions: six hour bands (hours 1-6 merged with 0: too few games each);
  a session-level shuffle test guards against the many contrasts a band ×
  control × weekday table produces; any future hour claim should pass it and
  repeat across 2021-23 vs 2024-26. Hour × weekday is pooled by time class.

### 7. Expected value of not resigning — done

- Goal: how many points your resignations gave away, and when resigning
  was right.
- Script `analysis/07_resignation.py` (~35 s).
- Decisions: three valuations kept (played-out positions, the model, a
  lower bound from all positions); the played-out set is selected (people
  who played on may have had reasons), so it is an upper-ish bound. Eval ×
  clock × side cells are thin outside 1+0 and 3+2, so the "when is it safe"
  map is pooled by time class.

### 8. Opening repertoire — done

- Goal: which openings you score and play well in, by colour; whether
  staying in book longer helps; the lines and moves that cost you most.
- Script `analysis/08_openings.py` (~40 s).
- Decisions: repertoire and family scores are pooled over all controls
  (opening × colour × control is thin; openings are not control-specific),
  with per-class scores as a secondary table; families are compared with
  your own average for that colour, because the Elo formula has no colour
  term. Names are by position, so transpositions can mislabel (the
  "Zukertort" as White is 1.e4 d5 2.Nf3).

### 9. Blunder anatomy — done

- Goal: what kinds of mistakes you make and miss: hanging pieces, missed
  and allowed mates, missed captures, quiet refutations, by piece, phase and
  clock; which types changed over time.
- Script `analysis/09_blunder_anatomy.py` (~2.5 min; python-chess replay of
  the blunder positions, no engine).
- Decisions: blunder = `cp_loss` ≥ 300 (the expected-points definition is
  alongside); the clock effect is estimated with phase, evaluation and move
  number held fixed; year trends stay by time class (year × control is
  thin). The stored `missed_mate` flag is correct (3,339).

### 10. Endgame technique — done

- Goal: do you convert theoretically won endgames and hold drawn ones?
  Which endgame types go wrong, and how much is the clock?
- Script `analysis/10_endgames.py` (~30 s).
- Decisions: tablebase data are small (9,898 moves in 469 games, ~160
  errors), so every table also has pools by time class and by increment
  within a class; small counts carry Wilson/Newcombe intervals as well as
  the bootstrap.

### 11. Performance vs opponent rating and upsets — done (in Q2)

### 12. Style fingerprint — done

- Goal: can a classifier pick your games out from your opponents' and from
  same-rated strangers', and on which features?
- Script `analysis/12_fingerprint.py` (~9 min single-threaded, from the
  committed `data/opponents/fingerprint_features.parquet`;
  `--rebuild-features` needs the 200 opponents' raw archives, which are
  gitignored: `scripts/download_opponents.py`, ~1 h, then ~17 min).
- Decisions: fits per control where you and the strangers both have
  enough games, otherwise per class matched on exact control; matching on
  control matters because time per move includes the increment. A
  clock-buffer feature would need `--rebuild-features`.

### 13. Findings as data — replaced

The hand-copied findings table (`analysis/findings/*.json`,
`13_findings.py`, `findings.parquet`) is retired: every script now writes
its own results as CSV, so nothing is copied by hand, and
`analysis/check_presentation.py` checks the one hand-written page against
those CSVs. The old reports and findings are recoverable from commit
`73c6c53` (see "Recovering the old long-form reports" in
`presentation/README.md`).

## Part C: How the work is organised

Dependency graph (→ = needs):

```
toolkit/charts ─┬→ Q1 decomposition (writes games_decomposed) ─┬→ Q2 trajectory (+Q11)
                │                                              ├→ Q5 sessions → Q6 time of day
                │                                              ├→ Q7 resignation
                │                                              └→ Q8 openings
                ├→ Q3 time pressure → Q4 clock allocation   (shared: clocks.py)
                ├→ Q9 blunder anatomy
                ├→ Q10 endgames
                └→ Q12 fingerprint (needs the committed opponent features)
presentation/README.md ← every question's CSVs and charts
```

To change a question: edit its script, rerun it, update its section of
`presentation/README.md` where numbers moved, rerun
`analysis/check_presentation.py` until it reports 0 problems, and update
the question's notes here. Changing the toolkit, the charts or the F6 model
means rerunning every script (about 18 minutes in total, Q12 being half of
it) and checking the page again.

History: the questions were first answered in three parallel waves split by
bullet/blitz/rapid with long Markdown reports, then rewritten in two
parallel workflows (implement, independent review, fix) to the exact
time-control split and the CSV/SVG/one-page format.

## Open questions

- Daily games: `%clk` looks like time taken for the move, not time left;
  not yet confirmed, so daily games stay out of every question.
- Rapid has a single control (10+0); 30+0 has one game. Nothing can be said
  about longer rapid.
