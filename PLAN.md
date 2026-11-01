# Analysis plan

What we want to learn from the games, in the order we'll do it. Numbers are
for zakihussa1n's data as of 2026-11-01: 6,081 games (6,047 standard
rules), 365,561 analysed moves, 5,498 distinct opponents matched within
±27 rating points, 57% bullet / 33% blitz / 10% rapid.

Status key: **done** · **next** · **planned** · **exploratory**.

Each finished question gets a script under `analysis/` and a written result
under `reports/`, and its status and notes here get updated.

## Part A: Foundations (all done)

| | What | Where |
|---|---|---|
| F1 | Engine-free enrichment: position per move, phase, move kind, time spent, both clocks, sessions, local time, rating-reliability proxies | `enrich.py` → `moves_enriched`, `my_games` |
| F2 | Stockfish 17.1 on every distinct position, 1M nodes, MultiPV 2 (316,076 positions, run on EC2) | `engine.py` → `evals` |
| F3 | Move quality from the mover's view: cp loss, best/second move, criticality, missed/allowed mates, labels | `analysed.py` → `moves_analysed` |
| F4 | Opening names and book depth (Lichess list, CC0) | `openings.py` → `my_games.opening_*`, `moves_enriched.in_book` |
| F5 | Syzygy 3-4-5 tablebases: exact result for the 9,940 moves in ≤5-piece positions | `tablebase.py` → `tb_*` columns |
| F6 | Win-probability calibration on our own games: expected points per move, P(win/draw/loss), criticality in expected points | `calibrate.py` → `expected_points`, `reports/calibration.md` |

Notes:
- F6 is the currency for everything below: **`ep_loss`** (calibrated
  expected points lost by a move) is the main quality measure; `cp_loss` is
  the familiar secondary one. Brier 0.179 vs 0.216 (Lichess curve) and
  0.267 (Stockfish WDL). Post-game ratings leak the result; the model uses
  pre-game ratings (opponent's estimated from your rating change).
- Phase is still the material heuristic; `book_ply` (median 4) is available
  as an alternative end-of-opening marker.
- Daily games (18) have no clock data; variants (34 games) are excluded.

### F7. Analysis toolkit — done

`chess_analysis/toolkit.py` plus `analysis/README.md` and `_template.py`:
`load_data()` (moves with the per-game view joined; standard live games),
game-clustered bootstrap CIs, `compare_sides()` (you vs opponents in the
same games, with a paired bootstrap for the difference), `binned()`,
`mean_with_ci()`, `per_game()`, Elo expectation, rating bands, and
`Report` for markdown + figures. Conventions: bullet/blitz/rapid reported
separately, every player number paired with the opponents', CIs on
everything, `ep_loss` first, no post-game ratings as predictors.

Notes:
- Baseline from the template run: mean `ep_loss` per move 0.029–0.032 for
  both sides in every time class; you lose marginally less than opponents
  in bullet (−0.0003 [−0.0006, −0.0000]) and rapid (−0.0010 [−0.0018,
  −0.0002]). Differences will be in the third decimal: CIs matter.

## Part B: Questions, in execution order

Each entry: **Goal · Inputs · Method · Outputs · Depends on · Caveats.**

### 1. Where points are won and lost: your mistakes vs your opponents' — planned

- Goal: decompose every game into expected points you gave away and
  points they gave away; who made the last decisive mistake; how often you
  convert an advantage and how often you swindle a lost position.
- Inputs: `ep_loss`, `cp_loss`, `label`, `p_win_before`, result.
- Method: per game, sum `ep_loss` by side; find the last move after which
  the loser's `p_win` fell below 10% ("decisive mistake") and who made it;
  conversion rate = P(win | reached p_win ≥ 80%), swindle rate =
  P(not loss | reached p_win ≤ 20%); error-size distributions you vs them
  by phase.
- Outputs: per-game decomposition table (`analysis` output, committed as
  `games_decomposed.parquet`), report with the rates above by time class.
- Depends on: F3, F6.
- Caveats: engine-perfect baseline; at this level both sides lose ~0.03
  expected points per move, so the story is in the distribution's tail.

### 2. Rating trajectory: what changed as you improved — planned (new)

- Goal: your blitz went from 252 to 1,050 and bullet from 160 to 1,300.
  Which things improved: fewer blunders, better conversion, faster play,
  stronger openings, endgames? What hasn't moved?
- Inputs: Q1's per-game table, `my_rating_before`, year, phase, labels.
- Method: metrics from Q1 by rating band (100-point bins) and by year;
  compare your error rates in each band with opponents' in the same band
  (they're matched, so this says what distinguishes you from the field at
  each level). Partial-dependence of rating on each metric.
- Outputs: report with trajectory charts and a ranked "what moved" table.
- Depends on: Q1.
- Caveats: 2024 is thin (639 games); rating reliability after breaks
  (`days_since_last_in_pool`) should be a control.

### 3. Time pressure: how your play degrades on a low clock, vs others — planned

- Goal: error vs clock left (seconds, fraction of base, relative to the
  opponent), overall and in endgames; whether you degrade more or less
  than opponents in the same games; what rating your play under 10 s
  resembles.
- Inputs: `clock_before_seconds`, `clock_fraction`, `clock_diff`,
  `ep_loss`, `criticality`, `phase`, `is_player_move`.
- Method: binned curves with clustered CIs, then a regression of
  `ep_loss` on clock bucket × player (you/opponent) × phase with
  criticality as a control; the interaction term is the susceptibility
  test. Rating mapping: opponents' rating vs their mean `ep_loss` per
  time class gives a curve; read your low-clock `ep_loss` off it.
- Outputs: report with curves, the susceptibility estimate, the rating
  equivalent with error bars.
- Depends on: F7. Independent of Q1.
- Caveats: time pressure correlates with position type and game length;
  stratify by phase and material. Rating mapping is indicative only.

### 4. Clock allocation and losses on time — planned

- Goal: where extra seconds buy you accuracy and where they don't; whether
  you spend time in the right places; what the 935 losses on time cost.
- Inputs: `time_spent_seconds`, `criticality`, `ep_criticality`,
  `ep_loss`, `phase`, result = timeout.
- Method: `ep_loss` vs time spent by criticality band and phase, you vs
  opponents; "misallocation" = long thinks in low-criticality positions
  and snap moves in high-criticality ones; for timeouts, `p_win` at the
  moment you flagged summed = points lost to the clock.
- Outputs: report with a where-to-spend-time map and the timeout cost.
- Depends on: Q3's binning code.
- Caveats: you think longer on harder positions, so raw correlations
  mislead; criticality controls help but don't fully fix it. "Optimal"
  allocation is directional, not a policy.

### 5. Sessions: fatigue, warm-up and tilt — planned

- Goal: does play degrade over a session (fatigue), start poorly (warm-up),
  or collapse after losses (tilt)? Does session length relate to results?
- Inputs: `session`, `game_in_session`, `minutes_into_session`, Q1 per-game
  metrics, previous results in the session.
- Method: per-game `ep_loss` and result-vs-expected by game number and
  minutes, with the stopping-rule bias handled by comparing within
  sessions of equal length (or game k vs 1 only in sessions that reached
  k+1). Tilt: metrics after a loss vs after a win, after two losses, with
  rating-expected result as the baseline. Check 15/30/60-minute cutoffs.
- Outputs: report with fatigue, warm-up and tilt curves; a "stop after
  N" suggestion if the data supports one.
- Depends on: Q1.
- Caveats: you choose when to stop; that selection is the main threat.

### 6. Time of day and day of week — planned

- Goal: when do you play best, after controlling for session position,
  time class and opponents?
- Inputs: `local_hour`, `weekday`, Q1 metrics, Q5 session variables.
- Method: hourly buckets with CIs, then a regression with session and
  time-class controls; opponents' error in the same games as the
  comparison (same hour, same pool).
- Outputs: short report.
- Depends on: Q1, Q5.
- Caveats: Europe/London assumed; travel breaks it.

### 7. Expected value of not resigning — planned

- Goal: how many points your 791 resignations gave away, and when
  resigning was right.
- Inputs: `p_win_before`, `p_draw_before` at the resignation position
  (with both clocks), time class, clock at resignation.
- Method: sum of P(win) + ½P(draw) over resigned games, by time class;
  compare with how played-out games from similar positions ended;
  separate "had to go" resignations (lots of time, position not clearly
  lost, quick final move) from "gave up".
- Outputs: report with the total, by time class, and a resignation rule
  of thumb (e.g. "in bullet, never below p_loss 0.9").
- Depends on: F6, Q1 (for the comparison set).
- Caveats: the model is trained on games that were played out, which is
  the right counterfactual, but opponents who are ahead may play
  differently against someone who hasn't resigned.

### 8. Opening repertoire — planned (new)

- Goal: which openings you score and play well in, by colour; whether
  staying in book longer helps; the lines that cost you most.
- Inputs: `opening_name`, `opening_eco`, `book_ply`, `in_book`, Q1
  metrics, `ep_loss` in the opening phase.
- Method: per opening (min 20 games): score, expected score from ratings,
  opening-phase `ep_loss`, eval after book; same for opponents in those
  openings. Book depth vs outcome.
- Outputs: report with a repertoire table and a "fix these lines" list.
- Depends on: Q1.
- Caveats: opening names at this level describe the first 2–4 moves;
  treat it as repertoire shape, not theory.

### 9. Blunder anatomy — planned (new)

- Goal: what kinds of mistakes you make and miss: hanging pieces, missed
  mates (5,144 missed, 5,602 allowed), missed captures, one-move vs deeper
  tactics, by piece and phase; which types improved over time.
- Inputs: `label`, `cp_loss`, `best_move`, `missed_mate`, `allowed_mate`,
  `fen_before`, `uci`, `criticality`.
- Method: classify each blunder by replaying the engine's best move and
  the played move with python-chess: was the best move a capture of a
  hanging piece, a mate, a check, a defensive move; did the played move
  hang a piece (is it capturable for free on the next move); depth of the
  refutation (how many plies until material is lost in the engine line,
  using the stored second-best/best evals as a proxy or a short re-search
  for a sample). Rates you vs opponents.
- Outputs: report with a taxonomy table and the most common patterns.
- Depends on: F3; a small re-analysis with Stockfish for refutation depth
  on a sample is optional.
- Caveats: taxonomy by rules is approximate; keep categories coarse.

### 10. Endgame technique — planned (new)

- Goal: do you convert theoretically won endgames and hold drawn ones?
  Which endgame types go wrong (K+P, rook endings, queen vs pawn)?
- Inputs: `tb_wdl_before/after`, `tb_error` (160 cases), `tb_dtz`, material
  columns, clocks.
- Method: for every tablebase position: outcome class vs eventual result;
  error rates by material type and by clock; DTZ vs time spent.
  Beyond 5 pieces: use `ep_loss` in the endgame phase by material class.
- Outputs: report with conversion/hold rates and the worst endgame types.
- Depends on: F5.
- Caveats: only 9,940 moves are in tablebase range; group material types
  coarsely.

### 11. Performance vs opponent rating and upsets — planned (new, small)

- Goal: do you over- or under-perform against stronger/weaker opponents
  relative to rating expectation; what's different in those games?
- Inputs: ratings before, result, Q1 metrics.
- Method: result vs Elo expectation by rating-difference bin; error rates
  by bin, both sides.
- Depends on: Q1. Can be folded into Q2's report.

### 12. Style fingerprint — exploratory, last

- Goal: can a classifier pick your games out from same-rated strangers',
  and on which features?
- Inputs: per-player-per-game features: clock usage profile, `ep_loss`
  distribution, move-kind rates (captures, checks, castling timing, pawn
  moves, piece preferences), opening choices by colour, book depth.
- Method, step 1 (no new data): you vs your opponent in the same games;
  exclude result, ratings, names; group CV by game and by year. Step 2
  (the honest test): download games of ~200 opponents against third
  parties (one API request at a time, 1–2 hours) and test whether you're
  separable from a same-rated stranger more than two strangers are from
  each other. Run with and without clock and opening features.
- Depends on: F7; step 2 on a background download job.
- Caveats: likely dominated by clock habits and openings. Prior work:
  Maia; McIlroy-Young et al. 2021 (behavioural stylometry).

### 13. Synthesis: what to work on — last, single agent

One report that ranks the findings by expected rating gain: which habit
(time use, resignation, a specific opening, an endgame type, session
length) would move your rating most, with the evidence from the reports
above. Depends on everything.

## Part C: How the work is organised

**Foundations were a single-agent, sequential job** (each stage depended on
the previous one's tables) and are done. **The questions are mostly
independent**, so they can run as parallel agents, each in its own
worktree, each writing `analysis/<nn>_<slug>.py` and
`reports/<nn>_<slug>.md` (plus figures), merged in order. Within a
question the work is a single agent.

Dependency graph (→ = needs):

```
F7 toolkit ─┬→ Q1 decomposition ─┬→ Q2 trajectory (+Q11 upsets)
            │                    ├→ Q5 sessions/tilt → Q6 time of day
            │                    ├→ Q7 resignation EV
            │                    └→ Q8 openings
            ├→ Q3 time pressure ─→ Q4 clock allocation
            ├→ Q9 blunder anatomy
            ├→ Q10 endgame technique
            └→ Q12 fingerprint (step 2 needs the opponent download job)
Q13 synthesis → all of the above
```

Suggested waves:

| Wave | Parallel agents | Notes |
|---|---|---|
| 0 | F7 toolkit (done) | single agent; everything else imports it |
| 1 | Q1, Q3, Q9, Q10, and start the Q12 opponent download in the background | five independent jobs; Q1 is the one others wait on |
| 2 | Q2 (+Q11), Q4, Q5, Q7, Q8 | each needs one wave-1 result |
| 3 | Q6, Q12 | Q6 needs Q5's session controls; Q12 needs its download |
| 4 | Q13 synthesis | single agent, reads all reports |

Rules for every agent: use `load_data()` from `chess_analysis.toolkit`,
report bullet/blitz/rapid separately, pair every "you" number with the
opponents' number from the same games, cluster CIs by game, don't use
post-game ratings as predictors, and update the question's status and notes
here.

### How to run the waves (brief for a fresh session)

Nothing below needs Stockfish, tablebases or the network; all inputs are
committed. Setup is in CLAUDE.md ("Starting in a fresh session").

1. Launch each question of a wave as its own agent in its own git worktree
   (Agent tool with `isolation: "worktree"`), in parallel. Give each agent:
   the question's section from this file verbatim; `analysis/README.md`
   and `analysis/_template.py` as the shape to follow; the data facts in
   CLAUDE.md; the deliverables: `analysis/<nn>_<slug>.py` (re-runnable,
   `python analysis/<nn>_<slug>.py` writes the report), `reports/<nn>_<slug>.md`
   with figures, any reusable per-game table committed as
   `data/<user>/<name>.parquet` if other questions need it (Q1 writes
   `games_decomposed.parquet`), and the status/notes update to its section
   here. Tell it the report must state numbers with CIs, compare with
   opponents, and end with a short "what this means for the player"
   paragraph and a list of caveats.
2. When a wave's agents finish, review each report for sanity (orders of
   magnitude, CIs present, bullet/blitz/rapid split), merge the worktrees
   into the branch in question order (resolve the PLAN.md notes edits by
   keeping all), run `pytest -q`, commit, push.
3. Start the next wave. Wave 2 agents should read the wave-1 reports they
   depend on (Q1 for Q2/Q5/Q7/Q8; Q3 for Q4).
4. The Q12 opponent download (step 2 of Q12) is a serial, rate-limited
   job: start it in the background at wave 1 with `archive.download()` for
   ~200 opponents chosen from `my_games.opponent` (most frequent first),
   into `data/opponents/<name>/raw/`; it needs the network only.
5. Finish with Q13 as a single agent reading every report.

Slugs: 01_decomposition, 02_trajectory, 03_time_pressure,
04_clock_allocation, 05_sessions, 06_time_of_day, 07_resignation,
08_openings, 09_blunder_anatomy, 10_endgames, 11_upsets (may live inside
02), 12_fingerprint, 13_synthesis.
