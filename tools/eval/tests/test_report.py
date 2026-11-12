import json

import pytest

from hanji_eval import guard, holdout, report
from hanji_eval.datasets import Registry

TEXT = ('[[dataset]]\nname = "hanji-ko-v1"\nversion = "1"\nlicense = "MIT"\nlicense_basis = "LICENSE"\nuse = "x"\n'
        'publishable = true\nroles = ["selection"]\n'
        'splits = ["dev", "holdout"]\nsealed_splits = ["holdout"]\n[[dataset.files]]\npath = "f"\n'
        f'sha256 = "{"1" * 64}"\n')
REGISTRY = Registry.from_text(TEXT)


def _row(tool: str, total: int, scored: int, excluded: dict) -> dict:
    return {"dataset": "hanji-ko-v1", "split": "dev", "tool": tool, "unit": "block", "total": total,
            "scored": scored, "excluded": excluded}


def _report(ledger: list[dict], split: str = "dev") -> dict:
    source = REGISTRY.source("hanji-ko-v1", split)
    return {
        "schema": "hanji-eval-report/1",
        "run": {"name": "synthetic", "date": "2026-11-12", "harness_commit": "a" * 40, "engine_commit": "b" * 40,
                "rules": {"norm": "NORM_V1", "num": None, "adapter": None, "mask": "mask1", "truth_version": 1}},
        "env": guard.machine_env({}) | {"tools": [], "datasets": [], "split": "hanji-ko-v1"},
        "ledger": ledger, "failures": [], "holdout_opened": {"opened": False, "entry": None},
        "results": [{"dataset": "hanji-ko-v1", "split": split, "tool": "hanji", "tool_version": "0.1.0",
                     "config": "default", "metric": "ece", "agg": "micro", "value": 0.0312, "ci95": [0.02, 0.04],
                     "n": 120, "n_failed": 1, "flags": ["few_clusters"],
                     "sources": [{"dataset": source.dataset, "split": source.split}]}],
    }


def test_the_markdown_is_made_from_the_json_only(tmp_path):
    rep = _report([_row("hanji", 10, 7, {"outside_gt": 2, "excluded_ruby": 1}),
                   _row("docling", 12, 8, {"ambiguous": 3, "excluded_ruby": 1})])
    report.write(rep, tmp_path, REGISTRY, tmp_path / "log.json")
    written = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert written == rep
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert md == report.render_md(written)
    assert "| hanji-ko-v1 | dev | hanji | 0.1.0 | default | ece | micro | 0.0312 | [0.0200, 0.0400] | 120 | 1 | " \
           "few_clusters |" in md
    assert "| hanji-ko-v1 | dev | hanji | block | 10 | 7 | excluded_ruby 1, outside_gt 2 |" in md


def test_a_ledger_that_does_not_add_up_is_refused(tmp_path):
    with pytest.raises(report.ReportError, match="ledger does not add up: hanji-ko-v1/dev/block/hanji"):
        report.write(_report([_row("hanji", 10, 8, {"outside_gt": 1})]), tmp_path, REGISTRY, tmp_path / "log")
    assert not (tmp_path / "report.json").exists()


def test_mask_exclusions_must_be_the_same_for_every_tool(tmp_path):
    ledger = [_row("hanji", 10, 9, {"excluded_ruby": 1}), _row("docling", 10, 8, {"excluded_ruby": 2})]
    with pytest.raises(report.ReportError, match="mask exclusions differ between tools"):
        report.write(_report(ledger), tmp_path, REGISTRY, tmp_path / "log")


def test_merging_reports_from_different_runs_is_refused():
    a, b = _report([]), _report([])
    b["run"] = b["run"] | {"name": "other"}
    with pytest.raises(report.ReportError, match="different runs"):
        report.merge(a, b)


def test_a_holdout_report_is_written_after_the_holdout_is_opened(tmp_path):
    log = tmp_path / "holdout-log.json"
    entry = holdout.admit(REGISTRY.get("hanji-ko-v1"), "holdout", "ev-d-public-claim", log, "b" * 40, None,
                          "2026-11-12")
    rep = _report([], "holdout")
    rep["holdout_opened"] = {"opened": True, "entry": entry}
    report.write(rep, tmp_path / "out", REGISTRY, log)
    assert json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))["holdout_opened"]["entry"] == 1


def test_the_cited_holdout_entry_must_match_the_dataset_split_and_engine_commit(tmp_path):
    log = tmp_path / "holdout-log.json"
    rep = _report([], "holdout")
    rep["holdout_opened"] = {"opened": True, "entry": 1}
    entry = {"entry": 1, "date": "2026-11-12", "dataset": "corpus-big", "split": "holdout", "engine_commit": "b" * 40,
             "table_version": None, "reason": "ev-d-public-claim"}
    for changed, message in [({}, "another dataset or split"),
                             ({"dataset": "hanji-ko-v1", "engine_commit": "c" * 40}, "another engine commit")]:
        log.write_text(json.dumps({"schema": "hanji-eval-holdout-log/1", "entries": [entry | changed]}),
                       encoding="utf-8")
        with pytest.raises(report.ReportError, match=message):
            report.write(rep, tmp_path / "out", REGISTRY, log)
    assert not (tmp_path / "out").exists()
