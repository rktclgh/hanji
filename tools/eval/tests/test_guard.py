import copy

import pytest

from hanji_eval import guard

BAD_TEXT = ["가나다", "someone@example.com", "010-1234-5678", "This is an English sentence", "unknown_value"]


def _report() -> dict:
    env = guard.machine_env({"HANJI_ORT_THREADS": "4"}) | {"tools": [], "datasets": [], "split": None}
    return {
        "schema": "hanji-eval-report/1",
        "run": {"name": "2026-11-12-synthetic", "date": "2026-11-12", "harness_commit": "a" * 40,
                "engine_commit": "b" * 40,
                "rules": {"norm": "NORM_V1", "num": "NUM_V1", "adapter": None, "mask": "mask1", "truth_version": 1}},
        "env": env,
        "ledger": [{"dataset": "hanji-ko-v1", "split": "dev", "tool": "hanji", "unit": "block", "total": 10,
                    "scored": 8, "excluded": {"outside_gt": 2}}],
        "results": [{"dataset": "hanji-ko-v1", "split": "dev", "tool": "hanji", "tool_version": "0.1.0",
                     "config": "default", "metric": "nid", "agg": "doc_mean", "value": 0.97, "ci95": [0.95, 0.98],
                     "n": 10, "n_failed": 0, "flags": [], "sources": [{"dataset": "c" * 64, "split": "dev"}]}],
        "failures": [{"dataset": "hanji-ko-v1", "split": "dev", "tool": "hanji", "doc": "pdf-0001",
                      "status": "timeout"}],
        "holdout_opened": {"opened": False, "entry": None},
    }


def test_a_well_formed_report_passes():
    guard.check(_report(), guard.REPORT)


@pytest.mark.parametrize("bad", BAD_TEXT)
@pytest.mark.parametrize("field", [
    ("run", "name"), ("results", 0, "metric"), ("results", 0, "tool_version"), ("failures", 0, "doc"),
    ("failures", 0, "status"), ("env", "cpu_model"), ("ledger", 0, "dataset"),
])
def test_text_in_any_field_is_rejected(field, bad):
    report = _report()
    target = report
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = bad
    with pytest.raises(guard.GuardError):
        guard.check(report, guard.REPORT)


def test_a_free_text_key_in_a_counter_is_rejected():
    report = _report()
    report["ledger"][0]["excluded"] = {"가나다": 2}
    with pytest.raises(guard.GuardError, match=r"\$\.ledger\[0\]\.excluded<key>"):
        guard.check(report, guard.REPORT)


@pytest.mark.parametrize("change", ["extra_key", "missing_key", "string_number", "bool_number", "nan"])
def test_shape_and_number_rules(change):
    report = copy.deepcopy(_report())
    row = report["results"][0]
    if change == "extra_key":
        row["title"] = "x"
    elif change == "missing_key":
        del row["n"]
    elif change == "string_number":
        row["value"] = "0.97"
    elif change == "bool_number":
        row["n"] = True
    else:
        row["value"] = float("nan")
    with pytest.raises(guard.GuardError):
        guard.check(report, guard.REPORT)


@pytest.mark.parametrize("doc", ["pdf-\uff10\uff10\uff10\uff11", "pdf-\u0660\u0660\u0660\u0661", "hwpx-0001\n"])
def test_non_ascii_digits_and_infinite_numbers_are_rejected(doc):
    spec = guard.obj({"doc": guard.pattern(guard.DOC), "value": guard.number()})
    guard.check({"doc": "pdf-0001", "value": 1.0}, spec)
    with pytest.raises(guard.GuardError):
        guard.check({"doc": doc, "value": 1.0}, spec)
    for value in (float("inf"), float("-inf")):
        with pytest.raises(guard.GuardError):
            guard.check({"doc": "pdf-0001", "value": value}, spec)


def test_the_error_message_names_the_field_but_not_the_value():
    report = _report()
    report["failures"][0]["doc"] = "가나다 문서"
    with pytest.raises(guard.GuardError) as caught:
        guard.check(report, guard.REPORT)
    assert str(caught.value) == "$.failures[0].doc: does not match the field pattern"


@pytest.mark.parametrize(("cpu", "ok"), [
    ("Apple M3 Pro", True), ("Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz", True), ("AMD Ryzen 9 7950X", True),
    ("other", True), ("Some unknown processor", False),
])
def test_cpu_names_follow_a_fixed_shape(cpu, ok):
    if ok:
        guard.pattern(guard.CPU)(cpu, "$")
    else:
        with pytest.raises(guard.GuardError):
            guard.pattern(guard.CPU)(cpu, "$")


def test_emit_prints_only_checked_records(capsys):
    spec = guard.obj({"doc": guard.pattern(guard.DOC), "seconds": guard.number(0)})
    guard.emit({"doc": "hwpx-0001", "seconds": 1.5}, spec)
    with pytest.raises(guard.GuardError):
        guard.emit({"doc": "가나다", "seconds": 1.5}, spec)
    assert capsys.readouterr().out == '{"doc":"hwpx-0001","seconds":1.5}\n'
