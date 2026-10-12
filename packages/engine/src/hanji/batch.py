"""여러 파일·폴더를 메모리 예산 안에서 병렬로 수집한다(문서 단위 spawn 프로세스 풀).

부모(IngestRun)는 경로를 펼치고, 문서를 모두 계획하고(sha256·쪽 수·추정 비용), 추정 비용이 큰 문서부터 예산 안에서
작업 프로세스에 보내고, 문서마다 작은 결과만 받는다. 작업 프로세스(_work)는 LocalEngine 수집 경로를 처음부터 끝까지 돌리고 자기
연결로 상태 파일에 커밋한다. 한 ProcessPoolExecutor가 사는 동안이 한 세대다. 부모는 onnxruntime을 import하지 않는다."""

import concurrent.futures as cf
import contextlib
import fnmatch
import hashlib
import multiprocessing
import os
import sys
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, NamedTuple

from . import resources
from .engine import LocalEngine
from .errors import LayoutUnavailable, OcrUnavailable, exit_code
from .formats.detect import default_parsers
from .formats.pdf import layout as layout_runtime
from .formats.pdf import ocr as ocr_runtime
from .formats.pdf.extract import PDFIUM_LOCK, pdfium
from .store.sqlite import SqliteStore, resolve_db

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


# 부모 쪽
TICK = 0.2  # 돌던 문서를 기다리는 동안 멈춤 표시를 보는 간격(초)
JOIN_TIMEOUT = 5.0  # 작업 프로세스를 terminate한 뒤 기다리는 초(지나면 kill)
MAX_FAILED_GENERATIONS = 3  # 연속으로 실패한 풀 세대가 이만큼이면 배치를 멈춘다
CRASHED = "worker process died; the document may or may not be stored"
INTERRUPTED = "interrupted before the result came back; the document may or may not be stored"


def _submission_order(docs: list[_Doc]) -> list[_Doc]:
    """보내는 순서(개정 1 판정 2, 개정 2): 같은 sha끼리 묶고 묶음 안은 입력 순서(같은 sha는 어차피 차례로 돈다),
    묶음은 첫 문서의 추정 추가 몫이 큰 것부터(가장 긴 일 먼저), 같으면 첫 문서의 입력 순번. 확장자가 달라 추정이
    다른 같은 바이트(first.md → second.pdf)도 차례 실행처럼 앞 파일이 먼저 돈다."""
    groups: dict[str, list[_Doc]] = {}
    for doc in docs:
        groups.setdefault(doc.sha, []).append(doc)
    ordered = sorted(groups.values(), key=lambda group: (-group[0].extra, group[0].entry.index))
    return [doc for group in ordered for doc in group]


class _Pool:
    """풀 세대 하나. limit건을 보내면 더 보내지 않는다(갈아 끼우기, 혼자 경로는 1)."""

    def __init__(self, workers: int, limit: int) -> None:
        self.executor = cf.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
        self.limit = limit
        self.sent = 0
        self.running: dict[cf.Future[dict[str, Any]], _Doc] = {}
        self.failed = ""  # 실패 이유(WorkerInitFailed 결과가 왔다). 비면 실패하지 않은 세대


class IngestRun(Iterator[IngestResult], contextlib.AbstractContextManager["IngestRun"]):
    """끝난 순서로 문서마다 결과 하나. with로 쓰거나 끝나면 close()를 부른다(break만 하면 풀은 close()나 GC까지 남는다).
    workers·ort_threads(None = onnxruntime 기본값)·budget(None = 예산 없음)·skipped·warnings·aborted·not_started는 요약용."""

    def __init__(self, entries: list[_Entry], skipped: int, settings: Settings, cost: resources.Cost,
                 budget: int | None, workers: int, ort_threads: int | None, warnings: list[str],
                 recycle_every: int, after_plan: Callable[[str], None] | None) -> None:
        self.workers, self.ort_threads, self.budget, self.skipped = workers, ort_threads, budget, skipped
        self.warnings = warnings
        self.aborted = False  # 풀 세대가 연속으로 실패해 멈췄다(CLI 종료 코드 7)
        self.not_started = 0  # 멈췄을 때 보내지 않은 문서 수
        self._entries, self._settings, self._cost = entries, settings, cost
        self._free = resources.room(budget, workers, cost)
        self._recycle_every, self._after_plan = recycle_every, after_plan
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed: tuple[IngestResult, ...] = ()
        self._pids: list[int] = []  # 테스트용: 결과를 낸 작업 프로세스(끝난 순서)
        self._gen: Iterator[IngestResult] | None = self._schedule()

    def __next__(self) -> IngestResult:
        with self._lock:
            if self._gen is None:
                raise StopIteration
            try:
                return next(self._gen)
            except BaseException:
                self._gen = None
                raise

    def close(self) -> tuple[IngestResult, ...]:
        """보내기를 멈추고 지금 세대를 끝낸다(이 실행의 작업 프로세스만). 받지 못한 문서의 결과(돌던 문서는 interrupted)를
        돌려준다. 끝까지 돈 뒤면 빈 튜플. 여러 번 불러도 같다. 다른 스레드가 기다리는 중이면 틱(0.2초) 안에 멈춘다."""
        self._stop.set()
        with self._lock:
            if self._gen is not None:
                self._gen.close()
                self._gen = None
        return self._closed

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _failed(self, entry: _Entry, error: IngestError) -> IngestResult:
        return IngestResult(entry.index, entry.shown, "failed", None, None, None, 0.0, error)

    def _lost(self, doc: _Doc, status: Status) -> IngestResult:
        kind, message = ("WorkerCrashed", CRASHED) if status == "worker_crashed" else ("Interrupted", INTERRUPTED)
        return IngestResult(doc.entry.index, doc.entry.shown, status, "doc_" + doc.sha[:24], None, None,
                            round(time.monotonic() - doc.sent, 2), IngestError(1, kind, message))

    def _received(self, doc: _Doc, reply: dict[str, Any]) -> IngestResult:
        self._pids.append(reply["pid"])
        error = None if reply["error"] is None else IngestError(*reply["error"])
        return IngestResult(doc.entry.index, doc.entry.shown, reply["status"], reply.get("document_id"),
                            reply.get("version"), reply.get("pages"), round(reply["seconds"], 2), error)

    def _end(self, pool: _Pool, status: Status) -> list[IngestResult]:
        """세대 끝내기: 그 세대의 작업 프로세스만 terminate → join(5) → 살아 있으면 kill. 결과가 이미 온 문서는 그 결과,
        아니면 status(interrupted·worker_crashed)."""
        processes = list((getattr(pool.executor, "_processes", None) or {}).values())  # shutdown이 None으로 바꾼다
        for process in processes:
            process.terminate()
        for process in processes:
            process.join(JOIN_TIMEOUT)
            if process.is_alive():
                process.kill()
                process.join()
        pool.executor.shutdown(wait=False, cancel_futures=True)
        results = []
        for future, doc in pool.running.items():
            if future.done() and not future.cancelled() and future.exception() is None:
                results.append(self._received(doc, future.result()))
            else:
                results.append(self._lost(doc, status))
        pool.running.clear()
        return results

    def _schedule(self) -> Iterator[IngestResult]:
        docs: list[_Doc] = []  # 계획한 문서(보내기 전에 모두 계획한다: 비용 내림차순으로 줄 세우려면 다 알아야 한다)
        planned = 0  # 계획 단계에서 지나간 항목 수
        queue: deque[_Doc] = deque()
        head: _Doc | None = None  # 줄의 맨 앞, 아직 보내지 않은 문서
        pool: _Pool | None = None
        failures, reason, finished = 0, "", False
        warned = False  # 혼자여도 예산을 넘는다는 경고를 냈다(배치에 한 번)

        def retire() -> None:  # 보통 닫기(돌던 문서가 없다). 실패한 세대면 연속 실패 수를 올리고 아니면 0으로
            nonlocal pool, failures, reason
            assert pool is not None and not pool.running
            pool.executor.shutdown(wait=True)
            failures, reason = (failures + 1, pool.failed) if pool.failed else (0, reason)
            pool = None

        try:
            for entry in self._entries:  # 계획: 순회 오류·읽기 오류는 여기서 바로 결과
                if self._stop.is_set():
                    return
                planned += 1
                if entry.error is not None:
                    yield self._failed(entry, entry.error)
                    continue
                try:
                    doc = _plan(entry, self._cost)
                except OSError as exc:
                    yield self._failed(entry, _error(exc))
                    continue
                if self._after_plan is not None:
                    self._after_plan(entry.path)
                docs.append(doc)
            queue.extend(_submission_order(docs))
            docs.clear()
            while not self._stop.is_set() and failures < MAX_FAILED_GENERATIONS:
                # 보내기: 줄의 맨 앞 문서가 들어갈 수 있는 동안(앞질러 가지 않는다)
                while not self._stop.is_set() and failures < MAX_FAILED_GENERATIONS:
                    if head is None:
                        if not queue:
                            break
                        head = queue.popleft()
                    if pool is not None and (pool.failed or pool.sent >= pool.limit):
                        if pool.running:
                            break  # 갈아 끼우기·실패 세대: 돌던 문서가 끝나기를 기다린 뒤 닫는다
                        retire()
                        continue
                    running = [(d.sha, d.extra) for d in pool.running.values()] if pool else []
                    decision = resources.admit(head.sha, head.extra, running, self.workers, self._free)
                    if decision == "wait":
                        break
                    if decision == "alone":  # 돌던 문서가 없다: 풀을 닫고 작업 1개짜리 세대에서 혼자 돈다
                        if pool is not None and self.workers > 1:
                            retire()
                            continue
                        if not warned and resources.over_budget(head.extra, self._cost, self.budget):
                            warned = True  # 배치에 한 번만(첫 문서)
                            total = resources.PARENT_RESERVE + self._cost.base + head.extra
                            self.warnings.append(
                                f"{head.entry.shown}: estimated memory {resources.format_size(total)} is above the "
                                f"budget {resources.format_size(self.budget or 0)} even alone; running it anyway")
                        if pool is None:  # 작업 1개면 이미 혼자다: 있던 풀을 그대로, 새 풀도 보통 갈아 끼우기 수로
                            pool = _Pool(1, 1) if self.workers > 1 else _Pool(1, self._recycle_every)
                    elif pool is None:
                        pool = _Pool(self.workers, self._recycle_every)
                    try:
                        future = pool.executor.submit(_work, self._settings, head.entry.path, head.sha)
                    except BrokenProcessPool:  # 세대가 깨졌다: 돌던 문서는 아래 기다리기에서, 맨 앞 문서는 다음 세대로
                        pool.failed = pool.failed or "worker process died"
                        break
                    head.sent = time.monotonic()
                    pool.running[future] = head
                    pool.sent += 1
                    head = None
                if failures >= MAX_FAILED_GENERATIONS or self._stop.is_set():
                    break
                if pool is None or not pool.running:
                    if head is None and not queue:
                        finished = True
                        return
                    continue
                # 기다리기: 틱마다 멈춤 표시(close·aclose·취소)를 본다
                done, _ = cf.wait(pool.running, timeout=TICK, return_when=cf.FIRST_COMPLETED)
                if self._stop.is_set():
                    return
                broken = False
                for future in done:
                    doc = pool.running.pop(future)
                    try:
                        reply = future.result()
                    except BrokenProcessPool:
                        broken = True
                        yield self._lost(doc, "worker_crashed")
                        continue
                    except Exception as exc:  # 결과를 받지 못한 그 밖의 오류
                        yield self._failed(doc.entry, _error(exc))
                        continue
                    if reply.get("init_failed"):
                        pool.failed = reply["error"][2]
                    yield self._received(doc, reply)
                if broken:  # 세대 끝내기: 받지 못한 문서는 worker_crashed
                    for result in self._end(pool, "worker_crashed"):
                        yield result
                    failures, reason, pool = failures + 1, "worker process died", None
            if failures >= MAX_FAILED_GENERATIONS:  # 보내지 않은 문서만 BatchAborted(계획 단계 오류는 이미 제 오류로 냈다)
                self.aborted = True
                error = IngestError(1, "BatchAborted", _text(
                    f"batch stopped after {failures} failed worker pools in a row ({reason}); not ingested"))
                unsent = ([head] if head is not None else []) + list(queue)
                head, queue = None, deque()
                for doc in unsent:
                    yield self._failed(doc.entry, error)
                finished = True
        finally:
            if pool is not None:
                if pool.running:
                    self._closed = tuple(self._end(pool, "interrupted"))
                else:
                    pool.executor.shutdown(wait=True)
            if not finished:
                self.not_started = (head is not None) + len(queue) + len(docs) + len(self._entries) - planned


def _use(flag: bool | None, runtime: Any, error: type[Exception]) -> bool:
    """켤 모델인가(비용 설정을 고르는 데만 쓴다). False면 끔, 자동(None)은 설치·모델 파일을 찾을 수 있으면 켬(import하지
    않는다), 명시적 True인데 찾을 수 없으면 시작 실패."""
    if flag is False:
        return False
    if runtime.available():
        return True
    if flag:
        raise error(runtime._missing())
    return False


def ingest_many(paths: Iterable[str | os.PathLike[str]], *, db: str | os.PathLike[str] | None = None,
                workers: int | None = None, max_memory: int | str | None = None, ocr: bool | None = None,
                layout: bool | None = None, force: bool = False, recursive: bool = False,
                _recycle_every: int | None = None, _after_plan: Callable[[str], None] | None = None) -> IngestRun:
    """여러 파일·폴더를 병렬로 수집한다. 결과는 끝난 순서(IngestResult.index가 입력 순번)이고 문서마다 저장 내용은
    같은 실효 ORT 스레드 수로 parse를 입력 순서대로 돌린 것과 같다. with로 쓰거나 끝나면 close()를 부른다.
    Windows·macOS 스크립트에서는 if __name__ == "__main__": 아래에서 부른다(spawn).

    시작 실패(문서를 보내기 전, 예외): 상태 파일을 열 수 없음·형식이 다름, HANJI_ORT_THREADS 값 오류, ocr·layout이
    True인데 설치·모델 파일이 없음. workers < 1·잘못된 max_memory는 ValueError."""
    if workers is not None and workers < 1:
        raise ValueError("workers must be >= 1")
    if isinstance(max_memory, str):
        max_memory = resources.parse_size(max_memory)
    if max_memory is not None and max_memory < 1:
        raise ValueError("max_memory must be at least 1 byte")
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise TypeError("paths must be an iterable of paths, not a single path")
    db_path = resolve_db(db)
    SqliteStore(db_path).close()  # 표·WAL·형식을 여기서 한 번 만들고 확인한다(작업 프로세스끼리 만들기 다툼이 없다)
    user_threads = resources.ort_threads_env()
    use_ocr = _use(ocr, ocr_runtime, OcrUnavailable)
    use_layout = _use(layout, layout_runtime, LayoutUnavailable)
    cost = resources.COSTS[resources.config_of(use_ocr, use_layout)]
    budget, warnings = resources.budget(max_memory, resources.available_memory())
    cores = resources.cpu_count()
    count, more = resources.plan_workers(budget, cost, cores, workers, sys.platform == "win32")
    threads = resources.pool_threads(count, cores, os.cpu_count() or 1)
    extensions = frozenset(e for parser in default_parsers(ocr=False, layout=False) for e in parser.extensions)
    entries, skipped = expand(paths, recursive, extensions)
    settings = Settings(os.fspath(db_path), ocr, layout, force, threads)
    return IngestRun(entries, skipped, settings, cost, budget, count, user_threads or threads, warnings + more,
                     _recycle_every or resources.RECYCLE_PER_WORKER * count, _after_plan)
