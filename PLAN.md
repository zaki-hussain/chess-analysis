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

### 2. Rating trajectory: what changed as you improved — done

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

Notes:
- Status: **done** (Q11 folded in as section 6). Script `analysis/02_trajectory.py` runs in ~12 s, deterministic; report `reports/02_trajectory.md` with three figures. 6,023 games (the first game of each pool has no pre-game rating; one game with rating 0 dropped).
- The trajectory is not monotone: bullet 160 (Apr 2022) to a 1300 peak (Apr 2025) and back to ~800 by the last game; blitz 252 to 1050 (peak Feb 2024); rapid 526 to 1222. Bands pool games from the climb and the fall; 60+1 games sit mostly in bullet bands 700-900 and 300+x in blitz bands below 700 (time-control confound; main-tc sensitivity in section 4).
- Absolute accuracy improved with the level, not relative to it: low vs high third of games by rating, cp per move 79 to 70 in bullet for you (-9 [-12, -5]) vs -13 [-16, -9] for opponents (you minus field +3.7 [1.5, 5.5] cp, blunders +0.46 [0.07, 0.77] per 100 moves); blitz -17 for both. In ep per move you match the field at every band (differences within ±0.001) and the >= 0.2 ep error rate fell identically for both sides (bullet 2.65 to 1.69 per 100 moves). Phase ep per move: no band where you differ from the field beyond ±0.003.
- What is yours: losses on time fell relative to the field (bullet share of losses on time 0.53 to 0.45, you minus field -0.102 [-0.180, -0.018]; blitz 0.25 to 0.16, -0.089 [-0.178, -0.004]; rapid 0.06 to 0); blitz play sped up more than the field's (-0.32 [-0.50, -0.17] s/move) and the end-of-game clock gap closed partly (+0.031 [0.008, 0.056] of base) but you still leave less time than opponents in every blitz band below 1000 (0.42 vs 0.53 at 300-399; 0.46 vs 0.51 at 900-999). Half the bullet seconds-per-move drop is the 60+1 to 60+0 mix (-0.64 overall, -0.30 in 60+0 only).
- What did not move: the conversion edge (+0.04 to +0.10 over the field from band 700 up in bullet, every band in blitz) did not grow; in bullet your conversion fell 0.937 to 0.895 (-0.042 [-0.073, -0.005]) while the field's stayed flat, and the swindle edge narrowed (-0.056 [-0.110, 0.003]). Losses while still alive stayed at 0.35-0.39 in bullet, above the field's 0.31 in most bands.
- Game-level regression of rating on your own per-game metrics (standardised, tc + log days-since-last-game controls): % of base time spent per move is the strongest term in every class (-91 [-127, -60] bullet, -51 [-67, -35] blitz, -97 [-128, -70] rapid points per SD), then book depth (+22 / +32 / +50) and cp per move (-20 / -41 / -29); signs survive year dummies at smaller magnitudes. Descriptive only (R2 0.24-0.34 without year).
- Downstream (Q13): the rating gain came with the level, not from a visible fix in move quality; the player-specific movers are clock habits (fewer flags, faster blitz) and the stable edges are conversion/swindle. Q4 should use the blitz end-of-clock gap by band; Q7 the stable lost-while-alive share.

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

### 4. Clock allocation and losses on time — done

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

Notes:
- Status: **done**. Script runs in ~2 min (includes a deterministic refit of F6's win model, which reproduces stored `ep_after` to within 0.0000, used to re-value positions under counterfactual clocks).
- Flag cost (position after the winner's last move, flagged side to move, valued with the flagged side's clock set equal to the winner's): bullet 0.375 [0.361, 0.388] ep per flag, 305.7 [294.8, 316.9] points over 816 flags (incl. 44 flag draws), 0.0885 [0.0826, 0.0945] per bullet game (opponents 0.1188 [0.1125, 0.1260]); blitz 60.1 [53.9, 66.4] points / 153 flags. As-played (0.14) and board-only Stockfish (0.43) valuations bracket it. Net to you +104.5 points in bullet (+3.0 per 100 games), -5.3 blitz: the clock is a net gift only because opponents flag more often (1,049 vs 772 bullet).
- The flags are a pace problem: in blitz the opponent had 30+ s in 78.4% [72.5, 84.3] of your flags; your clock at move 20 is already 27.4 vs 34.5 s (bullet, flagged vs other games) and 85.8 vs 125.8 s (blitz); across all blitz games you are -7.8 [-10.0, -5.5] s at move 20 and -11.4 at move 30 (behind by 3+ s in 54-57% of games). Time goes to the opening (bullet 1.50 vs 1.39 s/move, blitz 5.18 vs 4.60) and blitz middlegame (7.54 vs 7.12). Only 5.5% [4.0, 7.2] of your bullet flags came from winning boards (opponents 2.4%).
- Allocation: in bullet your time follows the stakes less than opponents' (time on high-stakes / no-stakes positions 1.63 [1.58, 1.69] vs 1.96 [1.89, 2.03]), you snap 21.1% vs 18.9% of high-stakes positions; blitz is the reverse (23.8% vs 29.3%). ~35% of bullet long thinks and ~49% of blitz long thinks land on positions where the two best moves are equally good, for both sides.
- Value of time cannot be identified from time spent: error rises with seconds spent for both sides even after stakes/phase/clock/legal-move/eval controls (selection). Robust part is the side contrast: your fast moves are cleaner than opponents' (<0.5 s bullet 34.3 vs 45.1 cp; pre-moves 8.4% vs 10.0% of bullet moves) and your longest thinks worse (8+ s bullet 153 vs 134 cp, 20+ s blitz 141 vs 127; adjusted gradient steeper by 36.1 [18.8, 54.7] and 25.5 [11.9, 37.7] cp).
- Clock charge (model price of the seconds, same position re-valued with the clock as if the move were instant): bullet ~1/60 point per second (0.024 ep for a 1-2 s move at 30+ s, 0.12 for 8+ s), blitz 0.004 for 2-5 s at 30+ s; per game bullet 0.935 vs 0.932 (cancels between sides), blitz 0.248 vs 0.211 (you pay 0.038 [0.023, 0.053] more per game for your pace).
- Accounting fact for Q13 and anyone reading `ep_loss` in bullet: the unfloored change `ep_before - ep_after` averages ~0 per move (bullet +0.0005) because the calibration already prices in typical time use; `ep_loss` is its positive part and its steep rise with seconds spent in bullet is largely the clock charge, not board error. Do not subtract the charge from `ep_loss` (floor artefact favours long thinks). `cp_loss` is the clock-free currency for time-use questions.
- For Q13: directional advice is pace (opening and no-stakes positions at snap speed, cap long thinks), not low-clock composure; a bullet flag costs nearly as much per game as all under-10 s moves together (0.089 vs 0.123 ep).

### 5. Sessions: fatigue, warm-up and tilt — done

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

Notes:
- Status: **done** (revised after review). Script `analysis/05_sessions.py` (~18 s), report `reports/05_sessions.md`, three figures. Sessions recomputed over standard live games only (`session_features(games, gap_minutes)`), 1,500 at 30 min; median 3 games, bullet median 4 and 14% of bullet sessions reach 10+ games; 7% of sessions mix time classes.
- The stopping rule is the main effect and the main confounder: P(session ends | loss) 0.329 vs P(ends | win) 0.173 (difference 0.156 [0.137, 0.179]; bullet 0.290 vs 0.114, blitz 0.376 vs 0.254, rapid 0.417 vs 0.243). The hazard is roughly flat across game numbers for both results (after a win 0.09–0.21, after a loss 0.30–0.43) and the loss−win gap persists at every game number; the last game of a session scores 0.199 [0.172, 0.226] below the rest. Long sessions gain rating (+25 [14, 36] per 11+ game session, 73% of them positive) because you keep playing while winning, not because you play better late.
- No fatigue in move quality: paired game-k-minus-game-1 within sessions that reached k+1 (non-last games) is never significantly positive in any time class (bullet game 10+: +0.0064 [-0.0027, 0.0153] ep/move, opponents +0.0056; all classes 10+: +0.0015 [-0.0085, 0.0098]); within-session regression slope -0.0010 [-0.0028, 0.0005] ep/move per doubling of game number, score − expectation -0.002 [-0.024, 0.024]. The naive/pooled score slope (+0.044 [0.021, 0.070] per doubling) is between-session selection. No warm-up either (game 2 − game 1: -0.0011 [-0.0043, 0.0018] ep/move); in equal-length sessions the opponents' ep/move follows yours position by position, so within-session shape = game composition.
- Tilt is real but mostly in results: next game after a loss vs after a win scores -0.055 [-0.084, -0.028] vs model expectation (bullet -0.052, blitz -0.047, rapid -0.106); your ep/move +0.0024 [0.0005, 0.0043] but opponents' +0.0017 [-0.0001, 0.0035] in the same games, move-weighted you−them gap only +0.0007 [0.0002, 0.0012]. The resolved contrast is "after a loss vs after a win"; a dose-response in the streak length is NOT resolved (3+ losses vs 0: -0.084 [-0.154, -0.020], but 3+ vs 1: -0.038 [-0.101, 0.034]; LL − WL before a game: -0.028 [-0.073, 0.019]). After a loss you resign a larger share of your losses (+0.084 [0.046, 0.122]), spend +0.16 [-0.04, 0.34] s/move more (opponents +0.01), do not re-queue faster; a blown win (lost after P(win) ≥ 0.8) does not tilt more than an ordinary loss (+0.047 [-0.040, 0.129]).
- No fixed "stop after N" is supported (after game 3 vs games 1–3 of the same sessions: -0.041 [-0.075, -0.008] score − expectation, -0.0002 ep/move; other N unresolved); if any rule, it is about the previous result. 15/30/60-minute gap definitions agree on all headlines (tilt score -0.054/-0.055/-0.054; your ep/move +0.0022/+0.0024/+0.0025 vs opponents' +0.0016/+0.0017/+0.0019; fatigue 5+ vs 1 within ±0.002 ep/move).
- Methodological: a within-session (fixed-effect) regression must not include the previous result (lagged outcome in 2–10 game panels → Nickell bias flips the sign); tilt is estimated from the pooled model and the conditional tables instead. Per-game "you − them" gaps should be move-weighted ratio differences, not means of per-game rates (the two disagree by 3×).
- For Q6: use `session_features()` from this script for the controls (game_in_session, minutes_into_session, is_last_in_session, prev_outcome / loss_streak); the time-of-day analysis should drop or flag last games of sessions and include a previous-result dummy, since the session position carries a −0.2 score effect through the stopping rule and ~−0.05 through tilt.

### 6. Time of day and day of week — done

- Goal: when do you play best, after controlling for session position,
  time class and opponents?
- Inputs: `local_hour`, `weekday`, Q1 metrics, Q5 session variables.
- Method: hourly buckets with CIs, then a regression with session and
  time-class controls; opponents' error in the same games as the
  comparison (same hour, same pool).
- Outputs: short report.
- Depends on: Q1, Q5.
- Caveats: Europe/London assumed; travel breaks it.

Notes:
- Status: **done**. Script `analysis/06_time_of_day.py` (~20 s, deterministic; imports `session_features`/`build_games`/`boot_sums`/`cluster_ols` from `analysis/05_sessions.py` via importlib), report `reports/06_time_of_day.md`, four figures. 6,027 games in 1,500 sessions; six bands (night 0-6: 280 games, morning 7-11: 1,055, midday 12-14: 922, afternoon 15-17: 1,624, evening 18-20: 1,403, late 21-23: 743); CIs clustered by session.
- No time-of-day effect survives the controls. Raw: late 21-23 scores -0.046 [-0.086, 0.001] vs the rest (paired ep gap +0.0007 [-0.0001, 0.0014], flags/loss +0.040 [-0.014, 0.104]), midday +0.038 [0.001, 0.077]; 3 of 46 band-vs-rest contrasts exclude zero (~2 expected by chance); across 19 hourly buckets your and the opponents' ep/move move together (r = 0.91). Regression with Q5's session controls + time class + rating + year: band coefficients on score -0.023 (late) to +0.037 (midday) vs afternoon, only midday × paired ep gap excludes zero (-0.0016 [-0.0029, -0.0001]); session-level permutation test of the band block p = 0.46 (score), 0.19 (ep gap), 0.74 (your ep), 0.90 (lost on time / s per move).
- The midday candidate does not replicate (2021-23: +0.082 [0.038, 0.124] score, -0.0015 ep gap; 2024-26: -0.012 [-0.066, 0.038], 0.0000); sign of raw band effects agrees in only 2 of 6 bands between halves. Treat it as a chance hit.
- Weekdays: Thursday +0.047 [0.014, 0.076] is the only day excluding zero (1 of 7); weekend − weekday +0.007 [-0.025, 0.037] score, -0.0001 [-0.0007, 0.0005] ep gap; weekend evenings (464 games) vs weekday nights (815): +0.022 [-0.044, 0.084], ep gap -0.0004 [-0.0015, 0.0007]. 42-cell weekday × band grid is a heat-map of noise around zero.
- In the same model the session effects dominate: last game of session -0.187 [-0.213, -0.162], previous game lost -0.053 [-0.093, -0.010] (Q5's numbers reproduced with hour controls); seconds per move and flag share are flat across hours within a time class.
- Night games (01-07h, 165) are episodic (Dec 2025: 90, Apr 2023: 45) with ordinary-hour games the same days, so not obviously travel; night band without Dec 2025 -0.003 [-0.091, 0.062]. Hours are confounded with era (night 52% from 2025-26, morning 67% from 2022-23) and time class (night 80% bullet); year, class and rating are controls.
- For Q13: time of day and weekday are not levers (nothing to rank); the advice stays "stop after a loss", whatever the hour. Any future hour claim should pass the session-permutation test and replicate across halves.

### 7. Expected value of not resigning — done

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

Notes:
- Status: **done**. Script `analysis/07_resignation.py` (~16 s), report `reports/07_resignation.md`. 788 of your resignations and 810 by opponents among 6,029 games; the resignation position is the position after the last move, flipped to the resigner's view when the opponent moved last (you moved last in 21% of yours).
- Two valuations: model EV = P(win)+½P(draw) at the position (model view, fitted on all games incl. resigned ones, under-confident at extremes) and the robust played-out valuation: observed score of positions in the same model-EV bin, same side and time class, from games that side did not resign/abandon (board-terminal positions excluded), joint game bootstrap over comparison and resigned games. An all-positions variant (resigned games included) is the model-free lower bound.
- Cost: your resignations gave away ≈131 points played-out (model 132, lower bound 104), 0.166 per resignation; opponents' resignations handed you ≈97 (0.119 each): net −34 to you. Per 100 games you 1.08 [0.90, 1.25] bullet (≈7 Elo), 2.89 [2.46, 3.36] blitz (≈20 Elo), 6.19 [4.71, 7.81] rapid (≈43 Elo) vs opponents 1.04 / 2.46 / 2.02.
- Rapid is the leak: you resign 0.656 [0.591, 0.712] of rapid losses vs opponents' 0.521 (diff 0.135 [0.050, 0.213]; bullet/blitz equal), 27% of your rapid resignations from model P(win) ≥ 0.2 vs 11% of theirs, and you out-score the model from bad rapid positions (played-out EV/resignation 0.225 vs model 0.170). 26% of your resignations are from still-playable (≥0.2) positions and carry 65% of the cost; the 26% from clearly lost (<0.05) carry 3%.
- Bullet: the opponent's clock decides. Your played-out positions with mate/≤ −2000 against you scored 0.32 with the opponent under 10 s (0.09 at ≥30 s), −1000..−500 scored 0.60 vs 0.28; but 82% of your bullet resignations came with the opponent at ≥30 s, so bullet resignations are mostly sound. Rule of thumb (played-out upper CI ≤ 0.05): resign only below model EV 0.05–0.10 in bullet (opponent ≥30 s) and blitz, below 0.02–0.05 in rapid, never with the opponent short on time in bullet; in engine terms no band is free (mate/≤ −2000 scored 0.20/0.08/0.12 bullet/blitz/rapid when played on, −1000..−500 0.37/0.23/0.27).
- 'Had to go' proxy (playable, ≥25% clock, quick last own move): 65 resignations (8%), 29 points; 49% [37%, 62%] were the session's last game vs 35% [31%, 39%] of lost resignations (+0.14 [+0.01, +0.27]), so the proxy has signal. 13–14% of your resignations followed a ≥0.2 ep blunder on your last own move (opponents 14–16%).
- Caveat for Q13: the played-out comparison set is itself selected (players who played on may have had reasons), so it is an upper-ish bound; the model view is the lower-ish bound; both agree to within ~10% in bullet/blitz. The plan's "791 resignations" is 788 among standard live games.

### 8. Opening repertoire — done

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

Notes:
- Status: **done**. Script `analysis/08_openings.py` (~50 s), report `reports/08_openings.md`, figures under `reports/figures/08_openings/`. Joins `games_decomposed.parquet` for `my/opp_ep_lost_opening` and `my_ep_start`; families = `opening_name` before the colon (95 families; those with ≥ 20 games cover 93% of each colour).
- Repertoire shape: 1.e4 in 88% of white games, 1...e5 vs 1.e4 and 1...d5 vs 1.d4; the Sicilian as Black (89 games, score − expected −0.10 [−0.20, −0.00]) is entirely 2022 at rating ~340 and no longer played. Overall score − Elo expectation +0.030 [0.013, 0.047] as White, +0.005 [−0.014, 0.023] as Black; per-family CIs almost all straddle zero. Clear positives: QGD Austrian as Black (+0.085 [0.009, 0.153], 138 games), Bishop's Opening as Black (+0.095 [0.020, 0.161], 148), Tennison Gambit transposition (labelled 'Zukertort Opening') as White (+0.143 [0.025, 0.247], 61). Clear negative still in use: 1.Nf3 lines as Black (−0.21 [−0.31, −0.09], 38 games).
- Opening-phase quality equals the opponents' in ep per move in 5 of 6 colour × time-class cells; the exception is bullet as Black (+0.0013 [0.0007, 0.0018] ep per move, plies 11–30). In cp you are better as White in every time class (bullet −4.6 [−6.3, −3.0], blitz −6.7, rapid −11.1 cp per move).
- Book depth is not a lever: score − expected is flat from 1–2 to 9+ book plies in every time class (all CIs overlap), 'book gain' (ep after book − ep at move 1) is within ±0.02; you leave the named lines first in 64% / 65% / 63% of bullet / blitz / rapid games, and your deviating move costs less than the opponents' (bullet −0.004 [−0.006, −0.001] ep; cp 39 vs 57), i.e. you leave book by choice.
- Fix list (lines ≥ 20 games with the largest deficit vs expectation, with recent games): White: Scandinavian Main Line (36 games, −0.148 [−0.311, 0.004]), Giuoco Piano (145, −0.031 [−0.106, 0.047]), Blackburne-Kostić Gambit (40, −0.085); Black: Scotch Game (62, −0.126 [−0.248, 0.001], you lose 0.058 [0.018, 0.103] ep more than opponents in the opening), QP Chigorin (40, −0.142), Four Knights Scotch Accepted (34, −0.144). The 'years' / 'last 12 mo' columns matter: several deficit lines (Two Knights Modern Bishop's Opening, Smith-Morra as Black, 2.f3 vs the Scandinavian) are 2022 history.
- Recurring opening mistakes by exact position (≥ 5 times, mean ep_loss ≥ 0.05): 6.Qf3 instead of 6.Nxf7 in the Fried Liver (66 times 2022–25, 0.063 [0.048, 0.077] ep each, 4.1 ep in total), 4.Ng5 in the Giuoco Piano (11×, 0.116), 8.Ba4 in the Polerio Bishop Check line (19×, 0.062), 6...Ke8 in a Scotch line (8×, 0.41 ep, score 0.00), 3...Nf6 in the Bishop's Opening (6×, 0.39 ep). Opponents' most expensive repeated move against you: 7.Ke1 in the Traxler (16×, 0.26 ep, you score 0.94).
- For Q13: openings are not where the edge or the leak is; the concrete deliverables are the ~5 recurring positions above and the Scotch/Scandinavian-Main-Line deficits. Names are by position (transpositions mislabel: 'Zukertort Opening' as White = 1.e4 d5 2.Nf3).

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

### 11. Performance vs opponent rating and upsets — done (in 02_trajectory)

- Goal: do you over- or under-perform against stronger/weaker opponents
  relative to rating expectation; what's different in those games?
- Inputs: ratings before, result, Q1 metrics.
- Method: result vs Elo expectation by rating-difference bin; error rates
  by bin, both sides.
- Depends on: Q1. Can be folded into Q2's report.

Notes:
- Status: **done**, as section 6 of `reports/02_trajectory.md` (script `analysis/02_trajectory.py`). Opponent pre-game rating = `opponent_rating_after + my_rating_change` in rated games, `opponent_rating_after` in unrated (approximate under Glicko; stated in the report). Pairing is tight: median |difference| 22 points, 82% of rated games within 50.
- Result vs Elo expectation (rated, pooled): no deficit against weaker opponents (<= -100: 0.704 vs 0.724, excess -0.020 [-0.087, 0.040], 179 games) and a clear excess against stronger ones (> 100: 0.403 vs 0.256, +0.148 [0.087, 0.210], 202 games; 50..100: +0.064 [0.010, 0.121]); bullet +0.187 [0.099, 0.275], blitz +0.108 [0.024, 0.201], rapid has too few wide pairings. The excess holds both below and from a rating of 700 (+0.150 / +0.144).
- Checks: your rating changes in the outer bins scale as Elo predicts (win +16.4 vs +8.8 against even opponents; Elo ratio 1.44, observed 1.88), so the stronger opponents were not provisional accounts; the reconstruction bias would push results the other way; unrated games (exact ratings) show a smaller, non-significant excess (> 100: +0.067 [-0.039, 0.174], 70 games).
- Error rates by bin for both sides: ep per move rises symmetrically at both ends (0.036-0.038 in the outer bins vs 0.028 when even); in cp you lose less than weaker opponents (65 vs 77) and more than stronger ones (83 vs 75); your conversion exceeds the opponents' in every bin.
- Upsets: wins against >= +100 opponents (95 games) came 40% by the opponent flagging (ordinary wins 44%) with opponents losing 0.039 ep per move; upset losses (70 games) are games where you played badly (0.050 ep per move vs 0.032 in ordinary losses), half of them while still alive. Q13: the over-performance against stronger opponents is small in games (about 200) but consistent across bullet and blitz.

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
