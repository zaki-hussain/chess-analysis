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

### 1. Where points are won and lost: your mistakes vs your opponents' — done

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

Notes:
- Status: **done** (revised after review). Table `data/zakihussa1n/games_decomposed.parquet` (6,029 rows keyed by `uuid`, same order as `load_data().games`, 62 columns, deterministic); report `reports/01_decomposition.md`; script runs in ~17 s.
- Budget: both sides give away ~0.9 expected points per game; paired difference in your favour 0.009 [0.000, 0.017] bullet, 0.000 [-0.011, 0.013] blitz, 0.025 [0.003, 0.048] rapid. Score minus move-1 expectation: +0.008 / +0.019 / +0.044. Per-move phase differences are ≤0.001 ep: bullet endgames cleaner than opponents' on every measure, bullet openings slightly worse, blitz middlegame tail slightly fatter (≥0.2: +0.003 [-0.000, 0.006]). Moves losing ≥0.1 are ~10% of moves and 52–57% of all ep lost, for both sides.
- Decisive mistake: the plan's "last crossing below 10%" rule does not work with the calibrated P(win) (it decays gradually and keeps flag chances alive in bullet); implemented instead: in games that ended with the loser < 10% after their last own move, the loser's costliest move from the last P(win) ≥ 20% position to the end (blunder if ≥ 0.05 ep). Judged over the loser's own moves only (`decisive_by` is always the loser). Found in 85–92% of on-board losses; identical profile for you and opponents (0.18–0.27 ep, mostly endgame, ~20 plies from the end).
- Lost while still alive (loser ≥ 10% after last own move): 37% / 42% / 48% of your bullet / blitz / rapid losses vs 32% / 39% / 26% of opponents' — flags, resignations and mates (in bullet ~46% of mates count as "alive" because the model's clock term keeps flag chances open); input for Q4 and Q7 (`loser_last_p_win`, `decisive_*` columns).
- Conversion (win | reached P(win) ≥ 80%): 0.911 vs 0.866 bullet (+0.045 [0.021, 0.066]), 0.920 vs 0.877 blitz, 0.935 vs 0.826 rapid. Swindle (not loss | fell to ≤ 20%): 0.219 vs 0.171 bullet (+0.047 [0.024, 0.071]), 0.229 vs 0.176 blitz, 0.269 vs 0.173 rapid; holds at every threshold. Of the swindle *wins*, 69% [62%, 74%] of yours in bullet came by the opponent flagging vs 58% [51%, 64%] of theirs (blitz 22% vs 22%; as a share of all swindles: 48% vs 37% bullet). You reach ≥80% positions slightly less often than opponents in bullet (0.440 vs 0.452), more often in rapid (0.497 vs 0.412).
- Model caveat for every downstream user of P(win): the pooled model is under-confident at the extremes (model 0.8–0.9 → observed 0.958 for you, 0.920 for opponents) and you out-perform it in every bin while opponents under-perform it; absolute threshold rates are model-relative, you-vs-them comparisons are the robust part. `net_ep` correlates 0.71 with your score. Rating match (opponent post-game rating + your rating change vs your pre-game rating): median |gap| 23, mean |gap| 48, no systematic gap.
- Downstream (Q2, Q5, Q7, Q8): join `games_decomposed.parquet` on `uuid`; use `my_ep_lost`/`opp_ep_lost` (and per-phase versions), `my_errors_ge_20`, `reached_winning`+outcome for conversion, `reached_losing`+outcome for swindles, `decisive_*` for turning points; never use `my_ep_end`/`my_p_win_end` as predictors (they encode the result).

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

### 3. Time pressure: how your play degrades on a low clock, vs others — done

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

Notes:
- Exposure, not composure: you play 11.7% [11.2, 12.1] of bullet moves under 10 s (opponents 11.4%) and reach <10 s in 48.4% [46.5, 50.0] of bullet games (opponents 51.0% [49.0, 52.5]); blitz 2.3% of moves, rapid ~0 (21 moves), so rapid low-clock cells are uninformative and the fraction-of-base curves are the rapid view.
- Raw ep_loss rises from 0.029 (30-60 s) to 0.040 [0.037, 0.043] under 5 s in bullet, identically for both sides; in no low-clock cell are you worse than opponents (10-30 s bullet: -0.0026 [-0.0040, -0.0011] in your favour); the only cells where you are slightly worse are at 30-60 s (bullet +0.0007 [0.0002, 0.0012]).
- Low-clock moves are 89-94% endgame moves at roughly double the stakes (bullet 10-30 s: 71%); adjusting for phase/stakes/ply/material/opponent clock removes the ep_loss rise entirely for both sides (bullet <5 s: you -0.0045 [-0.0069, -0.0021], opp -0.0023 [-0.0042, -0.0002]). Susceptibility interaction in ep: bullet -0.0022 [-0.0048, 0.0004], blitz +0.0025 [-0.0072, 0.0126] -> no excess susceptibility in expected points.
- In centipawns the clock does bite and you degrade more than opponents in bullet under 5 s (gap +7.9 [2.3, 15.6] cp per move); the ep calibration (clocks are features) discounts positional damage when the mover is about to flag, so ep_loss understates low-clock sloppiness and attributes the cost of getting into time pressure to earlier moves (Q4's territory).
- Moves under 10 s cost 0.123 [0.114, 0.131] ep per bullet game (opp 0.122), 13.6% [12.7, 14.4] of all your bullet ep_loss; blitz 0.026 [0.019, 0.033] vs 0.019.
- Rating mapping only works in cp_loss (ep_loss is flat in rating by construction; endgame-only cp curve has no usable slope) and uses a reconstructed pre-game opponent rating (`opponent_rating_after + my_rating_change`, see `opponent_rating_pre()` in the script): bullet play under 10 s resembles a 775 [606, 907] player vs 1050 [1005, 1107] at 30+ s (actual mean 932); indicative only, blitz too wide to quote.
- For Q4: in blitz you are 30+ s behind on the clock twice as often as opponents (11.7k vs 5.8k moves) and lose more than them when 10-30 s behind (+0.0053 [0.0021, 0.0090]); the lever is spending less time earlier, not playing better when short.

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

### 9. Blunder anatomy — done

- Goal: what kinds of mistakes you make and miss: hanging pieces, missed
  mates (3,339 missed, 5,602 allowed), missed captures, one-move vs deeper
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

Notes:
- Blunder rates (cp_loss ≥ 300) equal the matched opponents' in every time class: bullet 6.16 [5.97, 6.35] vs 6.00 [5.80, 6.18] per 100 moves, blitz 5.70 [5.46, 5.95] vs 5.85 [5.64, 6.09], rapid 5.53 [5.15, 5.96] vs 5.72 [5.31, 6.12]; blunders carry 28–34% of each side's total ep_loss, 0.13–0.19 ep each.
- Taxonomy (rule-based, SEE on python-chess replay) is the same for both sides: 42% of your blunders hand over material (moved en prise 15%, ignored a threatened piece 14%, exposed another piece 8%, losing capture 4%), 13% allow mate, 12% a check tactic, 28% a quiet/deeper refutation (of which 29% from already-won positions, 40% endgame). Queen hung 726 times (12.0 [11.2, 13.0] per 100 games vs 13.7 [12.8, 14.7] for opponents).
- The two robust differences: you **allow mates more often** (bullet +0.18 [0.11, 0.24] longer, +0.11 [0.07, 0.16] mate-in-1 per 100 moves; holds within every eval band, so not exposure: you spend fewer moves in lost positions than opponents), and your blunders are **less often missed free material** (−0.22 to −0.34 per 100 moves in all time classes) and more often a missed quiet move (+0.4 bullet/blitz). Missing an available mate is identical for both sides (bullet 18% vs 17.5%, blitz 14% vs 13.5%).
- Refutation depth (game-line proxy, no re-search): of refuted blunders that could be followed, 65% lost material on the next move, 21% within 2–3, 12% mate, 3% deeper. Punishment: 55–58% of your blunders punished (next reply cp_loss < 100) vs 55–65% of theirs by you; losing captures are punished most (75–79%), check tactics and quiet refutations least (45–46%).
- By piece: queen moves blunder most per move (bullet 9.2 vs 8.2 per 100, +1.03 [0.33, 1.79]); blitz knight moves safer for you (−0.98 [−1.65, −0.36]). Middlegame is the blunder phase (8.2–8.7 per 100) and where you allow mates at ~1.5× the opponents' rate (1.17 vs 0.68 bullet, 1.20 vs 0.79 blitz).
- Over the years: overall and hung-material rates fell for both sides together (bullet 7.9 → 5.4 per 100 moves: a change in the games, not in you); your bullet check-tactic blunders fell 1.10 → 0.47 (now equal); your allowed-mate rate stayed at 0.7–0.95 while the opponents' fell to 0.4–0.5 (a persistent gap).
- Data note for F3: the stored `missed_mate` flag is True for the 1,801 moves that delivered mate (mate_after == 0); real missed mates are ~3,336, not 5,137/5,144 as stated in the plan. Corrected inside this script only; `src/analysed.py` should mask `mate_after == 0` (delivered) out of `missed_mate`. Q13 should cite the corrected count.
- Downstream: Q13 should use "allows mates more often, misses free material less often" as the player-specific pattern; Q3/Q4 might split the allowed-mate gap by clock.

### 10. Endgame technique — done

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

Notes:
- Tablebase layer is small: 9,898 moves / 469 games, 160 errors (72 yours, 88 theirs); all cell CIs are wide, treat differences as unresolved unless the CI excludes zero.
- Conversion of tablebase wins: you 75.7% [69.5, 81.9] (210 games) vs opponents 72.2% [65.7, 77.8] (198); bullet only 63.4% [52.7, 73.1] vs 84.8% blitz, 88.0% rapid. Hold rate of drawn entries: you 80.3% [70.5, 90.2] (61 games).
- Per-move tablebase error rate: you 2.6 [1.8, 3.4] vs opponents 3.2 [2.5, 4.0] per 100 at-risk moves. You spoil wins less (win->draw 1.4 vs 2.8 per 100) but lose drawn positions more (draw->loss 7.2 [4.0, 10.2] vs 4.2 [2.0, 6.9]), mainly pawn endings (5.3 vs 1.1) and rook endings. Defending drawn endings is the technique gap.
- Worst material class for both sides: rook endings, 12.9 [7.7, 18.3] vs 13.6 [7.6, 22.1] errors per 100, almost all KRP vs KR from either side. Q/R vs bare king are near-flawless (<1 per 100).
- Main leak is the clock, not technique: 33 of your 51 failed conversions had no tablebase error and ended by 'timeout vs insufficient material' (23) or lost on time (10); 29 of those are bullet. Opponents: 19 of 55. Q4 (clock allocation) and Q7 (resignation EV) should note this: in a trivially won bullet ending the task is to mate fast and keep mating material.
- No visible clock-bucket gradient in TB error rate (2.1-3.0 per 100 across all buckets); by DTZ you err less than opponents at every distance and spend slightly less time per move in blitz/rapid when winning.
- Beyond 5 pieces (121k endgame-phase moves), ep_loss by coarse material class matches opponents in nearly every cell; queen+others endgames are the noisiest class for both (~0.036 per move), pawn/minor/queen-only endings the quietest (~0.02). Reusable helpers inside the script: piece_counts(fen), tb_material_class, big_material_class, canonical_material, termination_reason.

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
