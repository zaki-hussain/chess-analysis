import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_presentation", Path(__file__).resolve().parent.parent / "analysis" / "check_presentation.py")
check_presentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_presentation)


def _write(tmp_path, body):
    (tmp_path / "data" / "q").mkdir(parents=True)
    (tmp_path / "data" / "q" / "t.csv").write_text("tc_group,band,score,score_lo,share,games\nbullet 1+0,late 21-23,-0.0551,-0.084,0.4843,2937\n")
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "c.svg").write_text("<svg/>")
    md = tmp_path / "README.md"
    md.write_text(body)
    return md


GOOD = """## 1. A question?

**Short answer:** in 1+0 you score 0.055 less (48.4% of games).

### 1.1 A sub-question?

![chart](figures/c.svg)

- Late games (21-23) score −0.055 (CI −0.084 to −0.055) in bullet 1+0.
- That covers 2,937 games, 48% of them `n=7`.

Data: [table](data/q/t.csv) · [up](#1-a-question)

<details><summary>Method and caveats</summary>

- 500 resamples (not data, not checked).
</details>
"""


def test_good_file_passes(tmp_path):
    assert check_presentation.check(_write(tmp_path, GOOD)) == []


def test_wrong_number_missing_link_and_unbacked_bullet_fail(tmp_path):
    bad = GOOD.replace("−0.084", "−0.085").replace("figures/c.svg", "figures/missing.svg").replace("#1-a-question", "#no-such-heading") + "\n### 1.2 No data?\n\n- 7 things.\n"
    problems = check_presentation.check(_write(tmp_path, bad))
    assert any("0.085" in p for p in problems)
    assert any("missing link target figures/missing.svg" in p for p in problems)
    assert any("links no CSV" in p for p in problems)
    assert any("no heading for anchor #no-such-heading" in p for p in problems)
