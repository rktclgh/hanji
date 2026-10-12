"""여러 파일·폴더를 메모리 예산 안에서 병렬로 수집한다(문서 단위 spawn 프로세스 풀).

부모(IngestRun)는 경로를 펼치고, 문서를 모두 계획하고(sha256·쪽 수·추정 비용), 추정 비용이 큰 문서부터 예산 안에서
작업 프로세스에 보내고, 문서마다 작은 결과만 받는다. 작업 프로세스(_work)는 LocalEngine 수집 경로를 처음부터 끝까지 돌리고 자기
연결로 상태 파일에 커밋한다. 한 ProcessPoolExecutor가 사는 동안이 한 세대다. 부모는 onnxruntime을 import하지 않는다."""

import fnmatch
import hashlib
import os
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

from . import resources
from .engine import LocalEngine
from .errors import exit_code
from .formats.detect import default_parsers
from .formats.pdf.extract import PDFIUM_LOCK, pdfium
from .store.sqlite import SqliteStore

Status = Literal["created", "unchanged", "failed", "worker_crashed", "interrupted"]
READ_CHUNK = 1 << 20  # 부모가 해시할 때 한 번에 읽는 바이트(파일을 통째로 들지 않는다)
TEST_EXIT_ENV = "HANJI_TEST_WORKER_EXIT"  # 테스트 전용: "<파일 이름 패턴>:<init|hang|before_commit|after_commit>"


@dataclass(frozen=True, slots=True)
class IngestError:
    code: int  # parse와 같은 종료 코드 대응(3 형식, 4 파싱, 5 없음, 1 그 밖)
    type: str  # 예외 클래스 이름 또는 배치가 정한 이름(WorkerCrashed, Interrupted, ChangedDuringRun, …)
    message: str  # 비지 않는다


@dataclass(frozen=True, slots=True)
class IngestResult:
    index: int  # 펼친 입력 목록의 순번(0부터)
    path: str  # 사용자가 준 경로(폴더에서 고른 것은 폴더 인자 + 상대 경로)
    status: Status
    document_id: str | None
    version: int | None
    pages: int | None
    seconds: float
    error: IngestError | None


class Settings(NamedTuple):
    """작업 프로세스에 넘기는 설정(문자열·bool·정수·None뿐)."""

    db: str
    ocr: bool | None
    layout: bool | None
    force: bool
    threads: int | None  # 풀이 정한 ORT 스레드 수(사용자가 정한 HANJI_ORT_THREADS가 이긴다)


@dataclass(frozen=True, slots=True)
class _Entry:
    index: int
    path: str  # 열 경로(OS 문자열 그대로)
    shown: str  # 결과에 쓸 경로(UTF-8로 풀 수 없는 바이트는 \xNN)
    error: IngestError | None = None  # 순회 오류(부모 단계 실패)


@dataclass(slots=True)
class _Doc:
    entry: _Entry
    sha: str
    extra: int  # 추정 추가 몫(바이트)
    sent: float = 0.0  # 보낸 시각(time.monotonic)


def _text(text: str) -> str:
    """JSON·UTF-8로 쓸 수 있는 문자열(짝 없는 서로게이트는 \\udcNN)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _shown(path: str) -> str:
    return os.fsencode(path).decode("utf-8", "backslashreplace")


def _error(exc: BaseException) -> IngestError:
    return IngestError(exit_code(exc), type(exc).__name__, _text(str(exc) or type(exc).__name__))


def _folder(top: str, recursive: bool, extensions: frozenset[str]) -> tuple[list[tuple[str, IngestError | None]], int]:
    """폴더 하나에서 고른 (경로, 순회 오류)와 고르지 않은 파일 수. 지원 확장자만, 이름이 '.'으로 시작하는 파일·폴더는
    건너뛴다. 순서는 (상대 경로를 '/'로 이은 NFC, 그 원래 문자열)의 코드 포인트 순. 심볼릭 링크 폴더는 따라가지 않는다."""
    picked: list[tuple[tuple[str, str], str, IngestError | None]] = []
    skipped = 0

    def add(rel: str, error: IngestError | None = None) -> None:
        key = rel.replace(os.sep, "/")
        picked.append(((unicodedata.normalize("NFC", key), key), os.path.join(top, rel) if rel else top, error))

    def failed(exc: OSError) -> None:
        rel = os.path.relpath(exc.filename, top) if exc.filename else "."
        add("" if rel == "." else rel, _error(exc))

    def consider(rel: str) -> None:
        nonlocal skipped
        name = os.path.basename(rel)
        if name.startswith("."):
            return
        if os.path.splitext(name)[1].lower() in extensions:
            add(rel)
        else:
            skipped += 1

    if recursive:
        for folder, dirs, files in os.walk(top, onerror=failed, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if os.path.isfile(os.path.join(folder, name)):
                    consider(os.path.relpath(os.path.join(folder, name), top))
    else:
        try:
            with os.scandir(top) as found:
                names = [e.name for e in found if not e.name.startswith(".") and e.is_file()]
        except OSError as exc:
            failed(exc)
            names = []
        for name in names:
            consider(name)
    picked.sort(key=lambda item: item[0])
    return [(path, error) for _, path, error in picked], skipped


def expand(paths: Iterable[str | os.PathLike[str]], recursive: bool,
           extensions: frozenset[str]) -> tuple[list[_Entry], int]:
    """입력 경로 → 수집할 항목(인자 순서, 폴더 안은 정렬)과 고르지 않은 파일 수. 파일 인자는 확장자와 상관없이 넣는다.
    같은 실제 경로는 처음 것만. 없는 경로는 계획할 때 FileNotFoundError 결과가 된다."""
    found: list[tuple[str, IngestError | None]] = []
    skipped, seen = 0, set()
    for arg in paths:
        top = os.fspath(arg)
        if os.path.isdir(top):
            items, more = _folder(top, recursive, extensions)
            skipped += more
        else:
            items = [(top, None)]
        for path, error in items:
            if error is None:
                key = os.path.normcase(os.path.realpath(path))
                if key in seen:
                    continue
                seen.add(key)
            found.append((path, error))
    return [_Entry(i, path, _shown(path), error) for i, (path, error) in enumerate(found)], skipped


def _pdf_pages(path: str) -> int:
    """쪽 수(PDFIUM_LOCK 안에서 경로로 열어 센다). 열지 못하면(암호·손상) 1: 오류 문구는 작업 프로세스가 만든다."""
    try:
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(path)
            try:
                return max(1, len(document))
            finally:
                document.close()
    except Exception:
        return 1


def _plan(entry: _Entry, cost: resources.Cost) -> _Doc:
    """1MiB씩 읽어 sha256과 크기, PDF면 쪽 수로 추가 몫을 추정한다. 읽기 오류는 OSError 그대로."""
    digest, size = hashlib.sha256(), 0
    with open(entry.path, "rb") as file:
        while chunk := file.read(READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    pdf = os.path.splitext(entry.path)[1].lower() == ".pdf"
    return _Doc(entry, digest.hexdigest(), resources.estimate(cost, _pdf_pages(entry.path) if pdf else 0, size, pdf))


# 작업 프로세스 쪽(spawn 자식에서 돈다)
_ENGINE: LocalEngine | None = None


def _test_hook(name: str, stage: str) -> None:
    """테스트 전용(HANJI_TEST_WORKER_EXIT, 작업 프로세스에서만 읽는다). 파일 이름이 패턴(fnmatch, *는 모두)에 맞으면
    init: 초기화 예외, hang: 현재 폴더에 "<파일 이름>.started"를 만들고(테스트가 기다리는 시작 표시) 오래 잠든다,
    before_commit·after_commit: 수집 앞·뒤에서 os._exit(70)."""
    pattern, _, when = os.environ.get(TEST_EXIT_ENV, "").rpartition(":")
    if when != stage or not pattern or not fnmatch.fnmatchcase(name, pattern):
        return
    if stage == "init":
        raise RuntimeError("test hook: worker initialization failed")
    if stage == "hang":
        Path(f"{name}.started").touch()
        time.sleep(3600)
        return
    os._exit(70)


def _init(settings: Settings, name: str) -> LocalEngine:
    """첫 호출에서 게으르게: 풀이 정한 ORT 스레드 수(사용자 값이 없을 때만), 자기 상태 파일 연결, 기본 파서. 명시적
    True면 여기서 세션을 만든다(설치·모델 확인)."""
    _test_hook(name, "init")
    if settings.threads is not None and resources.ort_threads_env() is None:  # 없음·빈 값·공백뿐은 같은 "없음"
        os.environ[resources.ORT_THREADS_ENV] = str(settings.threads)
    store = SqliteStore(settings.db)
    try:
        return LocalEngine(store, default_parsers(ocr=settings.ocr, layout=settings.layout))
    except BaseException:
        store.close()
        raise


def _reply(start: float, status: str = "failed", error: IngestError | None = None, **fields: Any) -> dict[str, Any]:
    return {"status": status, "seconds": time.perf_counter() - start, "pid": os.getpid(),
            "error": None if error is None else (error.code, error.type, error.message), **fields}


def _work(settings: Settings, path: str, planned_sha: str) -> dict[str, Any]:
    """문서 하나: 바이트를 읽어 계획한 sha256과 같은지 보고, LocalEngine 수집 경로(파싱 → 트리 → 자산 확인 → diff →
    커밋)를 돈다. 예외는 여기서 잡아 작은 dict로 돌려준다(예외 객체는 프로세스 경계를 넘기지 않는다)."""
    global _ENGINE
    start = time.perf_counter()
    name = os.path.basename(path)
    if _ENGINE is None:
        try:
            _ENGINE = _init(settings, name)
        except Exception as exc:  # 이 프로세스는 다음 호출에서 다시 초기화를 시도한다
            message = _text(f"{type(exc).__name__}: {exc}")
            return _reply(start, error=IngestError(1, "WorkerInitFailed", message), init_failed=True)
    try:
        _test_hook(name, "hang")
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != planned_sha:
            return _reply(start, error=IngestError(1, "ChangedDuringRun",
                                                   "the file changed after it was planned; it was not ingested"))
        _test_hook(name, "before_commit")
        outcome = _ENGINE._ingest_outcome(data, name, force=settings.force)
        _test_hook(name, "after_commit")
    except Exception as exc:
        return _reply(start, error=_error(exc))
    return _reply(start, "created" if outcome.created else "unchanged", document_id=outcome.ref.document_id,
                  version=outcome.ref.version, pages=outcome.pages)
