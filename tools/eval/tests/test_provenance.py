import copy
from pathlib import Path

import pytest

from hanji_eval import guard, provenance, report
from hanji_eval.datasets import Registry, RegistryError

DEV, DPB, INTERNAL = "1" * 64, "2" * 64, "3" * 64


def _entry(name: str, sha: str, roles: str, splits: str, sealed: str = "[]", publishable: str = "true") -> str:
    return (f'[[dataset]]\nname = "{name}"\nversion = "1"\nlicense = "MIT"\nlicense_basis = "LICENSE"\nuse = "x"\n'
            f'publishable = {publishable}\nroles = {roles}\n'
            f'splits = {splits}\nsealed_splits = {sealed}\n[[dataset.files]]\npath = "f-{name}"\nsha256 = "{sha}"\n')


TEXT = (_entry("hanji-ko-v1", DEV, '["selection", "calibration"]', '["dev", "holdout"]', '["holdout"]')
        + _entry("dp-bench", DPB, '["diagnostic"]', '["all"]')
        + _entry("layout-v0", INTERNAL, '["diagnostic"]', '["all"]', publishable="false"))
REGISTRY = Registry.from_text(TEXT)
DEV_SRC = REGISTRY.source("hanji-ko-v1", "dev")
HOLDOUT_SRC = REGISTRY.source("hanji-ko-v1", "holdout")
DPB_SRC = REGISTRY.source("dp-bench", "all")
INTERNAL_SRC = REGISTRY.source("layout-v0", "all")


def _report(source, dataset: str = "hanji-ko-v1") -> dict:
    env = guard.machine_env({}) | {"tools": [], "datasets": [], "split": None}
    return {
        "schema": "hanji-eval-report/1",
        "run": {"name": "synthetic", "date": "2026-11-12", "harness_commit": "a" * 40, "engine_commit": "b" * 40,
                "rules": {"norm": None, "num": None, "adapter": None, "mask": None, "truth_version": None}},
        "env": env, "ledger": [], "failures": [], "holdout_opened": {"opened": False, "entry": None},
        "results": [{"dataset": dataset, "split": source.split, "tool": "hanji", "tool_version": "0.1.0",
                     "config": "default", "metric": "nid", "agg": "doc_mean", "value": 0.9, "ci95": None, "n": 3,
                     "n_failed": 0, "flags": [], "sources": [{"dataset": source.dataset, "split": source.split}]}],
    }


def test_a_publishable_dev_report_is_written(tmp_path):
    report.write(_report(DEV_SRC), tmp_path / "out", REGISTRY, tmp_path / "holdout-log.json")
    assert (tmp_path / "out" / "report.json").exists() and (tmp_path / "out" / "report.md").exists()


@pytest.mark.parametrize("out", ["results/a", "elsewhere/b", "lab/c"])
def test_unpublishable_values_are_refused_whatever_the_output_path(tmp_path, out):
    with pytest.raises(provenance.ProvenanceError, match="layout-v0 is not publishable"):
        report.write(_report(INTERNAL_SRC, "layout-v0"), tmp_path / out, REGISTRY, tmp_path / "log.json")
    assert not (tmp_path / out).exists()


def test_unpublishable_values_merged_into_a_publishable_report_are_refused(tmp_path):
    merged = report.merge(_report(DEV_SRC), _report(INTERNAL_SRC, "layout-v0"))
    with pytest.raises(provenance.ProvenanceError, match="not publishable"):
        report.write(merged, tmp_path / "out", REGISTRY, tmp_path / "log.json")


def test_an_alias_cannot_be_registered_and_a_relabelled_result_is_still_caught(tmp_path):
    alias = _entry("dp-bench-copy", INTERNAL, '["diagnostic"]', '["all"]')
    with pytest.raises(RegistryError, match="alias"):
        Registry.from_text(TEXT + alias)
    relabelled = _report(INTERNAL_SRC, "hanji-ko-v1")
    with pytest.raises(provenance.ProvenanceError):
        report.write(relabelled, tmp_path / "out", REGISTRY, tmp_path / "log.json")
    relabelled["results"][0]["split"] = "dev"
    with pytest.raises(provenance.ProvenanceError, match="has other sources"):
        report.write(relabelled, tmp_path / "out", REGISTRY, tmp_path / "log.json")


def test_holdout_values_need_an_opened_holdout_entry(tmp_path):
    with pytest.raises(provenance.ProvenanceError, match="sealed"):
        report.write(_report(HOLDOUT_SRC), tmp_path / "out", REGISTRY, tmp_path / "log.json")
    opened = _report(HOLDOUT_SRC)
    opened["holdout_opened"] = {"opened": True, "entry": 1}
    with pytest.raises(report.ReportError, match="not in the holdout log"):
        report.write(opened, tmp_path / "out", REGISTRY, tmp_path / "log.json")


def test_unknown_identities_are_refused_at_every_exit():
    forged = provenance.Source("9" * 64, "dev")
    for check in (lambda s: provenance.check_report(s, REGISTRY, True), lambda s: provenance.check_calibration(
            s, REGISTRY), lambda s: provenance.check_selection(s, REGISTRY)):
        with pytest.raises(provenance.ProvenanceError, match="unknown dataset identity"):
            check([forged])


@pytest.mark.parametrize("bad", [DPB_SRC, HOLDOUT_SRC], ids=["dp-bench", "holdout"])
def test_calibration_refuses_dp_bench_and_holdout_labels_directly_merged_or_combined(bad):
    dev_labels = provenance.trace([1, 0, 1], DEV_SRC)
    provenance.check_calibration(dev_labels.sources, REGISTRY)
    bad_labels = provenance.trace([1, 1], bad)
    with pytest.raises(provenance.ProvenanceError):
        provenance.check_calibration(bad_labels.sources, REGISTRY)
    merged = provenance.combine(lambda a, b: a + b, dev_labels, bad_labels)
    assert merged.value == [1, 0, 1, 1, 1]
    with pytest.raises(provenance.ProvenanceError):
        provenance.check_calibration(merged.sources, REGISTRY)
    fitted = provenance.combine(lambda labels: sum(labels) / len(labels), merged)
    with pytest.raises(provenance.ProvenanceError):
        provenance.check_calibration(fitted.sources, REGISTRY)


@pytest.mark.parametrize("bad", [DPB_SRC, HOLDOUT_SRC], ids=["dp-bench", "holdout"])
def test_selection_gates_refuse_dp_bench_and_holdout_values_directly_or_merged(bad):
    dev_rows = [_report(DEV_SRC)["results"][0]]
    provenance.check_selection(provenance.sources_of(dev_rows), REGISTRY)
    bad_rows = copy.deepcopy(dev_rows)
    bad_rows[0]["sources"] = [{"dataset": bad.dataset, "split": bad.split}]
    for rows in (bad_rows, dev_rows + bad_rows):
        with pytest.raises(provenance.ProvenanceError):
            provenance.check_selection(provenance.sources_of(rows), REGISTRY)


def test_an_empty_input_is_refused_by_calibration_and_selection():
    with pytest.raises(provenance.ProvenanceError, match="no sources"):
        provenance.check_calibration([], REGISTRY)
    with pytest.raises(provenance.ProvenanceError, match="no sources"):
        provenance.check_selection([], REGISTRY)


def test_the_holdout_log_path_does_not_matter_for_refusal(tmp_path):
    for log in (tmp_path / "a.json", Path(tmp_path / "b" / "c.json")):
        with pytest.raises(provenance.ProvenanceError):
            report.write(_report(HOLDOUT_SRC), tmp_path / "out", REGISTRY, log)


def test_a_split_the_dataset_does_not_have_is_refused_at_every_exit():
    combined = provenance.Source(DEV_SRC.dataset, "all")  # 개발·보류를 합쳐 잰 값
    for check in (lambda s: provenance.check_report(s, REGISTRY, False), lambda s: provenance.check_calibration(
            s, REGISTRY), lambda s: provenance.check_selection(s, REGISTRY)):
        with pytest.raises(provenance.ProvenanceError, match="has no split 'all'"):
            check([combined])


def test_a_row_labelled_holdout_with_dev_sources_is_refused(tmp_path):
    mislabelled = _report(DEV_SRC)
    mislabelled["results"][0]["split"] = "holdout"
    with pytest.raises(provenance.ProvenanceError, match="has other sources"):
        report.write(mislabelled, tmp_path / "out", REGISTRY, tmp_path / "log.json")
    assert not (tmp_path / "out").exists()


def test_ledger_rows_of_the_sealed_split_need_an_opened_holdout(tmp_path):
    rep = _report(DEV_SRC)
    rep["ledger"] = [{"dataset": "hanji-ko-v1", "split": "holdout", "tool": "hanji", "unit": "doc", "total": 3,
                      "scored": 3, "excluded": {}}]
    with pytest.raises(provenance.ProvenanceError, match="sealed"):
        report.write(rep, tmp_path / "out", REGISTRY, tmp_path / "log.json")


def test_failure_rows_of_the_sealed_split_need_an_opened_holdout(tmp_path):
    rep = _report(DEV_SRC)
    rep["failures"] = [{"dataset": "hanji-ko-v1", "split": "holdout", "tool": "hanji", "doc": "pdf-0001",
                        "status": "timeout"}]
    with pytest.raises(provenance.ProvenanceError, match="sealed"):
        report.write(rep, tmp_path / "out", REGISTRY, tmp_path / "log.json")
