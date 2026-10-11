"""실행 모델 자원(onnxruntime·numpy를 import하지 않는 순수 계산): ORT 스레드 손잡이, 메모리·CPU 한도 알아내기(cgroup v2·v1,
물리 메모리), 크기 문자열, 문서 비용 추정과 입장(ingest_many의 부모가 쓴다)."""

import math
import os
import re
from pathlib import Path

from .errors import HanjiError

ORT_THREADS_ENV = "HANJI_ORT_THREADS"


def ort_threads_env() -> int | None:
    """환경변수 HANJI_ORT_THREADS(양의 정수). 없거나 비었으면 None(onnxruntime 기본값). 잘못된 값은 HanjiError(설정 오류,
    종료 코드 1). OCR·레이아웃 세션과 ingest_many가 같은 함수로 읽는다."""
    raw = os.environ.get(ORT_THREADS_ENV, "").strip()
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise HanjiError(f"{ORT_THREADS_ENV} must be a positive integer, got {raw!r}")
    return int(raw)


def set_ort_threads(options: object) -> None:
    """onnxruntime 세션 옵션에 스레드 수를 정한다: HANJI_ORT_THREADS가 있으면 intra_op n, inter_op 1. 없으면 그대로."""
    threads = ort_threads_env()
    if threads is not None:
        options.intra_op_num_threads = threads  # type: ignore[attr-defined]
        options.inter_op_num_threads = 1  # type: ignore[attr-defined]


# 메모리·CPU 한도(스펙 §8.5). 파일 시스템 뿌리를 인자로 받아 가짜 /proc·cgroup 파일로 테스트한다
GiB, MiB = 2**30, 2**20
BUDGET_SHARE = 0.8  # 기본 예산 = 알아낸 사용 가능 메모리 × 0.8(D5)
_UNITS = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
_SIZE = re.compile(r"(\d+(?:\.\d+)?)([KMGT]?)(?:I?B)?", re.IGNORECASE)


def parse_size(text: str) -> int:
    """크기 문자열 → 바이트: "8G", "512M", "1.5GiB", "4096"(바이트). K·M·G·T는 1024배수, B·iB를 붙여도 된다(대소문자
    무시). 단위 없는 소수, 1바이트 아래, 그 밖은 ValueError."""
    match = _SIZE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"invalid size {text!r} (use e.g. 8G, 512M, 1.5GiB or a byte count)")
    number, unit = match.group(1), match.group(2).upper()
    if not unit and "." in number:
        raise ValueError(f"invalid size {text!r} (a byte count must be a whole number)")
    value = int(float(number) * _UNITS[unit])
    if value < 1:
        raise ValueError(f"invalid size {text!r} (must be at least 1 byte)")
    return value


def format_size(value: int) -> str:
    """경고·요약 줄의 크기: 1GiB 이상은 GiB 소수 한 자리, 아래는 MiB 정수."""
    return f"{value / GiB:.1f}GiB" if value >= GiB else f"{value // MiB}MiB"


def physical_memory() -> int | None:
    """물리 메모리 바이트(sysconf: 리눅스·macOS). 알 수 없으면(Windows) None."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _number(path: Path) -> int | None:
    text = _read(path)
    return int(text) if text is not None and text.lstrip("-").isdigit() else None


def _cgroup_dirs(root: Path, version: int, controller: str) -> list[Path] | None:
    """자기 cgroup 폴더부터 그 계층의 마운트 지점까지(아래 → 위). version 2는 통합 계층("0::경로"와 cgroup2 마운트),
    1은 controller 계층("번호:…,controller,…:경로"와 그 옵션이 있는 cgroup 마운트). 마운트 뿌리는 경로에서 뺀다.
    /proc 파일이 없거나 계층을 찾지 못하면 None."""
    groups, mounts = _read(root / "proc/self/cgroup"), _read(root / "proc/self/mountinfo")
    if groups is None or mounts is None:
        return None
    own = None
    for line in groups.splitlines():
        number, _, rest = line.partition(":")
        names, _, path = rest.partition(":")
        if (version == 2 and number == "0" and names == "") or (version == 1 and controller in names.split(",")):
            own = path
            break
    if own is None:
        return None
    for line in mounts.splitlines():
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not sep or len(fields) < 5 or len(tail) < 3:
            continue
        if (version == 2 and tail[0] != "cgroup2") or (
                version == 1 and (tail[0] != "cgroup" or controller not in tail[2].split(","))):
            continue
        mount_root, point = fields[3], root / fields[4].lstrip("/")
        if mount_root == "/":
            rel = own
        elif own == mount_root or own.startswith(mount_root + "/"):
            rel = own[len(mount_root):]
        else:  # 자기 경로가 마운트 밖(다른 이름공간): 마운트 지점을 본다
            rel = "/"
        folder, dirs = point / rel.strip("/"), []
        while True:
            dirs.append(folder)
            if folder == point or point not in folder.parents:
                return dirs
            folder = folder.parent
    return None


def _cgroup_room(dirs: list[Path], limit_file: str, usage_file: str, physical: int | None) -> int | None:
    """폴더마다 min(한도, 물리) − 사용량의 최솟값(0 아래는 0). 한도 파일이 없거나 'max'인 폴더는 건너뛰고, 사용량
    파일이 없으면 0. 숫자 한도가 하나도 없으면 None."""
    best = None
    for folder in dirs:
        limit = _number(folder / limit_file)
        if limit is None:
            continue
        room = (limit if physical is None else min(limit, physical)) - (_number(folder / usage_file) or 0)
        best = room if best is None else min(best, room)
    return None if best is None else max(0, best)


def _available(root: Path, physical: int | None) -> int | None:
    candidates = []
    if _read(root / "proc/self/cgroup") is not None and _read(root / "proc/self/mountinfo") is not None:
        candidates += [(_cgroup_dirs(root, 2, "memory") or [], "memory.max", "memory.current"),
                       (_cgroup_dirs(root, 1, "memory") or [], "memory.limit_in_bytes", "memory.usage_in_bytes")]
    # /proc를 못 읽거나, 마운트·한도 파일을 찾지 못했으면 뿌리 파일(스펙 §8.5-3)
    candidates += [([root / "sys/fs/cgroup"], "memory.max", "memory.current"),
                   ([root / "sys/fs/cgroup/memory"], "memory.limit_in_bytes", "memory.usage_in_bytes")]
    for dirs, limit_file, usage_file in candidates:
        room = _cgroup_room(dirs, limit_file, usage_file, physical)
        if room is not None:
            return room
    return physical


def available_memory() -> int | None:
    """사용 가능 메모리 바이트(먼저 찾은 것): cgroup v2(자기 경로부터 마운트 뿌리까지), cgroup v1(memory 계층, 같다),
    /proc 정보를 읽을 수 없으면 뿌리 파일(/sys/fs/cgroup/memory.max·memory/memory.limit_in_bytes), cgroup 한도가 없으면
    물리 메모리(다른 프로세스 사용량은 빼지 않는다). 한도를 찾으면 늘 min(한도, 물리) − 사용량. 알 수 없으면 None."""
    return _available(Path("/"), physical_memory())


def _cpu_quota(dirs: list[Path], version: int) -> int | None:
    best = None
    for folder in dirs:
        if version == 2:
            quota, _, period = (_read(folder / "cpu.max") or "max").partition(" ")
            cores = math.ceil(int(quota) / int(period)) if quota.isdigit() and period.isdigit() else None
        else:
            quota_us, period_us = _number(folder / "cpu.cfs_quota_us"), _number(folder / "cpu.cfs_period_us")
            cores = math.ceil(quota_us / period_us) if quota_us and quota_us > 0 and period_us else None
        if cores is not None:
            best = cores if best is None else min(best, cores)
    return best


def _cores(root: Path, counts: list[int]) -> int:
    quotas = [_cpu_quota(dirs, version) for version in (2, 1)
              if (dirs := _cgroup_dirs(root, version, "cpu")) is not None]
    return max(1, min(counts + [q for q in quotas if q is not None]))


def _cpu_count(root: Path) -> int:
    counts = [os.cpu_count() or 1]
    if hasattr(os, "sched_getaffinity"):
        counts.append(len(os.sched_getaffinity(0)))
    return _cores(root, counts)


def cpu_count() -> int:
    """실효 코어 수(작업 수와 풀의 ORT 스레드 수가 쓴다): cgroup cpu.max(v2, ceil(quota/period), 'max'면 없음)·v1
    cpu.cfs_quota_us/cpu.cfs_period_us(-1이면 없음, 자기 경로부터 마운트 뿌리까지 가장 작은 값), sched_getaffinity(리눅스),
    os.cpu_count() 가운데 가장 작은 값."""
    return _cpu_count(Path("/"))


def budget(max_memory: int | None, limit: int | None) -> tuple[int | None, list[str]]:
    """메모리 예산과 경고. max_memory가 있으면 그 값(알아낸 한도보다 크면 경고하고 한도로 자른다, D10), 없으면
    floor(한도 × 0.8)(D5). 한도도 없으면 None(예산 없음)."""
    if max_memory is None:
        return (None if limit is None else math.floor(limit * BUDGET_SHARE)), []
    if limit is not None and max_memory > limit:
        return limit, [f"max memory {format_size(max_memory)} is above the available memory "
                       f"{format_size(limit)}; using {format_size(limit)}"]
    return max_memory, []
