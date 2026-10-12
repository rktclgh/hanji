"""여러 문서 병렬 수집(hanji.batch). 풀을 띄우는 테스트는 시나리오 스크립트를 자기 프로세스 그룹의 자식 Python에서
돌린다(scenario): spawn 자식은 테스트 모듈을 import할 수 없고, 멈추거나 실패하면 그룹째(작업 프로세스까지) 끝낸다.
경로 펼치기·계획·작업 함수는 같은 프로세스에서 본다. 글자는 모두 지어낸 합성 문장이다."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import unicodedata
from pathlib import Path

import pytest

from hanji import batch, resources
from hanji.formats.detect import default_parsers

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXTENSIONS = frozenset(e for p in default_parsers(ocr=False, layout=False) for e in p.extensions)


def write(path: Path, text: str = "# 지어낸 제목\n\n지어낸 문단\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


STOP_GRACE = 5.0  # 그룹을 끝낼 때 SIGTERM 뒤 SIGKILL까지 기다리는 초


def _signal_group(pgid: int, sig: int) -> bool:
    """프로세스 그룹에 신호를 보낸다(POSIX). 그룹이 이미 없으면 False."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):  # macOS는 좀비만 남은 그룹에 EPERM을 줄 수 있다
        return False
    return True


def _end_group(child: subprocess.Popen) -> None:
    """시나리오 프로세스와 그 그룹(작업 프로세스 포함)을 끝내고 거둔다. POSIX: SIGTERM → 그룹이 빌 때까지 최대
    STOP_GRACE초 → SIGKILL → 거두기. Windows: taskkill /F /T → 거두기."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
        child.wait()
        return
    deadline = time.monotonic() + STOP_GRACE
    _signal_group(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=STOP_GRACE)  # 우두머리를 먼저 거둬야 좀비가 그룹을 살아 있게 보이지 않는다
    except subprocess.TimeoutExpired:
        pass
    while time.monotonic() < deadline and _signal_group(child.pid, 0):
        time.sleep(0.05)
    _signal_group(child.pid, signal.SIGKILL)
    child.wait()


def scenario(tmp_path: Path, source: str, *args: object, timeout: float = 180, env: dict[str, str] | None = None):
    """시나리오 스크립트를 자기 프로세스 그룹의 자식 Python으로 돌리고 마지막 줄 JSON을 돌려준다(작업 함수는 스크립트
    안에 둔다). 시간을 넘기거나 실패하면 그룹째 끝내고 거둔다(남은 작업 프로세스가 다음 테스트로 새지 않게)."""
    script = tmp_path / "scenario.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    group: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                   else {"start_new_session": True})
    child = subprocess.Popen([sys.executable, str(script), *map(str, args)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=tmp_path,
                             env={**os.environ, **(env or {})}, **group)
    try:
        try:
            out, err = child.communicate(timeout=timeout)
        except BaseException as exc:  # 시간 초과·Ctrl+C: 그룹을 끝내고 남은 출력을 붙여 다시 던진다
            finished = child.poll()  # 스크립트는 끝났는데 남은 작업 프로세스가 파이프를 쥐어 시간을 넘겼을 수 있다
            _end_group(child)
            try:
                out, err = child.communicate(timeout=STOP_GRACE)
            except subprocess.TimeoutExpired:  # 그룹 밖으로 샌 손주가 파이프를 쥐고 있다
                out, err = "", ""
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            assert finished in (None, 0), err  # 실패한 스크립트는 시간 초과가 아니라 실패로 보인다
            raise subprocess.TimeoutExpired(child.args, timeout, output=out, stderr=err) from None
        if child.returncode != 0:
            _end_group(child)
        assert child.returncode == 0, err
    finally:
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
    return json.loads(out.strip().splitlines()[-1])


def test_folder_picks_supported_extensions_and_skips_hidden_names(tmp_path):
    """폴더에서는 지원 확장자만(대소문자 무시), 이름이 '.'으로 시작하는 파일·폴더는 건너뛴다. --recursive가 없으면 바로
    아래만. 고르지 않은 파일(지원하지 않는 확장자) 수를 센다. 파일 인자는 확장자와 상관없이 넣는다."""
    for name in ("a.md", "B.PDF", "c.Markdown", "d.txt", ".e.md", ".hidden/f.md", "sub/g.md"):
        write(tmp_path / "in" / name)
    write(tmp_path / "x.txt")
    entries, skipped = batch.expand([tmp_path / "in", tmp_path / "x.txt"], False, EXTENSIONS)
    folder = os.path.join(str(tmp_path / "in"), "")
    assert [e.shown for e in entries] == [folder + "B.PDF", folder + "a.md", folder + "c.Markdown",
                                          str(tmp_path / "x.txt")]
    assert [e.index for e in entries] == [0, 1, 2, 3] and skipped == 1
    entries, _ = batch.expand([tmp_path / "in"], True, EXTENSIONS)
    assert [os.path.relpath(e.path, tmp_path / "in") for e in entries] == [
        "B.PDF", "a.md", "c.Markdown", os.path.join("sub", "g.md")]


def test_order_joins_relative_paths_with_slash_and_duplicates_come_once(tmp_path):
    """폴더 안 순서는 '/'로 이은 상대 경로(NFC)의 코드 포인트 순이다('a-.md' < 'a/z.md' < 'b.md'). 같은 실제 경로는
    처음 것만 넣는다."""
    for name in ("b.md", "a-.md", "a/z.md"):
        write(tmp_path / "in" / name)
    entries, _ = batch.expand([tmp_path / "in" / "b.md", tmp_path / "in", tmp_path / "in"], True, EXTENSIONS)
    assert [os.path.relpath(e.path, tmp_path / "in") for e in entries] == ["b.md", "a-.md", os.path.join("a", "z.md")]


@pytest.mark.skipif(sys.platform != "linux", reason="NFC·NFD로 다른 두 이름은 리눅스 파일 시스템만 만들 수 있다")
def test_names_equal_after_nfc_keep_a_stable_second_key(tmp_path):
    nfc, nfd = unicodedata.normalize("NFC", "가.md"), unicodedata.normalize("NFD", "가.md")
    write(tmp_path / "in" / nfd)
    write(tmp_path / "in" / nfc)
    entries, _ = batch.expand([tmp_path / "in"], False, EXTENSIONS)
    assert [os.path.basename(e.path) for e in entries] == sorted([nfc, nfd])


def test_recursive_does_not_follow_symlinked_folders_but_takes_symlinked_files(tmp_path):
    write(tmp_path / "other" / "o.md")
    write(tmp_path / "in" / "a.md")
    try:
        os.symlink(tmp_path / "other", tmp_path / "in" / "link", target_is_directory=True)
        os.symlink(tmp_path / "other" / "o.md", tmp_path / "in" / "file.md")
    except OSError:
        pytest.skip("cannot create symlinks here")
    entries, _ = batch.expand([tmp_path / "in"], True, EXTENSIONS)
    assert [os.path.basename(e.path) for e in entries] == ["a.md", "file.md"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX 권한(root가 아닐 때)")
def test_unreadable_subfolder_is_a_failed_entry_in_its_place(tmp_path):
    """순회 오류는 무시하지 않고 그 경로의 실패 항목이 된다(정렬된 자리의 순번)."""
    write(tmp_path / "in" / "a.md")
    write(tmp_path / "in" / "c.md")
    write(tmp_path / "in" / "b" / "x.md")
    (tmp_path / "in" / "b").chmod(0)
    try:
        entries, _ = batch.expand([tmp_path / "in"], True, EXTENSIONS)
    finally:
        (tmp_path / "in" / "b").chmod(0o755)
    assert [(os.path.relpath(e.path, tmp_path / "in"), e.error and e.error.type) for e in entries] == [
        ("a.md", None), ("b", "PermissionError"), ("c.md", None)]
    assert entries[1].error.code == 1 and entries[1].error.message


@pytest.mark.skipif(sys.platform != "linux", reason="UTF-8이 아닌 파일 이름은 리눅스만 만들 수 있다")
def test_file_names_that_are_not_utf8_are_shown_with_escapes(tmp_path):
    folder = os.fsencode(tmp_path / "in")
    os.makedirs(folder)
    with open(os.path.join(folder, b"\xff.md"), "wb") as f:
        f.write("# 지어낸 제목\n".encode())
    entries, _ = batch.expand([tmp_path / "in"], False, EXTENSIONS)
    assert entries[0].shown == os.path.join(str(tmp_path / "in"), "\\xff.md")
    json.dumps(entries[0].shown, ensure_ascii=False).encode("utf-8")


def test_plan_hashes_the_file_and_estimates_its_extra_memory(tmp_path):
    """계획은 sha256과 추가 몫(쪽 몫 × 쪽 수 + 바이트 배수 × 크기)이다. 열지 못하는 PDF는 1쪽으로 보고 보낸다."""
    cost = resources.Cost(base=1000, per_page=10)
    pdf = FIXTURES / "pdf" / "inputs" / "report.pdf"
    md = write(tmp_path / "a.md")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    plans = [batch._plan(batch._Entry(0, str(p), str(p)), cost) for p in (md, pdf, broken)]
    assert [d.sha for d in plans] == [hashlib.sha256(p.read_bytes()).hexdigest() for p in (md, pdf, broken)]
    by = resources.BYTES_FACTOR
    assert [d.extra for d in plans] == [by["other"] * md.stat().st_size, 2 * 10 + by["pdf"] * pdf.stat().st_size,
                                        10 + by["pdf"] * broken.stat().st_size]


@pytest.fixture
def work(tmp_path, monkeypatch):
    """같은 프로세스에서 작업 함수를 부른다(작업 프로세스 상태를 테스트마다 비운다)."""
    monkeypatch.setattr(batch, "_ENGINE", None)
    monkeypatch.delenv(batch.TEST_EXIT_ENV, raising=False)
    settings = batch.Settings(str(tmp_path / "state.db"), False, False, False, None)

    def call(path: Path, sha: str | None = None) -> dict:
        reply = batch._work(settings, str(path), sha or hashlib.sha256(path.read_bytes()).hexdigest())
        assert reply.pop("pid") == os.getpid() and reply.pop("seconds") >= 0
        return reply

    yield call
    if batch._ENGINE is not None:
        batch._ENGINE._store.close()


def test_work_ingests_and_reports_created_then_unchanged_with_pages(tmp_path, work):
    md, pdf = write(tmp_path / "a.md"), shutil.copy(FIXTURES / "pdf" / "inputs" / "report.pdf", tmp_path)
    first = work(md)
    assert first == {"status": "created", "error": None, "document_id": first["document_id"], "version": 1,
                     "pages": None}
    assert work(md) == {**first, "status": "unchanged"}
    assert work(Path(pdf))["pages"] == 2


def test_work_turns_every_exception_into_a_small_error(tmp_path, work, monkeypatch):
    """예외는 작업 함수에서 (코드, 클래스 이름, 문구)가 된다: 지원하지 않는 형식 3, 파싱 실패 4, 계획과 다른 바이트는
    ChangedDuringRun(파싱하지 않음), 빈 문구는 클래스 이름."""
    other = write(tmp_path / "a.txt")
    assert work(other)["error"] == (3, "UnsupportedFormat", "unsupported format: a.txt")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    assert work(broken)["error"][:2] == (4, "ParseError")
    md = write(tmp_path / "b.md")
    assert work(md, sha="0" * 64) == {"status": "failed", "error": (
        1, "ChangedDuringRun", "the file changed after it was planned; it was not ingested")}

    def out_of_memory(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(batch._ENGINE, "_ingest_outcome", out_of_memory)
    assert work(md)["error"] == (1, "MemoryError", "MemoryError")


def test_worker_init_failure_is_a_result_and_the_next_call_tries_again(tmp_path, work, monkeypatch):
    monkeypatch.setenv(batch.TEST_EXIT_ENV, "bad*.md:init")
    bad, good = write(tmp_path / "bad1.md"), write(tmp_path / "good.md")
    assert work(bad) == {"status": "failed", "init_failed": True, "error": (
        1, "WorkerInitFailed", "RuntimeError: test hook: worker initialization failed")}
    assert work(good)["status"] == "created"


@pytest.mark.parametrize(("user", "expected", "threads"), [(None, "2", 2), ("5", "5", 5), ("", "2", 2), ("  ", "2", 2)])
def test_worker_init_leaves_onnxruntime_alone_and_user_threads_win(tmp_path, user, expected, threads):
    """작업 프로세스 초기화 직후(자동 모드, 글자 문서) onnxruntime을 import하지 않고, 원격 측정은 꺼져 있다. 풀이 정한
    스레드 수는 사용자가 정한 HANJI_ORT_THREADS가 없을 때만 들어간다. 빈 값·공백뿐은 없는 것과 같아(ort_threads_env)
    풀 값이 들어가고, 세션이 부르는 set_ort_threads도 그 값을 쓴다(가짜 세션 옵션)."""
    write(tmp_path / "a.md")
    env = {k: v for k, v in os.environ.items() if k not in ("ORT_DISABLE_TELEMETRY", "HANJI_ORT_THREADS")}
    if user is not None:
        env["HANJI_ORT_THREADS"] = user
    code = textwrap.dedent("""
        import hashlib, json, os, sys, types
        from hanji import batch, resources
        data = open("a.md", "rb").read()
        reply = batch._work(batch.Settings("state.db", None, None, False, 2), "a.md", hashlib.sha256(data).hexdigest())
        options = types.SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
        resources.set_ort_threads(options)  # OCR·레이아웃 세션이 옵션에 부르는 함수
        print(json.dumps([reply["status"], "onnxruntime" in sys.modules, os.environ.get("ORT_DISABLE_TELEMETRY"),
                          os.environ.get("HANJI_ORT_THREADS"), options.intra_op_num_threads]))
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                          timeout=120, cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["created", False, "1", expected, threads]
