"""Q13: the findings of every report as one table.

Reads analysis/findings/<nn>_<slug>.json (hand-extracted from the reports, numbers
copied as printed), validates each record against its report, derives numeric
columns and writes data/<user>/findings.parquet and reports/13_findings.md.

    python analysis/13_findings.py                      # validate everything and build
    python analysis/13_findings.py --check FILE.json    # validate one file, build nothing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

from chess_analysis.config import load_settings
from chess_analysis.toolkit import Report, repo_root

SLUG = "13_findings"
FINDINGS_DIR = repo_root() / "analysis" / "findings"
TIME_CLASSES = {"bullet", "blitz", "rapid", "all"}
KINDS = {"comparison", "effect", "null_result", "description"}
NUMBER_FIELDS = ("player", "opponents", "difference", "stake")
REQUIRED = {
    "id", "section", "claim", "metric", "unit", "time_class", "player", "opponents", "difference",
    "n", "kind", "lever", "higher_is_better", "stake", "stake_unit", "stake_note", "caveat",
}
NUM = r"[+−-]?\d[\d,]*(?:\.\d+)?"
VALUE_RE = re.compile(rf"^(?P<est>{NUM})(?P<pct>%?)(?:\s*\[\s*(?P<lo>{NUM})%?,\s*(?P<hi>{NUM})%?\s*\])?$")


def normalise(text: str) -> str:
    """Whitespace runs to one space, Unicode minus and non-breaking space to ASCII."""
    return re.sub(r"\s+", " ", text.replace("−", "-").replace(" ", " ")).strip()


def to_float(token: str) -> float:
    return float(token.replace("−", "-").replace(",", "").replace("+", ""))


def parse_value(text: str) -> tuple[float, float | None, float | None, bool]:
    """'0.187 [0.162, 0.213]' -> (0.187, 0.162, 0.213, False); '76% [70, 82]' -> (76.0, 70.0, 82.0, True)."""
    m = VALUE_RE.match(normalise(text))
    if not m:
        raise ValueError(f"not a number or 'est [lo, hi]': {text!r}")
    est = to_float(m["est"])
    lo = to_float(m["lo"]) if m["lo"] else None
    hi = to_float(m["hi"]) if m["hi"] else None
    if lo is not None and hi is not None:
        if lo > hi:
            lo, hi = hi, lo  # a few tables print the interval the other way round
        if not (lo - 1e-9 <= est <= hi + 1e-9):
            raise ValueError(f"CI does not bracket the estimate: {text!r}")
    return est, lo, hi, bool(m["pct"])


def headings(report_text: str) -> list[str]:
    return [normalise(line.lstrip("#").strip()) for line in report_text.splitlines() if line.startswith("#")]


def validate_file(path: Path) -> tuple[list[dict], list[str]]:
    """Return the records of one file and the list of problems (empty when valid)."""
    problems: list[str] = []
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [], [f"{path.name}: invalid JSON: {exc}"]
    for key in ("question", "report", "findings"):
        if key not in doc:
            problems.append(f"{path.name}: missing top-level key {key!r}")
    if problems:
        return [], problems
    if doc["question"] != path.stem:
        problems.append(f"{path.name}: question {doc['question']!r} does not match the file name")
    report_path = repo_root() / doc["report"]
    if not report_path.exists():
        return [], problems + [f"{path.name}: report {doc['report']} not found"]
    report_text = normalise(report_path.read_text())
    report_headings = headings(report_path.read_text())
    records: list[dict] = []
    seen: set[str] = set()
    for i, f in enumerate(doc["findings"]):
        where = f"{path.name}#{i} ({f.get('id', '?')})"
        missing = REQUIRED - set(f)
        extra = set(f) - REQUIRED
        if missing:
            problems.append(f"{where}: missing fields {sorted(missing)}")
        if extra:
            problems.append(f"{where}: unknown fields {sorted(extra)}")
        if missing:
            continue
        if not re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", f["id"]):
            problems.append(f"{where}: id must be snake_case")
        if f["id"] in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(f["id"])
        if f["time_class"] not in TIME_CLASSES:
            problems.append(f"{where}: time_class {f['time_class']!r} not in {sorted(TIME_CLASSES)}")
        if f["kind"] not in KINDS:
            problems.append(f"{where}: kind {f['kind']!r} not in {sorted(KINDS)}")
        if not isinstance(f["lever"], bool):
            problems.append(f"{where}: lever must be true/false")
        if f["higher_is_better"] not in (True, False, None):
            problems.append(f"{where}: higher_is_better must be true/false/null")
        for key in ("claim", "metric", "unit", "section"):
            if not isinstance(f[key], str) or not f[key].strip():
                problems.append(f"{where}: {key} must be a non-empty string")
        section = normalise(str(f["section"])).lower()
        if section and not any(section in h.lower() for h in report_headings):
            problems.append(f"{where}: section {f['section']!r} is not a heading of {doc['report']}")
        if f["player"] is None and f["difference"] is None and f["opponents"] is None:
            problems.append(f"{where}: at least one of player / opponents / difference is needed")
        if (f["stake"] is None) != (f["stake_unit"] is None):
            problems.append(f"{where}: stake and stake_unit go together")
        row = {
            "question": doc["question"], "report": doc["report"], "finding_id": f["id"], "section": f["section"],
            "claim": f["claim"], "metric": f["metric"], "unit": f["unit"], "time_class": f["time_class"], "kind": f["kind"],
            "lever": f["lever"], "higher_is_better": f["higher_is_better"], "n": f["n"],
            "stake_unit": f["stake_unit"], "stake_note": f["stake_note"], "caveat": f["caveat"],
        }
        for key in NUMBER_FIELDS:
            prefix = {"player": "player", "opponents": "opp", "difference": "diff", "stake": "stake"}[key]
            value = f[key]
            row[f"{key}_text"] = value
            row[f"{prefix}_est"] = row[f"{prefix}_lo"] = row[f"{prefix}_hi"] = None
            if value is None:
                continue
            if not isinstance(value, str):
                problems.append(f"{where}: {key} must be the printed string, not {type(value).__name__}")
                continue
            if normalise(value) not in report_text:
                problems.append(f"{where}: {key} {value!r} does not occur verbatim in {doc['report']}")
            try:
                est, lo, hi, pct = parse_value(value)
            except ValueError as exc:
                problems.append(f"{where}: {key}: {exc}")
                continue
            row[f"{prefix}_est"], row[f"{prefix}_lo"], row[f"{prefix}_hi"] = est, lo, hi
            if key == "player":
                row["player_is_percent"] = pct
        basis = "diff" if f["difference"] is not None else "player"
        lo, hi = row.get(f"{basis}_lo"), row.get(f"{basis}_hi")
        row["ci_basis"] = basis if lo is not None else None
        row["ci_excludes_zero"] = (lo > 0 or hi < 0) if lo is not None and hi is not None else None
        row.setdefault("player_is_percent", None)
        records.append(row)
    if not records and not problems:
        problems.append(f"{path.name}: no findings")
    return records, problems


COLUMNS = [
    "question", "finding_id", "time_class", "kind", "lever", "claim", "metric", "unit", "higher_is_better",
    "player_text", "player_est", "player_lo", "player_hi", "player_is_percent",
    "opponents_text", "opp_est", "opp_lo", "opp_hi",
    "difference_text", "diff_est", "diff_lo", "diff_hi", "ci_basis", "ci_excludes_zero",
    "stake_text", "stake_est", "stake_lo", "stake_hi", "stake_unit", "stake_note",
    "n", "section", "report", "caveat",
]


def build(records: list[dict]) -> pd.DataFrame:
    table = pd.DataFrame(records)[COLUMNS]
    for col in ("lever",):
        table[col] = table[col].astype(bool)
    for col in ("higher_is_better", "ci_excludes_zero", "player_is_percent"):
        table[col] = table[col].astype("boolean")
    for col in [c for c in COLUMNS if c.endswith(("_est", "_lo", "_hi"))]:
        table[col] = pd.to_numeric(table[col]).astype("float64")
    ids = table["finding_id"]
    if ids.duplicated().any():
        raise SystemExit(f"duplicate finding ids across files: {sorted(ids[ids.duplicated()])}")
    return table.sort_values(["question", "finding_id"], kind="stable").reset_index(drop=True)


def write_report(table: pd.DataFrame, path_out: Path) -> Path:
    report = Report(SLUG, "Findings as data")
    report.text(
        f"{len(table)} findings from {table['question'].nunique()} reports, extracted into `analysis/findings/*.json` with every number copied as "
        f"printed in its report (validated verbatim by `analysis/13_findings.py`) and written to `{path_out.relative_to(repo_root())}`. "
        "No prose here: the table is the deliverable. Columns are documented in `analysis/findings/README.md`."
    )
    counts = table.groupby("question").agg(findings=("finding_id", "size"), levers=("lever", "sum"), ci_excludes_zero=("ci_excludes_zero", lambda s: int(s.fillna(False).sum()))).reset_index()
    report.heading("Per report")
    report.table(counts)
    levers = table[table["lever"] & table["stake_est"].notna()].sort_values("stake_est", ascending=False, kind="stable")
    report.heading("Levers with a stake in expected points")
    report.table(levers[["question", "finding_id", "time_class", "stake_text", "stake_unit", "claim"]])
    report.heading("All findings")
    show = table[["question", "finding_id", "time_class", "kind", "lever", "player_text", "opponents_text", "difference_text", "ci_excludes_zero", "unit", "claim"]].copy()
    show["ci_excludes_zero"] = show["ci_excludes_zero"].map({True: "yes", False: "no"}).fillna("")
    report.table(show.fillna(""))
    return report.write()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", type=Path, help="validate this one file and exit")
    args = parser.parse_args()
    if args.check:
        records, problems = validate_file(args.check)
        for p in problems:
            print(p)
        print(f"{args.check.name}: {len(records)} findings, {len(problems)} problems")
        return 1 if problems else 0
    files = sorted(FINDINGS_DIR.glob("*.json"))
    if not files:
        print(f"no findings files in {FINDINGS_DIR}")
        return 1
    all_records: list[dict] = []
    all_problems: list[str] = []
    for path in files:
        records, problems = validate_file(path)
        all_records.extend(records)
        all_problems.extend(problems)
        print(f"{path.name}: {len(records)} findings, {len(problems)} problems")
    if all_problems:
        print("\n".join(all_problems))
        return 1
    table = build(all_records)
    settings = load_settings()
    out = settings.data_dir / settings.username.lower() / "findings.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)
    report_path = write_report(table, out)
    print(f"wrote {out} ({len(table)} rows) and {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
