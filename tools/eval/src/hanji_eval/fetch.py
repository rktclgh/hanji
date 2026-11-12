"""데이터셋 받기: 작업 폴더(workdir) 아래 data/<이름>/의 새 빈 폴더에 받고, 파일마다 sha256이 표와 다르면 받은 것을
지우고 실패한다. 받은 파일은 경로로만 넘긴다(받은 데이터 안의 파이썬 파일을 import하지 않는다)."""

import contextlib
import hashlib
import os
import time
import urllib.request
from pathlib import Path

from .datasets import Dataset, FileEntry
from .workdir import REPO, refuse_lab

USER_AGENT = "hanji-eval"
TIMEOUT = 60  # 소켓 한 번을 기다리는 초
MAX_BYTES = 2 * 1024 ** 3  # 파일 하나의 바이트 상한(datasets.toml에 size가 없을 때)
DEADLINE = 30 * 60  # 파일 하나를 받는 데 쓰는 전체 초
_clock = time.monotonic


class FetchError(RuntimeError):
    """받기 실패(대상 폴더가 비어 있지 않음·다른 받기가 쓰는 중, 받을 주소 없음, 상한 넘음, sha256 불일치)."""


def _download(f: FileEntry, target: Path, written: list[Path]) -> str:
    """target을 새로(xb) 만들어 받는다. 만든 뒤에만 written에 넣는다(남이 만든 파일은 지우지 않는다). 바이트가 size
    (없으면 MAX_BYTES)를 넘거나 DEADLINE이 지나면 FetchError(지우기는 부른 쪽이 한다)."""
    limit = f.size if f.size is not None else MAX_BYTES
    request = urllib.request.Request(f.url or "", headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total, start = 0, _clock()
    with target.open("xb") as out:
        written.append(target)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                total += len(chunk)
                if total > limit:
                    raise FetchError(f"{f.path}: more than {limit} bytes (removed)")
                if _clock() - start > DEADLINE:
                    raise FetchError(f"{f.path}: past the {DEADLINE} s deadline (removed)")
                digest.update(chunk)
                out.write(chunk)
    return digest.hexdigest()


def _remove(written: list[Path], made: list[Path]) -> None:
    for path in written:
        path.unlink(missing_ok=True)
    for folder in reversed(made):
        with contextlib.suppress(OSError):  # 지우기 실패가 원래 예외를 덮지 않게
            folder.rmdir()


def _not_empty(target: Path) -> bool:
    return target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir())))


def fetch(dataset: Dataset, dest_root: Path, repo: Path = REPO) -> Path:
    """데이터셋을 dest_root/<이름>/에 받는다. 그 폴더가 있고 비어 있지 않거나 링크면 받지 않는다. lab/ 아래로 풀리는
    대상은 CLI를 거치지 않아도 거부한다(WorkDirError). 쓰기 전에 dest_root/.<이름>.fetch-lock을 O_EXCL로 만들어
    대상을 잡는다(같은 곳에 동시에 받으면 뒤의 것이 FetchError). 실패하면 이 받기가 만든 파일·폴더만 지운다."""
    if any(f.url is None for f in dataset.files):
        raise FetchError(f"{dataset.name}: not a downloadable dataset (local files only)")
    if (dest_root / dataset.name).is_symlink():
        raise FetchError(f"{dataset.name}: destination is a link, not a new empty folder")
    target = refuse_lab(dest_root, repo) / dataset.name
    refuse_lab(target, repo)
    if _not_empty(target):
        raise FetchError(f"{dataset.name}: destination is not a new empty folder: {target}")
    made: list[Path] = []
    for folder in reversed([target.parent, *target.parent.parents]):
        if not folder.exists():
            folder.mkdir()
            made.append(folder)
    lock = target.parent / f".{dataset.name}.fetch-lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        _remove([], made)
        raise FetchError(f"{dataset.name}: another fetch is writing this destination") from None
    written: list[Path] = []
    try:
        if _not_empty(target):  # 잠그기 전에 끝난 다른 받기
            raise FetchError(f"{dataset.name}: destination is not a new empty folder: {target}")
        if not target.exists():
            target.mkdir()
            made.append(target)
        bad: list[str] = []
        for f in dataset.files:
            path = target / f.path
            for folder in reversed(path.parents):
                if folder.is_relative_to(target) and not folder.exists():
                    folder.mkdir()
                    made.append(folder)
            if _download(f, path, written) != f.sha256:
                bad.append(f.path)
        if bad:
            raise FetchError(f"{dataset.name}: sha256 mismatch for {', '.join(bad)} (removed)")
    except BaseException:
        lock.unlink(missing_ok=True)
        _remove(written, made)
        raise
    lock.unlink(missing_ok=True)
    return target
