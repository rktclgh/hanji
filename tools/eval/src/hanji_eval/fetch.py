"""데이터셋 받기: 작업 폴더(workdir) 아래 data/<이름>/의 새 빈 폴더에 받고, 파일마다 sha256이 표와 다르면 받은 것을
지우고 실패한다. 받은 파일은 경로로만 넘긴다(받은 데이터 안의 파이썬 파일을 import하지 않는다)."""

import contextlib
import hashlib
import urllib.request
from pathlib import Path

from .datasets import Dataset
from .workdir import REPO, refuse_lab

USER_AGENT = "hanji-eval"
TIMEOUT = 60


class FetchError(RuntimeError):
    """받기 실패(대상 폴더가 비어 있지 않음, 받을 주소 없음, sha256 불일치)."""


def _download(url: str, target: Path) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, target.open("xb") as out:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _remove(written: list[Path], made: list[Path]) -> None:
    for path in written:
        path.unlink(missing_ok=True)
    for folder in reversed(made):
        with contextlib.suppress(OSError):  # 지우기 실패가 원래 예외를 덮지 않게
            folder.rmdir()


def fetch(dataset: Dataset, dest_root: Path, repo: Path = REPO) -> Path:
    """데이터셋을 dest_root/<이름>/에 받는다. 그 폴더가 있고 비어 있지 않거나 링크면 받지 않는다. lab/ 아래로 풀리는
    대상은 CLI를 거치지 않아도 거부한다(WorkDirError)."""
    if any(f.url is None for f in dataset.files):
        raise FetchError(f"{dataset.name}: not a downloadable dataset (local files only)")
    if (dest_root / dataset.name).is_symlink():
        raise FetchError(f"{dataset.name}: destination is a link, not a new empty folder")
    target = refuse_lab(dest_root, repo) / dataset.name
    refuse_lab(target, repo)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FetchError(f"{dataset.name}: destination is not a new empty folder: {target}")
    made: list[Path] = []
    for folder in reversed([target, *target.parents]):
        if not folder.exists():
            folder.mkdir()
            made.append(folder)
    written: list[Path] = []
    bad: list[str] = []
    try:
        for f in dataset.files:
            path = target / f.path
            for folder in reversed(path.parents):
                if folder.is_relative_to(target) and not folder.exists():
                    folder.mkdir()
                    made.append(folder)
            written.append(path)
            if _download(f.url or "", path) != f.sha256:
                bad.append(f.path)
    except BaseException:
        _remove(written, made)
        raise
    if bad:
        _remove(written, made)
        raise FetchError(f"{dataset.name}: sha256 mismatch for {', '.join(bad)} (removed)")
    return target
