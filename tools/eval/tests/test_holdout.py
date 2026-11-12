import json

import pytest

from hanji_eval import holdout
from hanji_eval.datasets import Registry

DATASET = Registry.from_text(
    '[[dataset]]\nname = "hanji-ko-v1"\nversion = "1"\nlicense = "MIT"\nlicense_basis = "LICENSE"\nuse = "x"\n'
    'publishable = true\nroles = ["selection"]\n'
    'splits = ["dev", "holdout"]\nsealed_splits = ["holdout"]\n[[dataset.files]]\npath = "f"\n'
    f'sha256 = "{"1" * 64}"\n').get("hanji-ko-v1")


def test_scoring_the_sealed_split_without_a_reason_is_refused(tmp_path):
    log = tmp_path / "holdout-log.json"
    with pytest.raises(holdout.HoldoutSealed, match="--open-holdout"):
        holdout.admit(DATASET, "holdout", None, log, "b" * 40, None, "2026-11-12")
    assert not log.exists()


def test_opening_the_holdout_leaves_a_numbered_record_each_time(tmp_path):
    log = tmp_path / "results" / "holdout-log.json"
    assert holdout.admit(DATASET, "holdout", "ev-d-public-claim", log, "b" * 40, 1, "2026-11-12") == 1
    assert holdout.admit(DATASET, "holdout", "ev-d-ledger-fix-rerun", log, "c" * 40, 1, "2026-11-13") == 2
    entries = json.loads(log.read_text(encoding="utf-8"))["entries"]
    assert [(e["entry"], e["split"], e["engine_commit"][0], e["reason"]) for e in entries] == [
        (1, "holdout", "b", "ev-d-public-claim"), (2, "holdout", "c", "ev-d-ledger-fix-rerun")]


def test_dev_scoring_needs_no_reason_and_writes_nothing(tmp_path):
    log = tmp_path / "holdout-log.json"
    assert holdout.admit(DATASET, "dev", None, log, "b" * 40, None, "2026-11-12") is None
    assert not log.exists()


def test_a_free_text_reason_is_refused(tmp_path):
    with pytest.raises(holdout.guard.GuardError):
        holdout.admit(DATASET, "holdout", "opening it for a look", tmp_path / "log.json", "b" * 40, None, "2026-11-12")


@pytest.mark.parametrize("split", ["all", "holdout2", "test"])
def test_a_split_the_dataset_does_not_have_is_refused(tmp_path, split):
    # 채점 입구는 닫힌 쪽: 데이터셋에 없는 분할 이름(개발·보류를 합친 all 등)은 사유가 있어도 없어도 거부한다
    log = tmp_path / "holdout-log.json"
    for reason in (None, "ev-d-public-claim"):
        with pytest.raises(holdout.RegistryError, match=f"unknown split '{split}'"):
            holdout.admit(DATASET, split, reason, log, "b" * 40, None, "2026-11-12")
    assert not log.exists()


def test_a_log_with_numbers_out_of_order_is_refused(tmp_path):
    log = tmp_path / "holdout-log.json"
    holdout.admit(DATASET, "holdout", "ev-d-public-claim", log, "b" * 40, None, "2026-11-12")
    data = json.loads(log.read_text(encoding="utf-8"))
    data["entries"].append(data["entries"][0] | {"entry": 3})
    log.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(holdout.guard.GuardError, match="entries must be numbered 1..n"):
        holdout.read_log(log)
    assert not list(tmp_path.glob("*.tmp"))
