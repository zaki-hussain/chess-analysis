# Findings as data (Q13)

One JSON file per report, `<nn>_<slug>.json`, hand-extracted from
`reports/<nn>_<slug>.md`. `analysis/13_findings.py` validates every file,
derives the numeric columns and writes `data/zakihussa1n/findings.parquet`
and `reports/13_findings.md`. Check one file with

```bash
.venv/bin/python analysis/13_findings.py --check analysis/findings/05_sessions.json
```

## File shape

```json
{
  "question": "05_sessions",
  "report": "reports/05_sessions.md",
  "findings": [
    {
      "id": "sessions_tilt_score_after_loss",
      "section": "4. Tilt",
      "claim": "The game after a loss scores worse than the game after a win, relative to the model's expectation.",
      "metric": "score minus model expectation, next game after a loss minus after a win",
      "unit": "points per game",
      "time_class": "all",
      "player": "-0.055 [-0.084, -0.028]",
      "opponents": null,
      "difference": null,
      "n": "6,027 games",
      "kind": "effect",
      "lever": true,
      "higher_is_better": true,
      "stake": null,
      "stake_unit": null,
      "stake_note": null,
      "caveat": "Within-session comparison; the stopping rule (a loss ends the session more often) is handled by excluding the game stopped after."
    }
  ]
}
```

## Fields

| field | type | meaning |
|---|---|---|
| `id` | string | unique across all files, `snake_case`, starts with the question's slug word |
| `section` | string | text of a `##`/`###` heading of the report the numbers come from (matched case-insensitively as a substring) |
| `claim` | string | one neutral sentence; no advice |
| `metric` | string | what is measured, precisely enough to recompute |
| `unit` | string | `points per game`, `ep per move`, `cp per move`, `per 100 moves`, `share`, `%`, `seconds`, `games`, `AUC`, ... |
| `time_class` | string | `bullet`, `blitz`, `rapid` or `all` |
| `player` | string or null | the player's number **as printed in the report**: `est`, `est [lo, hi]`, `est% [lo, hi]` |
| `opponents` | string or null | the opponents' (or strangers') number from the same games, as printed |
| `opponents_is` | string, optional | what the `opponents` value is: `opponents` (default: the opponents in the same games), `strangers` (Q12 step 2), `control` (the player's own control condition in a contrast) |
| `difference` | string or null | the paired player-minus-opponents (or treatment-minus-control) difference, as printed |
| `n` | string or null | sample size as printed (`6,027 games`, `788 resignations`) |
| `kind` | string | `comparison` (player vs opponents), `effect` (a condition or habit vs its baseline), `null_result` (an effect the report rules out), `description` (a fact about the games) |
| `lever` | bool | the player could act on it (change a habit) |
| `higher_is_better` | bool or null | sign convention of `player`/`difference` from the player's view; null when it is not a quality measure |
| `stake` | string or null | expected points per game at stake if the behaviour changed, as printed in the report |
| `stake_unit` | string or null | usually `points per game`; `points per 100 games` and `points total` are allowed |
| `stake_note` | string or null | how the report derives the stake |
| `caveat` | string or null | the report's own caveat for this number |

Every string in `player`, `opponents`, `difference`, `stake` must occur
verbatim in the report (whitespace and the Unicode minus are normalised);
the validator refuses anything else, so nothing in the table can be
re-derived or rounded by hand. The script parses them into
`*_est`, `*_lo`, `*_hi` and sets `ci_excludes_zero` from `difference` when
present, else from `player`.
