import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("findings", ROOT / "analysis" / "13_findings.py")
findings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(findings)


def test_parse_value_handles_the_printed_forms():
    assert findings.parse_value("0.187 [0.162, 0.213]") == (0.187, 0.162, 0.213, False)
    assert findings.parse_value("76% [70, 82]") == (76.0, 70.0, 82.0, True)
    assert findings.parse_value("-0.0022 [-0.0048, +0.0004]") == (-0.0022, -0.0048, 0.0004, False)
    assert findings.parse_value("−0.21 [−0.31, −0.09]") == (-0.21, -0.31, -0.09, False)
    assert findings.parse_value("1,049") == (1049.0, None, None, False)
    with pytest.raises(ValueError):
        findings.parse_value("0.5 [0.6, 0.7]")
    with pytest.raises(ValueError):
        findings.parse_value("from 79 to 70")


def record(**overrides):
    base = {
        "id": "demo_one", "section": "Results", "claim": "c", "metric": "m", "unit": "points per game", "time_class": "blitz",
        "player": "0.187 [0.162, 0.213]", "opponents": None, "difference": None, "n": None, "kind": "effect", "lever": True,
        "higher_is_better": False, "stake": None, "stake_unit": None, "stake_note": None, "caveat": None,
    }
    base.update(overrides)
    return base


def test_validate_file_checks_numbers_verbatim_against_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(findings, "repo_root", lambda: tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "demo.md").write_text("## Results\n\nThe last game costs 0.187 [0.162, 0.213] points; opponents 0.1 [0.0, 0.2].\n")
    path = tmp_path / "demo.json"
    path.write_text(json.dumps({"question": "demo", "report": "reports/demo.md", "findings": [
        record(),
        record(id="demo_two", player="0.188 [0.162, 0.213]"),  # not in the report
        record(id="demo_three", section="Methods"),  # not a heading
        record(id="demo_four", difference="0.1 [0.0, 0.2]", kind="comparison"),
    ]}))
    records, problems = findings.validate_file(path)
    assert len(records) == 4
    assert any("does not occur verbatim" in p for p in problems)
    assert any("not a heading" in p for p in problems)
    assert len(problems) == 2
    one, _, _, four = records
    assert one["player_est"] == 0.187 and one["ci_excludes_zero"] is True and one["ci_basis"] == "player"
    assert four["ci_basis"] == "diff" and four["ci_excludes_zero"] is False
