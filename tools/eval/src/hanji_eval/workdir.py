"""작업 폴더: 문서 글자가 들 수 있는 작업 파일(받은 데이터, 대규모 실행의 문서별 기록 등)은 저장소와 lab/ 밖의 이
폴더에 둔다(컨트롤러 판정 R1). HANJI_EVAL_WORK가 있으면 그것, 없으면 사용자 캐시의 hanji-eval/work. lab/ 아래로
풀리는 출력 경로는 거부한다(lab은 읽기 전용)."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from platformdirs import user_cache_dir

ENV = "HANJI_EVAL_WORK"
REPO = Path(__file__).resolve().parents[4]  # tools/eval/src/hanji_eval/workdir.py → 저장소 뿌리


class WorkDirError(ValueError):
    """출력 경로가 lab/ 아래다."""


def work_dir(environ: Mapping[str, str] | None = None) -> Path:
    value = (os.environ if environ is None else environ).get(ENV, "").strip()
    return Path(value).expanduser() if value else Path(user_cache_dir("hanji-eval", appauthor=False)) / "work"


def lab_roots(repo: Path) -> list[Path]:
    """이 저장소의 lab/과, 워크트리라면 본 체크아웃의 lab/."""
    roots = [repo / "lab"]
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                              capture_output=True, text=True, encoding="utf-8", check=True)
        roots.append(Path(done.stdout.strip()).parent / "lab")
    except (OSError, subprocess.CalledProcessError):
        pass
    return roots


def _same(a: Path, b: Path) -> bool:
    # 대소문자를 가리지 않는 파일 시스템(macOS APFS)에서는 LAB/이 lab/이다: 있는 경로는 samefile, 없으면 casefold로 본다
    if str(a).casefold() == str(b).casefold():
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def refuse_lab(path: Path, repo: Path) -> Path:
    """path를 풀어(심볼릭 링크 포함) lab/ 아래면 WorkDirError, 아니면 푼 경로를 돌려준다. 아직 없는 경로는 있는
    조상까지 따라가 보고, 대소문자만 다른 철자도 lab/으로 본다."""
    resolved = path.expanduser().resolve()
    labs = [lab.resolve() for lab in lab_roots(repo)]
    for ancestor in (resolved, *resolved.parents):
        if any(_same(ancestor, lab) for lab in labs):
            raise WorkDirError("output path must not be under lab/ (lab is read-only)")
    return resolved
