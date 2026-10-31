# Analysis plan

What we want to learn from the games, whether each question can be answered
well, how, and notes for later. Update the notes under each question as work
happens. Numbers quoted are for zakihussa1n's data as of 2026-10-31
(6,081 games, 365,561 moves in standard-rules games).

Status key: **done** · **next** · **planned** · **exploratory** (worth trying,
may not give a clean answer).

## Foundations

### F1. Engine-free enrichment — done

`moves_enriched.parquet`: position faced (FEN), legal-move count, material,
phase, move kind (piece, capture, check, castling, promotion), clock before
the move, time spent, both clocks at decision time, `is_player_move`.
`my_games.parquet`: time control parts, local start time, sessions, days
since the last game in the pool, games in the pool in the last 30 days.

Notes:
- Phase is a heuristic (endgame ≤ 6 non-pawn pieces, Lichess's rule;
  opening until material comes off or move 12). Revisit with an opening book
  (F4). Shares: 35% opening, 29% middlegame, 36% endgame.
- Daily games (18) have no time columns: their `%clk` meaning is unverified.
- Rating-reliability proxies (days since last game, games in last 30 days)
  stand in for Glicko RD, which chess.com doesn't publish historically.

### F2. Stockfish evaluation of every position — next

`chess-analysis analyse` → `data/<user>/evals.parquet`, keyed by FEN
(ignoring move counters), joined to moves via `fen_before` and the game's
final `fen`. Resumable: evaluates only FENs not yet in the table, writes a
checkpoint every few hundred positions, so it can run anywhere in chunks and
the results are committed like any other data.

Settings: fixed node count, 1 thread per engine, 4 engines in parallel here;
Stockfish version, node count, and whether tablebases were available
recorded in every row. Fixed nodes + 1 thread + same binary = reproducible.
Columns: eval (cp or mate-in), best move, second-best move and its eval
(MultiPV 2, for position criticality), depth reached, Stockfish WDL.

Notes:
- 371,608 positions, 310,197 unique (17% fewer engine calls). 61% of
  opening positions are unique: even openings don't repeat that much.
- Budget: ~100k–300k nodes/position is enough to find blunders and mistakes
  at 700–1,300 rating; estimated ~10–30 core-hours (verify with a benchmark
  first). This container: 4 cores, background jobs ≤ 2 h, so run in chunks
  and commit. Deeper passes (1M+ nodes, ~100 core-hours) belong on a laptop
  overnight or a ~16-vCPU cloud machine (a few dollars).
- Installing Stockfish needs the official release binary from GitHub; the
  download was blocked by session permissions once and needs approval.
- Derived later, not stored by the engine step: eval before/after from the
  mover's view, centipawn loss, expected-points loss (F3), is_best,
  criticality (best minus second-best).

### F3. Win-probability calibration — planned

Stockfish's scale means little at 1,000 rating (+3 in bullet is far from a
sure win). Fit P(win/draw/loss | eval, phase, both clocks, time class,
rating) on our own games played to the end, both sides' positions, result as
label. Move quality then becomes *expected points lost*, comparable across
phases and time controls. Check against Lichess's cp→win% formula and
Stockfish's WDL as baselines; check against chess.com's accuracies on the 300
games that have them.

Notes:
- Positions within a game share a result; cluster by game when assessing
  fit. Consider a Lichess database sample of same-rated games if our own
  data is too thin in some region (e.g. big advantages in rapid).

### F4. Opening book — planned, low priority

Is it useful? Partly. Not for move quality: Stockfish already judges opening
moves, and at this level "book" ends within a few moves. Useful for:
(a) a principled end of the opening phase (first move out of known lines)
instead of "move 12"; (b) how deep each side stays in known lines, as a
feature for the style fingerprint (Q7); (c) naming the opening per game.

Approach: the static Lichess `chess-openings` TSV (ECO code + name per
position, a few thousand lines, deterministic, no API calls). Store
`opening_name` and `book_ply` per game. Avoid the live opening-explorer API:
rate-limited and its answers change over time.

### F5. Syzygy tablebases — planned

Worth doing, modestly. 20,219 positions have ≤ 7 pieces, 9,940 have ≤ 5.
The 3-4-5-piece tables are ~1 GB; 6-piece are ~150 GB (no); 7-piece are
impractical. Two uses: (1) point Stockfish at them so evals in those
positions are exact; (2) probe them directly with python-chess for an exact
win/draw/loss of each ≤ 5-piece position, which makes "did I throw away a
won endgame" and endgame accuracy under time pressure (Q2) exact rather than
engine-estimated. Download needs the same kind of permission as Stockfish.

## Questions

### Q1. Controlling for rating, phase, time control — planned

Method: stratify or regress on the enriched table, clustering errors by
game. Rating at the time is `my_rating_before`; reliability proxies from F1.
Phase from F1 (refined by F4). Always split bullet vs blitz vs rapid (57% of
games are bullet). The comparison group for "vs others" questions is the
opponents in the same games: 5,498 players matched within ±27 rating points
(median difference 0), same time controls and times of day, whose moves are
evaluated anyway.

### Q2. Endgame accuracy vs time pressure and time use — planned

Expected-points lost per move in endgames by clock bucket (seconds left,
fraction of base time, and relative to the opponent), with the opponents'
curve from the same games alongside. Time-use effectiveness: does spending
longer reduce your error, controlling for criticality (F2) and phase.
Tablebase positions (F5) give exact answers for small endgames.

### Q3. Session fatigue and warm-up — planned

Sessions already computed (30-minute gap: 1,502 sessions, median 3 games,
109 with ≥ 10 games). Measure accuracy, blunder rate and result-vs-expected
(from ratings) by `game_in_session` and `minutes_into_session`.

Notes:
- Stopping rules bias late-session games: you quit when tilted or play on
  when winning. Compare within sessions of equal length, or game k vs game 1
  only in sessions that reached k+1.
- Check the 30-minute cutoff against 15 and 60 (1,640 and 1,290 sessions).

### Q4. Expected value of not resigning — planned, with caveats

791 resignations (401 blitz, 226 bullet, 162 rapid, 2 daily). For each, the
calibrated P(win) and P(draw) at the moment of resignation from F3 (eval,
both clocks, phase, time class), trained on games played to the end at this
level. Sum to expected points given away.

Notes:
- Median clock when resigning: bullet 38 s, blitz 129 s, rapid 489 s: often
  plenty of time left. Report bullet and blitz mainly, and separate "had to
  go" resignations (lots of time, position not clearly lost, quick final
  move) from "gave up".
- The opponent could still flag: include their clock in the model.

### Q5. Time of day — planned

`local_hour` and `weekday` (Europe/London, DST handled). Games cluster
10:00–22:00: enough for hourly buckets. Use rating-adjusted results and
move accuracy. Control for session position (Q3) and time class.

Notes:
- Travel breaks the timezone assumption; the profile says GB.
- Time of day correlates with who else is online (opponent pool) and
  session length; check both.

### Q6. Your skill vs opponents' blunders — planned

In engine terms a move can only lose expected points, so each game
decomposes into points you gave away and points they gave away. Derived
stats: who made the last big mistake, conversion rate from a given
advantage, swindle rate from lost positions, your error distribution vs
theirs by phase and clock.

### Q7. Style fingerprint — exploratory

Classifier on per-player-per-game features: clock usage profile, error
distribution, capture/check/castling/pawn-move rates, piece preferences,
opening choices by colour (F4), depth in book.

Step 1 (no new data): you vs your opponent within the same games. Exclude
result, ratings and names; group cross-validation by game and by year.
Step 2 (the honest test): download games of ~200 opponents against third
parties (1–2 hours of API calls) and check you're separable from a
same-rated stranger more than two strangers are from each other.

Notes:
- Expect clock habits and opening repertoire to dominate; run with and
  without them to see what's left.
- Prior work: Maia (human-like move prediction by rating) and "behavioural
  stylometry" in chess (McIlroy-Young et al., 2021).

### Q8. Clock allocation — descriptive planned, prescriptive exploratory

Estimate where extra seconds buy accuracy: error vs time spent by
criticality and phase, you vs opponents. Compare with where you actually
spend time. Separately: 935 losses on time; the eval when you flagged says
how many points the clock cost while you were winning.

Notes:
- You think longer on harder positions, so raw correlations mislead;
  criticality controls help but don't fully fix it.
- "Optimal" allocation needs a model of your own error-vs-time curve; treat
  any recommendation as directional.

### Q9. Performance under time pressure vs others — exploratory

Fit error as a function of clock left with separate curves for you and the
opponent population (same games, so a fair comparison): do you degrade more
or less than others? For "what rating does your play under 10 seconds look
like": map accuracy to rating using the opponents (their rating vs their
accuracy at the same time control). Wide error bars; report as indicative.
