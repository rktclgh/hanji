"""봉인한 보류 세트: --open-holdout <사유> 없이는 봉인 분할을 채점하지 않고, 열 때마다 holdout-log.json에
{차례, 날짜, 엔진 커밋, 보정 표 판, 사유}를 남긴다."""

import json
import os
import time
from pathlib import Path

from . import guard
from .datasets import Dataset, RegistryError

LOG_SCHEMA = guard.obj({
    "schema": guard.enum("hanji-eval-holdout-log/1"),
    "entries": guard.array(guard.obj({
        "entry": guard.integer(1), "date": guard.pattern(guard.DATE), "dataset": guard.enum(*guard.DATASETS),
        "split": guard.enum(*guard.SPLITS),
        "engine_commit": guard.pattern(guard.COMMIT), "table_version": guard.optional(guard.integer(1)),
        "reason": guard.pattern(guard.SLUG),
    })),
})


LOCK_TRIES = 200  # 기록 잠금(<log>.lock, O_EXCL)을 기다리는 횟수
LOCK_WAIT = 0.05  # 한 번 기다리는 초(모두 10초)


class HoldoutSealed(PermissionError):
    """봉인 분할을 --open-holdout 없이 채점하려 했다."""


class HoldoutLogBusy(RuntimeError):
    """다른 프로세스가 기록을 잡고 놓지 않는다(죽은 프로세스가 남긴 <log>.lock이면 손으로 지운다)."""


def _lock(log: Path) -> Path:
    """읽기–번호 매기기–쓰기를 프로세스 사이에서 하나씩 하도록 <log>.lock을 O_CREAT|O_EXCL로 만든다(fcntl 없이, 어느
    운영 체제에서나)."""
    lock = log.with_name(log.name + ".lock")
    for _ in range(LOCK_TRIES):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return lock
        except FileExistsError:
            time.sleep(LOCK_WAIT)
    raise HoldoutLogBusy(f"the holdout log is locked by another admission: {lock.name}")


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    guard.check(data, LOG_SCHEMA)
    if [e["entry"] for e in data["entries"]] != list(range(1, len(data["entries"]) + 1)):
        raise guard.GuardError("holdout log: entries must be numbered 1..n")
    return data["entries"]


def admit(dataset: Dataset, split: str, reason: str | None, log: Path, engine_commit: str,
          table_version: int | None, date: str) -> int | None:
    """채점 전에 부른다. 봉인되지 않은 분할이면 None. 봉인 분할이면 사유가 있어야 하고 기록을 남겨 그 차례를 돌려준다."""
    if split not in dataset.splits:  # 닫힌 쪽: 없는 분할 이름(all 등)으로 봉인을 비켜 가지 못한다
        raise RegistryError(f"{dataset.name}: unknown split {split!r}")
    if split not in dataset.sealed_splits:
        return None
    if reason is None:
        raise HoldoutSealed(f"{dataset.name}/{split} is sealed: pass --open-holdout <reason>")
    guard.pattern(guard.SLUG)(reason, "$.entries[].reason")  # 잠그기 전에 값부터 본다
    log.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock(log)
    try:
        entries = read_log(log)
        entry = {"entry": len(entries) + 1, "date": date, "dataset": dataset.name, "split": split,
                 "engine_commit": engine_commit, "table_version": table_version, "reason": reason}
        data = {"schema": "hanji-eval-holdout-log/1", "entries": [*entries, entry]}
        guard.check(data, LOG_SCHEMA)
        partial = log.with_name(log.name + ".tmp")  # 쓰다 끊겨도 기록이 반쯤 남지 않게 다 쓴 뒤 바꿔 넣는다
        partial.write_text(json.dumps(data, ensure_ascii=True, indent=1) + "\n", encoding="utf-8")
        partial.replace(log)
    finally:
        lock.unlink(missing_ok=True)
    return entry["entry"]
