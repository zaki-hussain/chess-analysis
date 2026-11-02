"""Checks presentation/README.md against the CSVs its numbers come from.

The Markdown is written by hand; the numbers in it must not drift from the
data. For every sub-question block (a `### ` heading up to the next heading)
this script:

- collects the CSVs the block links to (its `Data:` line);
- checks every number in the block's bullet points against the values in
  those CSVs (numeric cells, and numbers inside text cells such as
  'late 21-23'), at the precision the number is printed: '0.055' matches any
  value that rounds to 0.055, '48.4%' matches 0.484 or 48.4, and a number
  may be written without its sign ('scores 0.055 less');
- checks the block's `**Short answer:**` line (one per question) against all
  of that question's CSVs.

It also checks that every image and link target exists. Ignored: numbers in
`code`, time-control labels like 1+0 or 3+2, the sub-question numbers in
headings, and everything inside the collapsed "Method and caveats" blocks
(method settings such as '500 resamples' are not data).

Run: .venv/bin/python analysis/check_presentation.py [path/to/file.md]
Exit code 1 if anything fails.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "presentation" / "README.md"

NUMBER = re.compile(r"(?<![\w.])[-−+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])[-−+]?\d+(?:\.\d+)?%?")
TC_LABEL = re.compile(r"\b\d+(?:\.\d+)?\+\d+\b")
CODE = re.compile(r"`[^`]*`")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
CSV_LINK = re.compile(r"\]\(([^)\s]+\.csv)\)")


def numbers_in(text: str) -> list[str]:
    text = CODE.sub(" ", text)
    text = LINK.sub(" ", text)  # link targets hold paths with digits in them; link text is kept below
    text = TC_LABEL.sub(" ", text)
    return [m.group(0) for m in NUMBER.finditer(text)]


def parse(token: str) -> tuple[float, int, bool]:
    """(absolute value, decimals printed, is percent)."""
    pct = token.endswith("%")
    core = token.rstrip("%").replace(",", "").replace("−", "-").lstrip("+-")
    decimals = len(core.split(".", 1)[1]) if "." in core else 0
    return float(core), decimals, pct


def csv_values(path: Path) -> list[float]:
    values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            for cell in row:
                try:
                    values.append(float(cell))
                except ValueError:
                    for token in NUMBER.findall(TC_LABEL.sub(" ", cell)):
                        try:
                            values.append(parse(token)[0])
                        except ValueError:
                            pass
    return values


def matches(token: str, values: list[float]) -> bool:
    value, decimals, pct = parse(token)
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    candidates = [abs(v) for v in values]
    if pct:
        candidates = candidates + [abs(v) * 100 for v in values]
    return any(abs(c - value) <= tol for c in candidates)


def split_blocks(lines: list[str]) -> list[tuple[str, int, list[str]]]:
    """(heading, line number, lines) for every heading block."""
    blocks: list[tuple[str, int, list[str]]] = []
    for i, line in enumerate(lines, start=1):
        if line.startswith("#"):
            blocks.append((line.strip(), i, []))
        elif blocks:
            blocks[-1][2].append(line)
    return blocks


def strip_details(lines: list[str]) -> list[str]:
    out, inside = [], False
    for line in lines:
        if line.strip().startswith("<details"):
            inside = True
        if not inside:
            out.append(line)
        if line.strip().startswith("</details"):
            inside = False
    return out


def check(path: Path) -> list[str]:
    base = path.parent
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    problems: list[str] = []
    cache: dict[Path, list[float]] = {}

    def values_for(paths: list[Path]) -> list[float]:
        out: list[float] = []
        for p in paths:
            if p not in cache:
                cache[p] = csv_values(p) if p.exists() else []
            out.extend(cache[p])
        return out

    # 1. every link and image target exists
    for i, line in enumerate(lines, start=1):
        for target in LINK.findall(line):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (base / target.split("#", 1)[0]).exists():
                problems.append(f"line {i}: missing link target {target}")

    # 2. numbers against the CSVs. A question (## heading) owns the CSVs of all its sub-questions.
    blocks = split_blocks(lines)
    question_csvs: dict[int, list[Path]] = {}
    current_q = None
    for k, (heading, _, body) in enumerate(blocks):
        if heading.startswith("## "):
            current_q = k
            question_csvs[k] = []
        if current_q is not None:
            question_csvs[current_q].extend(base / t for t in CSV_LINK.findall("\n".join(body)))

    current_q = None
    for k, (heading, start, body) in enumerate(blocks):
        if heading.startswith("## "):
            current_q = k
        visible = strip_details(body)
        own = [base / t for t in CSV_LINK.findall("\n".join(visible))]
        for offset, line in enumerate(body, start=1):
            if line not in visible:
                continue
            stripped = line.strip()
            if stripped.startswith("**Short answer:**"):
                pool = values_for(question_csvs.get(current_q, []))
                scope = "this question's CSVs"
            elif stripped.startswith(("- ", "* ")) and heading.startswith("### "):
                pool = values_for(own)
                scope = "this sub-question's CSVs"
                if not own:
                    problems.append(f"line {start + offset}: bullet under '{heading}' but the block links no CSV")
                    continue
            else:
                continue
            for token in numbers_in(stripped):
                if not matches(token, pool):
                    problems.append(f"line {start + offset}: {token!r} not found in {scope}: {stripped[:120]}")
    return problems


def main() -> None:
    path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT
    problems = check(path)
    for p in problems:
        print(p)
    print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
