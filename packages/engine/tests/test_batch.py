"""여러 문서 병렬 수집(hanji.batch). 풀을 띄우는 테스트는 시나리오 스크립트를 자기 프로세스 그룹의 자식 Python에서
돌린다(scenario): spawn 자식은 테스트 모듈을 import할 수 없고, 멈추거나 실패하면 그룹째(작업 프로세스까지) 끝낸다.
경로 펼치기·계획·작업 함수는 같은 프로세스에서 본다. 글자는 모두 지어낸 합성 문장이다."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import unicodedata
from pathlib import Path

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hanji import batch, resources
from hanji.formats.detect import default_parsers
from hanji.formats.pdf import layout as layout_runtime
from hanji.formats.pdf import ocr as ocr_runtime

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


DUMP = r'''
def dump(db):
    """문서별 저장 내용(표 바이트)과 변경 로그 다중집합."""
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        tables = [conn.execute(f"SELECT * FROM {t} ORDER BY {k}").fetchall() for t, k in (
            ("documents", "document_id"), ("versions", "document_id, version"),
            ("histories", "document_id, version"), ("assets", "sha"))]
        log = sorted(conn.execute("SELECT document_id, version, change_json FROM change_log").fetchall())
    finally:
        conn.close()
    return tables, log
'''


def test_pool_results_match_parse_in_input_order_byte_for_byte(tmp_path):
    """같은 입력·설정·실효 ORT 스레드 수면 ingest_many(작업 3개, 그리고 작업 1개 + 2건마다 풀 새로)와 입력 순서대로
    LocalEngine.ingest를 부른 상태 파일이 문서별로 바이트까지 같다(change_log는 다중집합). 같은 내용 쌍은 앞 파일
    이름이고 뒤 파일은 unchanged. force로 한 번 더 해도 같다(A·B)."""
    inputs = sorted((FIXTURES / "inputs").glob("*.md")) + sorted((FIXTURES / "pdf" / "inputs").glob("*.pdf"))
    inputs += [FIXTURES / "ocr" / "scanned.pdf", FIXTURES / "layout" / "figures.pdf",
               write(tmp_path / "쌍" / "앞.md"), write(tmp_path / "쌍" / "뒤.md")]
    assert len(inputs) == 15
    result = scenario(tmp_path, DUMP + r'''
import json, os, sys
from hanji import LocalEngine, SqliteStore
from hanji.batch import ingest_many
from hanji.formats.detect import default_parsers


def pool(db, force, **options):
    with ingest_many(paths, db=db, force=force, **options) as run:
        results = sorted(run, key=lambda r: r.index)
    return run, [r.status for r in results]


if __name__ == "__main__":
    paths = json.loads(sys.argv[1])
    same, statuses = [], []
    for force in (False, True):
        b, b_status = pool("B.db", force, workers=3)
        if b.ort_threads is not None:
            os.environ["HANJI_ORT_THREADS"] = str(b.ort_threads)  # A·C도 B와 같은 실효 스레드 수
        with SqliteStore("A.db") as store:
            engine = LocalEngine(store, default_parsers())
            for path in paths:
                engine.ingest(path, force=force)
        if force:  # force 한 번 더는 A·B만(시간)
            same.append(dump("A.db") == dump("B.db"))
            statuses.append([b_status])
            continue
        _, c_status = pool("C.db", force, workers=1, _recycle_every=2)
        same.append(dump("A.db") == dump("B.db") == dump("C.db"))
        statuses.append([b_status, c_status])
    with SqliteStore("B.db") as store:
        pair = store.get_tree("doc_" + __import__("hashlib").sha256(open(paths[-1], "rb").read()).hexdigest()[:24])
    print(json.dumps([same, statuses, pair.source.name]))
''', json.dumps([str(p) for p in inputs]), timeout=600)
    same, statuses, pair_name = result
    assert same == [True, True]
    assert statuses[0] == [["created"] * 14 + ["unchanged"]] * 2
    assert all(s == "unchanged" or s == "created" for run in statuses[1] for s in run)
    assert pair_name == "앞.md"


def test_workers_are_replaced_after_every_recycle_batch(tmp_path):
    """작업 1개, 2건마다 풀을 비우고 새로 만든다: 5건이면 작업 프로세스 3개."""
    for i in range(5):
        write(tmp_path / "in" / f"d{i}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, r'''
import json
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=1, ocr=False, layout=False, _recycle_every=2) as run:
        statuses = [r.status for r in run]
    print(json.dumps([statuses, len(set(run._pids)), run._pids == sorted(run._pids, key=run._pids.index)]))
''')
    assert result == [["created"] * 5, 3, True]


CRASH = r'''
import json, multiprocessing, os, sqlite3, sys
from hanji.batch import ingest_many


def run_once(**options):
    with ingest_many(["in"], db="s.db", ocr=False, layout=False, **options) as run:
        results = sorted(run, key=lambda r: r.index)
    return run, results


def stored(document_id):
    conn = sqlite3.connect("s.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM documents WHERE document_id = ?", (document_id,)).fetchone()[0]
    finally:
        conn.close()
'''


@pytest.mark.parametrize(("stage", "stored", "again"), [("before_commit", 0, "created"),
                                                         ("after_commit", 1, "unchanged")])
def test_a_dying_worker_leaves_a_crashed_result_whose_storage_is_unknown(tmp_path, stage, stored, again):
    """작업 프로세스가 커밋 앞에서 죽으면 저장되지 않았고, 커밋 뒤 응답 전에 죽으면 저장돼 있다. 둘 다 결과는
    worker_crashed(기본 ID 함께)이고 다른 문서는 성공한다. 같은 명령을 다시 돌리면 각각 created·unchanged."""
    for name in ("a.md", "b.md", "c.md"):
        write(tmp_path / "in" / name, f"# {name} 지어낸 제목\n")
    result = scenario(tmp_path, CRASH + r'''
if __name__ == "__main__":
    _, first = run_once(workers=1)
    crashed = first[1]
    in_store = stored(crashed.document_id)
    os.environ.pop("HANJI_TEST_WORKER_EXIT")
    _, second = run_once(workers=1)
    print(json.dumps([[r.status for r in first], crashed.document_id is not None, crashed.version, crashed.error.type,
                      in_store, [r.status for r in second], multiprocessing.active_children()]))
''', env={"HANJI_TEST_WORKER_EXIT": f"b.md:{stage}"})
    first, has_id, version, kind, in_store, second, children = result
    assert first == ["created", "worker_crashed", "created"]
    assert (has_id, version, kind, in_store, children) == (True, None, "WorkerCrashed", stored, [])
    assert second == ["unchanged", again, "unchanged"]


def test_three_broken_pool_generations_in_a_row_abort_only_the_unsent_documents(tmp_path):
    """작업 프로세스가 매번 죽으면 세대마다 실패(보낸 문서는 worker_crashed)이고 연속 3세대 뒤 멈춘다. 보내지 않은 문서만
    BatchAborted, 남은 자식 0."""
    for i in range(8):
        write(tmp_path / "in" / f"d{i}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, CRASH + r'''
if __name__ == "__main__":
    run, results = run_once(workers=2)
    print(json.dumps([[r.status if r.error is None else r.error.type for r in results], run.aborted,
                      multiprocessing.active_children()]))
''', env={"HANJI_TEST_WORKER_EXIT": "*:before_commit"})
    assert result == [["WorkerCrashed"] * 6 + ["BatchAborted"] * 2, True, []]


def test_close_while_aborted_results_are_being_yielded_returns_the_rest(tmp_path):
    """BatchAborted 결과를 내는 도중 close()하면 아직 내지 않은 결과는 close()가 돌려준다(조용히 사라지지 않는다):
    낸 결과 + close() 결과 = 펼친 항목 수, 겹침 없음."""
    for i in range(8):
        write(tmp_path / "in" / f"d{i}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, r'''
import json, multiprocessing
from hanji.batch import ingest_many

if __name__ == "__main__":
    run = ingest_many(["in"], db="s.db", ocr=False, layout=False, workers=2)
    seen = []
    for r in run:
        seen.append(r)
        if r.error is not None and r.error.type == "BatchAborted":
            break
    rest = run.close()
    indexes = [r.index for r in seen + list(rest)]
    print(json.dumps([len(seen), [r.error.type for r in rest], sorted(indexes) == list(range(8)),
                      multiprocessing.active_children()]))
''', env={"HANJI_TEST_WORKER_EXIT": "*:before_commit"})
    assert result == [7, ["BatchAborted"], True, []]


def test_a_generation_with_a_confirmed_result_resets_the_failure_count_even_if_it_breaks(tmp_path):
    """확정 결과를 하나라도 낸 세대는 깨져도 연속 실패로 세지 않는다(컨트롤러 판정): 작업 1개, 세 번째 문서마다 커밋 앞에서
    죽으면 그 문서만 worker_crashed이고 나머지는 created, 배치는 멈추지 않는다."""
    names = [f"d{i}-x.md" if i % 3 == 2 else f"d{i}.md" for i in range(10)]  # 폴더 순서 그대로, 크기가 같다
    for i, name in enumerate(names):
        write(tmp_path / "in" / name, f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, CRASH + r'''
if __name__ == "__main__":
    run, results = run_once(workers=1)
    print(json.dumps([[r.status if r.error is None else r.error.type for r in results], run.aborted,
                      multiprocessing.active_children()]))
''', env={"HANJI_TEST_WORKER_EXIT": "*-x.md:before_commit"})
    assert result == [["created", "created", "WorkerCrashed"] * 3 + ["created"], False, []]


def test_init_failures_count_per_generation_and_a_good_generation_resets_the_count(tmp_path):
    """작업 프로세스 초기화 실패는 그 문서의 WorkerInitFailed이고 그 세대를 실패로 센다. 세대마다 1건(갈아 끼우기 1)으로
    실패·실패·성공·실패·실패·성공이면 성공한 세대에서 연속 수가 0으로 돌아가 멈추지 않는다. 매번 실패하면 3세대 뒤 멈춘다.
    작업 2개에서 한 작업 프로세스만 실패해도 그 세대의 다른 문서는 확정 결과다."""
    names = ("1-bad.md", "2-bad.md", "3-ok.md", "4-bad.md", "5-bad.md", "6-ok.md")  # 폴더 순서 그대로
    for i, name in enumerate(names):  # 크기가 같아 추정이 같다: 비용 내림차순에서도 입력 순서로 보낸다
        write(tmp_path / "in" / name, f"# 지어낸 제목 {i}\n")
    for i, name in enumerate(("1-bad.md", "2-ok.md", "3-ok.md", "4-ok.md")):
        write(tmp_path / "two" / name, f"# 다른 지어낸 제목 {i}\n")
    result = scenario(tmp_path, r'''
import json, multiprocessing, os
from hanji.batch import ingest_many


def run_once(folder, db, **options):
    with ingest_many([folder], db=db, ocr=False, layout=False, **options) as run:
        results = sorted(run, key=lambda r: r.index)
    return [[r.status if r.error is None else r.error.type for r in results], run.aborted]


if __name__ == "__main__":
    runs = [run_once("in", "a.db", workers=1, _recycle_every=1), run_once("two", "b.db", workers=2)]
    os.environ["HANJI_TEST_WORKER_EXIT"] = "*:init"
    runs.append(run_once("in", "c.db", workers=1, _recycle_every=1))
    print(json.dumps([runs, multiprocessing.active_children()]))
''', env={"HANJI_TEST_WORKER_EXIT": "*-bad.md:init"})
    runs, children = result
    assert runs[0] == [["WorkerInitFailed", "WorkerInitFailed", "created", "WorkerInitFailed", "WorkerInitFailed",
                        "created"], False]
    assert runs[1] == [["WorkerInitFailed", "created", "created", "created"], False]
    assert runs[2] == [["WorkerInitFailed"] * 3 + ["BatchAborted"] * 3, True] and children == []


def test_a_file_changed_after_planning_is_not_ingested(tmp_path):
    write(tmp_path / "in" / "a.md")
    result = scenario(tmp_path, r'''
import json
from hanji.batch import ingest_many


def change(path):
    with open(path, "a", encoding="utf-8") as f:
        f.write("덧붙인 지어낸 문단\n")


if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", ocr=False, layout=False, _after_plan=change) as run:
        (result,) = list(run)
    print(json.dumps([result.status, result.error.type, result.document_id]))
''')
    assert result == ["failed", "ChangedDuringRun", None]


def test_documents_are_sent_largest_estimate_first_and_ties_keep_input_order():
    """보내는 순서는 같은 sha 묶음 단위로 첫 문서의 추정 추가 몫 내림차순(가장 긴 일 먼저), 같으면 입력 순서. 묶음
    안은 입력 순서: 뒤 문서의 추정이 더 커도(f: 5 → 500) 앞 문서가 먼저다. 순수 함수라 프로세스를 띄우지 않는다."""
    rows = [("a", 10), ("b", 300), ("c", 10), ("b", 300), ("d", 50), ("e", 0), ("f", 5), ("f", 500)]
    docs = [batch._Doc(batch._Entry(i, f"{i}.md", f"{i}.md"), sha, extra) for i, (sha, extra) in enumerate(rows)]
    assert [d.entry.index for d in batch._submission_order(docs)] == [1, 3, 4, 0, 2, 6, 7, 5]
    assert batch._submission_order([]) == []


def test_same_bytes_under_two_extensions_keep_input_order_like_a_serial_run(tmp_path):
    """같은 바이트를 first.md와 second.pdf로: PDF 추정(쪽 몫)이 더 커도 같은 sha라 입력 순서대로 돈다. 결과는 차례
    실행과 같다(created → unchanged, source.name은 first.md). PDF가 먼저 돌면 ParseError가 난다."""
    data = "# 지어낸 제목\n\n지어낸 문단\n".encode()
    (tmp_path / "in").mkdir()
    for name in ("first.md", "second.pdf"):
        (tmp_path / "in" / name).write_bytes(data)
    result = scenario(tmp_path, r'''
import json
from hanji import LocalEngine, SqliteStore, resources
from hanji.batch import _Entry, _plan, ingest_many
from hanji.formats.detect import default_parsers

if __name__ == "__main__":
    paths = ["in/first.md", "in/second.pdf"]
    cost = resources.COSTS["none"]
    extras = [_plan(_Entry(i, p, p), cost).extra for i, p in enumerate(paths)]
    with SqliteStore("serial.db") as store:
        engine = LocalEngine(store, default_parsers(ocr=False, layout=False))
        serial = ["created" if engine._ingest_outcome(open(p, "rb").read(), p.split("/")[1]).created else "unchanged"
                  for p in paths]
    with ingest_many(paths, db="pool.db", workers=2, ocr=False, layout=False) as run:
        results = sorted(run, key=lambda r: r.index)
    with SqliteStore("pool.db") as store:
        name = store.get_tree(results[0].document_id).source.name
    print(json.dumps([extras[1] > extras[0], serial, [r.status for r in results],
                      [r.error and r.error.type for r in results], name]))
''')
    assert result == [True, ["created", "unchanged"], ["created", "unchanged"], [None, None], "first.md"]


def test_a_document_bigger_than_the_room_runs_alone_with_a_warning(tmp_path):
    """여유보다 큰 문서는 작업 1개짜리 세대에서 혼자 돌고, 그 뒤 W개 세대를 새로 만든다. 추정이 가장 커서 먼저
    보낸다(비용 내림차순). 혼자여도 추정이 예산을 넘으면 경고 한 줄(그래도 돌린다). 바이트 배수를 키워 작은 파일로
    흉내 낸다."""
    write(tmp_path / "in" / "a.md", "# 가\n")
    write(tmp_path / "in" / "big.md", "# 지어낸 큰 문서\n\n" + "지어낸 문단 " * 100 + "\n")
    write(tmp_path / "in" / "c.md", "# 다\n")
    result = scenario(tmp_path, r'''
import json, os
from hanji import resources
from hanji.batch import ingest_many

if __name__ == "__main__":
    resources.BYTES_FACTOR["other"] = 10**6  # 부모가 추정에만 쓴다: 작은 문서 수 MB, big.md 약 1.4GB
    cost = resources.COSTS["none"]  # 예산을 작업 2개가 꼭 들어가게: 여유 = 2 × 50쪽 몫
    budget = resources.PARENT_RESERVE + 2 * (cost.base + cost.per_page * resources.SLOT_PAGES)
    with ingest_many(["in"], db="s.db", workers=2, max_memory=budget, ocr=False, layout=False) as run:
        results = list(run)
    print(json.dumps([run.workers, [os.path.basename(r.path) for r in results], [r.status for r in results],
                      run._pids, run.warnings]))
''')
    workers, order, statuses, pids, warnings = result
    assert workers == 2 and statuses == ["created"] * 3
    assert order[0] == "big.md" and sorted(order[1:]) == ["a.md", "c.md"]  # big(혼자) → a·c(작업 2개 세대)
    assert pids[0] not in pids[1:]  # 혼자 세대의 작업 프로세스는 다음 세대에 없다
    assert len(warnings) == 1 and re.fullmatch(r"in[/\\]big\.md: estimated memory \S+ is above the budget \S+ even "
                                               r"alone; running it anyway", warnings[0])


def test_with_one_worker_documents_over_the_room_share_one_worker_and_warn_once(tmp_path):
    """작업 1개(예산이 작업 1개 몫보다 작아 모든 문서가 여유보다 크다)면 혼자 경로도 문서마다 세대를 새로 만들지 않고
    한 작업 프로세스에서 차례로 돈다. 예산을 넘는다는 경고는 배치에 한 번만(컨트롤러 판정)."""
    for name in ("a.md", "b.md", "c.md"):
        write(tmp_path / "in" / name, f"# {name} 지어낸 제목\n")
    result = scenario(tmp_path, r'''
import json
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=1, max_memory="1M", ocr=False, layout=False) as run:
        statuses = [r.status for r in run]
    print(json.dumps([run.workers, statuses, len(run._pids), len(set(run._pids)), run.warnings]))
''')
    workers, statuses, sent, pids, warnings = result
    assert (workers, statuses, sent, pids) == (1, ["created"] * 3, 3, 1)
    assert len([w for w in warnings if w.endswith("even alone; running it anyway")]) == 1


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX 권한(root가 아닐 때)")
def test_an_unreadable_file_fails_on_its_own_and_the_batch_goes_on(tmp_path):
    write(tmp_path / "in" / "a.md")
    write(tmp_path / "in" / "b.md", "# 읽을 수 없는 지어낸 문서\n").chmod(0)
    write(tmp_path / "in" / "c.md", "# 다른 지어낸 문서\n")
    try:
        result = scenario(tmp_path, r'''
import json
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False) as run:
        results = sorted(run, key=lambda r: r.index)
    print(json.dumps([[r.status, r.error and r.error.type, r.error and r.error.code] for r in results]))
''')
    finally:
        (tmp_path / "in" / "b.md").chmod(0o644)
    assert result == [["created", None, None], ["failed", "PermissionError", 1], ["created", None, None]]


def test_a_script_without_a_main_guard_gets_crashed_results_instead_of_hanging(tmp_path):
    """spawn 작업 프로세스는 스크립트를 다시 import한다. if __name__ == "__main__": 없이 부르면 작업 프로세스가 시작하다
    죽는다: 멈추지 않고 worker_crashed와 BatchAborted로 끝난다(README의 spawn 주의)."""
    for i in range(4):
        write(tmp_path / "in" / f"d{i}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, r'''
import json, multiprocessing
from hanji.batch import ingest_many

with ingest_many(["in"], db="s.db", workers=1, ocr=False, layout=False) as run:
    statuses = sorted({r.status if r.error is None else r.error.type for r in run})
if multiprocessing.parent_process() is None:
    print(json.dumps([statuses, run.aborted]))
''', timeout=120)
    assert result == [["BatchAborted", "WorkerCrashed"], True]


def test_close_ends_only_this_runs_workers_and_reports_the_running_document(tmp_path):
    """느린 문서가 도는 중(시작 표시를 본 뒤) close(): 돌던 문서는 interrupted(기본 ID), 이 실행의 작업 프로세스만
    끝나고(따로 띄운 프로세스는 산다) 돌아온다(시간 상한은 시나리오 바깥 timeout). 끝낸 뒤 다시 부르면 같은 결과."""
    write(tmp_path / "in" / "a.md")
    write(tmp_path / "in" / "slow.md", "# 느린 지어낸 제목\n")
    result = scenario(tmp_path, r'''
import json, multiprocessing, os, time
from hanji.batch import ingest_many

if __name__ == "__main__":
    other = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,))
    other.start()
    run = ingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False)
    first = next(run)
    while not os.path.exists("slow.md.started"):  # 느린 문서가 작업 프로세스에서 돌기 시작했다
        time.sleep(0.05)
    closed = run.close()
    alive = [p is other for p in multiprocessing.active_children()]
    other.terminate()
    print(json.dumps([first.status, [(r.status, r.error.type, r.document_id is not None) for r in closed],
                      alive, run.close() == closed, run.not_started]))
''', timeout=60, env={"HANJI_TEST_WORKER_EXIT": "slow.md:hang"})
    assert result == ["created", [["interrupted", "Interrupted", True]], [True], True, 0]


def test_iterator_contract_with_block_and_close_after_break(tmp_path):
    """with에서 중간에 나오면 남은 자식 0. 참조를 쥔 채 break하면 풀이 남고 close()가 거둔다. close() 없이 참조를 놓으면
    GC가 거둔다."""
    for i in range(6):
        write(tmp_path / "in" / f"d{i}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, r'''
import gc, json, multiprocessing
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(["in"], db="a.db", workers=2, ocr=False, layout=False) as run:
        for result in run:
            break
    after_with = len(multiprocessing.active_children())
    run = ingest_many(["in"], db="b.db", workers=2, ocr=False, layout=False)
    for result in run:
        break
    before_close = len(multiprocessing.active_children())
    run.close()
    after_close = len(multiprocessing.active_children())
    run = ingest_many(["in"], db="c.db", workers=2, ocr=False, layout=False)
    for result in run:
        break
    del run, result
    gc.collect()
    print(json.dumps([after_with, before_close > 0, after_close, len(multiprocessing.active_children())]))
''')
    assert result == [0, True, 0, 0]


def test_the_parent_never_imports_onnxruntime(tmp_path):
    """OCR·레이아웃을 켠(자동) 실행에서 부모는 onnxruntime·numpy를 import하지 않는다(세션은 작업 프로세스가 만든다)."""
    result = scenario(tmp_path, r'''
import json, sys
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(sys.argv[1:], db="s.db", workers=2) as run:
        statuses = sorted(r.status for r in run)
    print(json.dumps([statuses, "onnxruntime" in sys.modules, "numpy" in sys.modules]))
''', FIXTURES / "ocr" / "scanned.pdf", FIXTURES / "layout" / "figures.pdf", write(tmp_path / "a.md"))
    assert result == [["created"] * 3, False, False]


def test_two_batches_with_the_same_file_store_one_version(tmp_path):
    """서로 다른 두 배치(다른 프로세스의 ingest_many)가 같은 내용을 동시에 수집하면 하나는 created, 하나는 unchanged이고
    변경 로그는 한 줄이다."""
    write(tmp_path / "in" / "a.md")
    result = scenario(tmp_path, r'''
import json, multiprocessing as mp, sqlite3
from hanji import SqliteStore


def one(barrier, queue):
    from hanji.batch import ingest_many
    barrier.wait()
    with ingest_many(["in"], db="s.db", workers=1, ocr=False, layout=False) as run:
        queue.put([r.status for r in run])


if __name__ == "__main__":
    SqliteStore("s.db").close()
    ctx = mp.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=one, args=(barrier, queue)) for _ in range(2)]
    for p in procs:
        p.start()
    statuses = sorted(s for _ in procs for s in queue.get(timeout=120))
    for p in procs:
        p.join()
    conn = sqlite3.connect("s.db")
    print(json.dumps([statuses, conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]]))
''')
    assert result == [["created", "unchanged"], 1]


def test_the_private_worker_table_batch_relies_on_is_still_there(tmp_path):
    """세대 끝내기는 ProcessPoolExecutor._processes(비공개, 3.12~3.14 같음)의 프로세스만 끝낸다. 없어지면 크게 실패한다."""
    result = scenario(tmp_path, r'''
import concurrent.futures as cf, json, multiprocessing

if __name__ == "__main__":
    with cf.ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as executor:
        executor.submit(int, "1").result()
        table = getattr(executor, "_processes", None)
        print(json.dumps([type(table).__name__, [type(p).__name__ for p in (table or {}).values()]]))
''')
    assert result == ["dict", ["SpawnProcess"]], "ProcessPoolExecutor._processes is gone; update batch.py"


def test_start_failures_raise_before_any_document_is_sent(tmp_path, monkeypatch):
    """상태 파일을 열 수 없음·형식이 다름, HANJI_ORT_THREADS 값 오류, 명시적 ocr=True인데 설치·모델이 없음은 풀을 띄우기
    전에 예외(CLI 종료 코드 1). 잘못된 workers·max_memory는 ValueError."""
    from hanji.errors import HanjiError, OcrUnavailable
    from hanji.formats.pdf import ocr

    paths = [write(tmp_path / "a.md")]
    (tmp_path / "bad.db").write_bytes(b"not a database file at all" * 10)
    with pytest.raises(Exception, match="file is not a database"):
        batch.ingest_many(paths, db=tmp_path / "bad.db")
    monkeypatch.setenv("HANJI_ORT_THREADS", "x")
    with pytest.raises(HanjiError, match="HANJI_ORT_THREADS"):
        batch.ingest_many(paths, db=tmp_path / "s.db")
    monkeypatch.delenv("HANJI_ORT_THREADS")
    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(ocr, "_missing", lambda: "OCR is not installed (missing numpy)")
    with pytest.raises(OcrUnavailable, match="missing numpy"):
        batch.ingest_many(paths, db=tmp_path / "s.db", ocr=True)
    for bad in ({"workers": 0}, {"max_memory": "1.5"}, {"max_memory": 0}):
        with pytest.raises(ValueError):
            batch.ingest_many(paths, db=tmp_path / "s.db", **bad)
    with pytest.raises(TypeError):
        batch.ingest_many(str(paths[0]), db=tmp_path / "s.db")


ASYNC = r'''
import asyncio, json, multiprocessing, sys, threading, time
from hanji.batch import aingest_many


async def ticker(ticks):
    while True:
        await asyncio.sleep(0.01)
        ticks.append(1)
'''


def test_async_slow_consumer_holds_the_producer_at_eight_results(tmp_path):
    """소비자가 느리면 큐가 8개에서 차고 생산자가 기다린다(실제 역압력). 나중에 모두 받는다."""
    for i in range(20):
        write(tmp_path / "in" / f"d{i:02}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, ASYNC + r'''
async def main():
    run = await aingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False)
    seen = [await run.__anext__()]
    for _ in range(200):  # 큐가 찰 때까지(최대 20초)
        if run._queue.full():
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    held = [run._queue.qsize(), run._thread.is_alive()]
    async for result in run:
        seen.append(result)
    await run.aclose()
    return [held, len(seen), sorted({r.status for r in seen}), run._thread.is_alive(),
            multiprocessing.active_children()]

if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
''')
    assert result == [[8, True], 20, ["created"], False, []]


def test_async_cancel_with_a_full_queue_ends_the_producer_without_blocking_the_loop(tmp_path):
    """큐가 찬 상태에서 소비 작업을 취소하면 생산자 스레드가 끝나고 남은 자식 0, 그동안 이벤트 루프는 막히지 않는다
    (시간 상한은 시나리오 바깥 timeout)."""
    for i in range(20):
        write(tmp_path / "in" / f"d{i:02}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, ASYNC + r'''
async def main():
    full = asyncio.Event()
    holder = {}

    async def consume():
        async with await aingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False) as run:
            holder["run"] = run
            async for _ in run:
                while not run._queue.full():
                    await asyncio.sleep(0.05)
                full.set()
                await asyncio.sleep(3600)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(full.wait(), 60)
    ticks = []
    tick = asyncio.create_task(ticker(ticks))
    start = time.monotonic()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    took = time.monotonic() - start
    tick.cancel()
    run = holder["run"]
    return [run._thread.is_alive(), multiprocessing.active_children(), len(ticks) >= int(took / 0.1)]

if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
''', timeout=60)
    assert result == [False, [], True]


@pytest.mark.parametrize("how", ["aclose", "cancel"])
def test_async_stop_while_the_only_document_is_still_running(tmp_path, how):
    """결과가 아직 없는 동안(긴 문서 하나가 돌기 시작한 뒤) aclose()나 async with 없는 소비 작업 취소: 생산자 스레드가
    끝나고 그 문서는 interrupted, 남은 자식 0(시간 상한은 시나리오 바깥 timeout)."""
    write(tmp_path / "in" / "slow.md")
    result = scenario(tmp_path, ASYNC + r'''
import os


async def main(how):
    run = await aingest_many(["in"], db="s.db", workers=1, ocr=False, layout=False)
    while not os.path.exists("slow.md.started"):  # 느린 문서가 작업 프로세스에서 돌기 시작했다
        await asyncio.sleep(0.05)
    if how == "aclose":
        closed = await run.aclose()
    else:
        waiting = asyncio.Event()

        async def consume():  # async with 없이: 취소가 __anext__에서 aclose()를 부른다
            waiting.set()
            async for _ in run:
                pass

        task = asyncio.create_task(consume())
        await waiting.wait()  # consume은 이벤트를 세운 뒤 queue.get()에서 멈춰 있다
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        closed = run.run.close()
    return [run._thread.is_alive(), [(r.status, r.error.type) for r in closed], multiprocessing.active_children()]

if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1]))))
''', how, timeout=60, env={"HANJI_TEST_WORKER_EXIT": "slow.md:hang"})
    assert result == [False, [["interrupted", "Interrupted"]], []]


def test_async_aclose_returns_buffered_results_so_failures_are_not_lost(tmp_path):
    """aclose()는 아직 건네지 않은 결과(큐에 쌓인 것을 큐 순서로, 그다음 넣으려던 것)와 돌던 문서의 interrupted를 돌려주고
    그 뒤 반복은 끝난다. 실패 결과도 잃지 않는다: 모든 입력은 aclose()가 돌려준 결과이거나 보내지 않은 문서
    (run.not_started)다. 돌려준 created 문서는 저장돼 있다. 여러 번 불러도 같다."""
    for i in range(20):
        write(tmp_path / "in" / f"d{i:02}.md", f"# 지어낸 제목 {i}\n")
    (tmp_path / "in" / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    result = scenario(tmp_path, ASYNC + r'''
import sqlite3


async def main():
    run = await aingest_many(["missing.md", "in"], db="s.db", workers=2, ocr=False, layout=False)
    while not run._queue.full():  # 아무것도 받지 않는다: 첫 결과(없는 파일의 실패)도 큐에 있다
        await asyncio.sleep(0.05)
    closed = await run.aclose()
    again = await run.aclose()
    after = [r async for r in run]
    conn = sqlite3.connect("s.db")
    stored = {row[0] for row in conn.execute("SELECT document_id FROM documents")}
    conn.close()
    indices = [r.index for r in closed]
    created = {r.document_id for r in closed if r.status == "created"}
    first = closed[0]
    return [[first.index, first.status, first.error.type], len(closed) >= 8, again == closed, after,
            len(set(indices)) == len(indices), len(closed) + run.run.not_started == 22,
            bool(created) and created <= stored, run._thread.is_alive(), multiprocessing.active_children()]

if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
''', timeout=60)
    assert result == [[0, "failed", "FileNotFoundError"], True, True, [], True, True, True, False, []]


def test_async_cancelled_next_closes_the_run_and_later_iteration_raises(tmp_path):
    """__anext__를 기다리다 취소되면(wait_for 시간 초과) 실행을 닫는다. 그 뒤 반복은 정상 끝(StopAsyncIteration)이 아니라
    RuntimeError, aclose()는 결과를 돌려준다: 모든 입력은 그 결과이거나 보내지 않은 문서다. 남은 자식 0."""
    for i in range(20):
        write(tmp_path / "in" / f"d{i:02}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, ASYNC + r'''
async def main():
    run = await aingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False)
    try:
        await asyncio.wait_for(run.__anext__(), 0.001)
        timed_out = False
    except TimeoutError:
        timed_out = True
    try:
        await run.__anext__()
        later = "result"
    except StopAsyncIteration:
        later = "stopped"
    except RuntimeError as exc:
        later = str(exc)
    closed = await run.aclose()
    indices = [r.index for r in closed]
    return [timed_out, later, await run.aclose() == closed, sorted({r.status for r in closed} - {"created"}),
            len(set(indices)) == len(indices), len(closed) + run.run.not_started == 20, run._thread.is_alive(),
            multiprocessing.active_children()]

if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
''', timeout=60)
    timed_out, later, same, statuses, unique, accounted, alive, children = result
    assert [timed_out, later, same, unique, accounted, alive, children] == [
        True, "ingest run was closed by cancellation; see aclose() results", True, True, True, False, []]
    assert set(statuses) <= {"interrupted"}


def test_async_an_exception_in_the_producer_reaches_the_consumer(tmp_path):
    """생산자 스레드(IngestRun)에서 난 예외는 소비자의 async for에서 그대로 나온다. 생산자 스레드는 끝나고 남은 자식 0."""
    write(tmp_path / "in" / "a.md")
    result = scenario(tmp_path, ASYNC + r'''
from hanji import resources


def admit(*args, **kwargs):
    raise ValueError("synthetic admit failure")


async def main():
    resources.admit = admit
    run = await aingest_many(["in"], db="s.db", workers=1, ocr=False, layout=False)
    try:
        async for _ in run:
            pass
        got = None
    except ValueError as exc:
        got = str(exc)
    await run.aclose()
    return [got, run._thread.is_alive(), multiprocessing.active_children()]

if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
''', timeout=60)
    assert result == ["synthetic admit failure", False, []]


def test_async_a_run_left_without_aclose_is_cleaned_up_when_the_loop_closes(tmp_path):
    """aclose() 없이 버려진 실행(큐가 찼고 긴 문서 하나가 도는 중): asyncio.run이 루프를 닫으면 생산자가 기다리기를
    멈추고 run.close()로 세대를 끝낸다. 생산자 스레드가 끝나고 남은 자식 0, 스크립트는 끝난다(시간 상한은 시나리오
    바깥 timeout)."""
    write(tmp_path / "in" / "a-slow.md", "# 지어낸 긴 제목\n\n" + "지어낸 문단\n\n" * 20000)
    for i in range(20):
        write(tmp_path / "in" / f"d{i:02}.md", f"# 지어낸 제목 {i}\n")
    result = scenario(tmp_path, ASYNC + r'''
import os


async def main():
    run = await aingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False)
    while not (run._queue.full() and os.path.exists("a-slow.md.started")):
        await asyncio.sleep(0.05)
    await run.__anext__()
    return run  # aclose()·async with 없이 루프를 닫는다

if __name__ == "__main__":
    run = asyncio.run(main())
    for _ in range(600):  # 생산자 스레드가 끝날 때까지(최대 30초)
        if not run._thread.is_alive():
            break
        time.sleep(0.05)
    print(json.dumps([run._thread.is_alive(), [p.pid for p in multiprocessing.active_children()]]))
''', timeout=60, env={"HANJI_TEST_WORKER_EXIT": "a-slow.md:hang"})
    assert result == [False, []]


def test_package_exports_the_batch_api():
    import hanji

    names = ("ingest_many", "aingest_many", "IngestRun", "IngestResult", "IngestError")
    assert all(getattr(hanji, n) is getattr(batch, n) and n in hanji.__all__ for n in names)



FONT = "HYGothic-Medium"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))


def text_pdf(path: Path, pages: int, broken: tuple[int, ...] = ()) -> Path:
    """쪽마다 지어낸 글자 한 줄인 PDF. 쪽 순번 broken은 CropBox가 MediaBox 밖이라 그 쪽을 읽으면 ParseError(빈 쪽 상자)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = Canvas(str(path), pagesize=(595.0, 842.0), invariant=1, pageCompression=0)
    for i in range(pages):
        c.setCropBox((700, 700, 800, 800) if i in broken else (0, 0, 595, 842))
        c.setFont(FONT, 11)
        c.drawString(72, 770, f"지어낸 {i}쪽 문단이다.")
        c.showPage()
    c.save()
    return path


def require_split(ocr: bool = False) -> None:
    """쪼개기는 모델을 쓸 때만이다(여기서는 레이아웃 자동, ocr이면 OCR도). 설치·모델 파일이 없으면 건너뛰고,
    HANJI_CI_REQUIRE_MODELS=1이면 실패한다. 글자만 있는 쪽은 레이아웃을 켜도 모델을 돌리지 않는다."""
    missing = [name for name, runtime in (("layout", layout_runtime), ("ocr", ocr_runtime))
               if (name == "layout" or ocr) and not runtime.available()]
    if missing:
        if os.environ.get("HANJI_CI_REQUIRE_MODELS") == "1":
            pytest.fail(f"model runtimes not available: {missing}; run `hanji models fetch`")
        pytest.skip(f"model runtimes not available: {missing}")


STRICT_ASSEMBLY = r"""
from hanji.formats.pdf import parser as _parser

_real_parse = _parser.PdfParser.parse


def _forbidden(*args, **kwargs):
    raise AssertionError("assembly computed a page model instead of using the chunk results")


def _strict_parse(self, data, name, models=None):
    # 조립(models 있음)이면 받은 쪽 모델이 모든 쪽이어야 하고, 조립 중 쪽 모델을 계산하면 실패한다. 이 줄들은
    # 스크립트 맨 위라 spawn 작업 프로세스(스크립트를 __mp_main__으로 다시 import한다)에서도 돈다
    if models is None:
        return _real_parse(self, data, name)
    import pypdfium2
    document = pypdfium2.PdfDocument(data)
    count = len(document)
    document.close()
    assert sorted(models) == list(range(count)), (sorted(models), count)
    real, _parser._models = _parser._models, _forbidden
    try:
        return _real_parse(self, data, name, models)
    finally:
        _parser._models = real


_parser.PdfParser.parse = _strict_parse
"""


def test_split_ingest_matches_a_serial_run_byte_for_byte(tmp_path):
    """기준을 1쪽·묶음 1쪽으로 낮추고 작업 2개로 OCR·그림 예제와 여러 쪽 골든을 문서마다 쪼개 수집한 상태 파일(B), 그리고
    같은 것을 갈아 끼우기 1(일마다 새 풀)로 수집한 상태 파일(C)이 같은 ORT 스레드 수로 차례로 LocalEngine.ingest한 것과
    문서별로 바이트까지 같다. 쪼갠 문서는 쪽마다 묶음 하나, 그 뒤 조립 하나이고, 조립은 받은 쪽 모델(모든 쪽)만 쓴다
    (STRICT_ASSEMBLY: 조립 중 쪽 모델을 계산하면 그 문서가 failed가 된다)."""
    require_split(ocr=True)
    inputs = [FIXTURES / "layout" / "figures.pdf", FIXTURES / "ocr" / "scanned.pdf",
              FIXTURES / "pdf" / "inputs" / "report.pdf", FIXTURES / "pdf" / "inputs" / "header_footer.pdf"]
    result = scenario(tmp_path, STRICT_ASSEMBLY + DUMP + r"""
import json, os, sys
from hanji import LocalEngine, SqliteStore
from hanji.batch import ingest_many

if __name__ == "__main__":
    paths = json.loads(sys.argv[1])
    logs, threads = [], None
    for db, recycle in (("B.db", None), ("C.db", 1)):
        for path in paths:  # 문서마다 따로: 쪼갠 문서는 한 번에 하나라서
            with ingest_many([path], db=db, workers=2, _recycle_every=recycle, _split_min_pages=1,
                             _chunk_pages=1) as run:
                logs.append([[r.status for r in run], run._log])
            threads = run.ort_threads
    if threads is not None:
        os.environ["HANJI_ORT_THREADS"] = str(threads)
    from hanji.formats.detect import default_parsers
    with SqliteStore("A.db") as store:
        engine = LocalEngine(store, default_parsers())
        for path in paths:
            engine.ingest(path)
    print(json.dumps([logs, dump("A.db") == dump("B.db") == dump("C.db")]))
""", json.dumps([str(p) for p in inputs]), timeout=600)
    logs, same = result
    assert [statuses for statuses, _ in logs] == [["created"]] * 8 and same
    assert [log for _, log in logs] == [[["chunk", 0]] * pages + [["assemble", 0]] for pages in (2, 1, 2, 3)] * 2


def test_chunks_come_before_the_assembly_one_result_per_document_and_one_split_at_a_time(tmp_path):
    """큰 문서(a.pdf 4쪽)는 묶음 넷 뒤에 조립 하나, 결과는 문서마다 하나다. a가 쪼개진 동안 줄 맨 앞에 온 b.pdf(3쪽)는
    기다리지 않고 쪼개지 않은 보통 작업으로 간다."""
    require_split()
    text_pdf(tmp_path / "in" / "a.pdf", 4)
    text_pdf(tmp_path / "in" / "b.pdf", 3)
    write(tmp_path / "in" / "c.md")
    result = scenario(tmp_path, r"""
import json
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, _split_min_pages=1, _chunk_pages=1) as run:
        results = sorted(run, key=lambda r: r.index)
    print(json.dumps([[r.status for r in results], [r.index for r in results], run._log]))
""")
    statuses, indexes, log = result
    assert statuses == ["created"] * 3 and indexes == [0, 1, 2]
    assert [kind for kind, index in log if index == 0] == ["chunk"] * 4 + ["assemble"]
    assert [kind for kind, index in log if index == 1] == ["doc"]


def test_close_while_the_assembly_waits_reports_the_split_document_interrupted(tmp_path):
    """예산을 작업 2개·여유 100쪽 몫으로 잡으면 120쪽 PDF는 문서로는 여유보다 크지만 60쪽 묶음은 들어간다. 묶음 둘이
    끝난 뒤 조립(문서 몫)은 돌던 일(느린 문서)이 끝나기를 기다린다. 그때 close()하면 쪼갠 문서와 느린 문서가 각각
    interrupted 하나이고 남은 자식은 없다."""
    require_split()
    text_pdf(tmp_path / "in" / "big.pdf", 120)
    write(tmp_path / "in" / "slow.md", "# 느린 지어낸 제목\n")
    result = scenario(tmp_path, r"""
import json, multiprocessing, os, threading, time
from hanji import resources
from hanji.batch import ingest_many

if __name__ == "__main__":
    cost = resources.COSTS["layout"]
    budget = resources.PARENT_RESERVE + 2 * (cost.base + resources.SLOT_PAGES * cost.per_page)
    run = ingest_many(["in"], db="s.db", workers=2, max_memory=budget, ocr=False, _chunk_pages=60)
    got = []
    worker = threading.Thread(target=lambda: got.extend(run), daemon=True)
    worker.start()

    def waiting():
        split = run._split
        return split is not None and len(split.models) == 120 and os.path.exists("slow.md.started")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not waiting():
        time.sleep(0.05)
    seen = waiting()
    rest = run.close()
    worker.join(30)
    print(json.dumps([seen, run._log, got, sorted((r.index, r.status) for r in rest),
                      multiprocessing.active_children()]))
""", env={"HANJI_TEST_WORKER_EXIT": "slow.md:hang"})
    assert result == [True, [["chunk", 0], ["chunk", 0], ["doc", 1]], [], [[0, "interrupted"], [1, "interrupted"]], []]


def test_close_while_chunks_run_is_one_interrupted_result_and_no_children(tmp_path):
    """묶음이 도는 중 close()하면 쪼갠 문서의 결과는 interrupted 하나(기본 ID 함께)이고 남은 자식은 없다."""
    require_split()
    text_pdf(tmp_path / "in" / "big.pdf", 3)
    result = scenario(tmp_path, r"""
import json, multiprocessing, os, threading, time
from hanji.batch import ingest_many

if __name__ == "__main__":
    run = ingest_many(["in"], db="s.db", workers=2, ocr=False, _split_min_pages=1, _chunk_pages=1)
    threading.Thread(target=lambda: list(run), daemon=True).start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not os.path.exists("big.pdf.started"):
        time.sleep(0.05)
    rest = run.close()
    print(json.dumps([[(r.index, r.status, r.document_id is not None) for r in rest], run._log,
                      multiprocessing.active_children()]))
""", env={"HANJI_TEST_WORKER_EXIT": "big.pdf:hang"})
    assert result == [[[0, "interrupted", True]], [["chunk", 0], ["chunk", 0]], []]


@pytest.mark.parametrize("broken", [(1,), (0, 1)], ids=["one", "two-siblings"])
def test_a_failing_chunk_reruns_the_document_once_and_fails_like_a_serial_run(tmp_path, broken):
    """묶음이 ParseError로 실패하면 보내지 않은 묶음은 버리고, 돌던 형제 묶음이 모두 끝난 뒤 문서를 쪼개지 않은 보통
    작업으로 한 번만 다시 보낸다: 결과는 차례 실행과 같은 오류(코드·문구) 하나다. 함께 돌던 두 묶음이 모두 실패해도
    다시 보내기는 한 번이다."""
    require_split()
    text_pdf(tmp_path / "in" / "bad.pdf", 3, broken)
    result = scenario(tmp_path, r"""
import json
from hanji import LocalEngine, SqliteStore
from hanji.batch import ingest_many
from hanji.errors import ParseError
from hanji.formats.detect import default_parsers

if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, _split_min_pages=1, _chunk_pages=1) as run:
        (r,) = list(run)
    with SqliteStore("a.db") as store:
        try:
            LocalEngine(store, default_parsers(ocr=False)).ingest("in/bad.pdf")
            serial = None
        except ParseError as exc:
            serial = str(exc)
    print(json.dumps([r.status, r.error.code, r.error.type, r.error.message == serial, run._log]))
""")
    status, code, kind, same, log = result
    assert (status, code, kind, same) == ("failed", 4, "ParseError", True)
    assert log.count(["again", 0]) == 1 and ["assemble", 0] not in log
    if broken == (0, 1):
        assert log == [["chunk", 0], ["chunk", 0], ["again", 0]]


def test_a_dying_chunk_is_one_crashed_result_without_a_rerun(tmp_path):
    """묶음 중 작업 프로세스가 죽으면 그 문서의 결과는 worker_crashed 하나다(다시 보내지 않고 저장되지 않는다). 다음
    세대에서 다른 문서는 돈다."""
    require_split()
    text_pdf(tmp_path / "in" / "big.pdf", 3)
    write(tmp_path / "in" / "other.md")
    result = scenario(tmp_path, CRASH + r"""
if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, _split_min_pages=1, _chunk_pages=1) as run:
        results = sorted(run, key=lambda r: r.index)
    print(json.dumps([[(r.status, r.error and r.error.type) for r in results], run._log, stored(results[0].document_id),
                      run.aborted, multiprocessing.active_children()]))
""", env={"HANJI_TEST_WORKER_EXIT": "big.pdf:chunk"})
    assert result == [[["worker_crashed", "WorkerCrashed"], ["created", None]],
                      [["chunk", 0], ["chunk", 0], ["doc", 1]], 0, False, []]


def test_a_file_changed_after_planning_fails_its_chunks_without_a_rerun(tmp_path):
    require_split()
    text_pdf(tmp_path / "in" / "big.pdf", 3)
    result = scenario(tmp_path, r"""
import json
from hanji.batch import ingest_many


def change(path):
    with open(path, "ab") as f:
        f.write(b"%% appended")


if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, _split_min_pages=1, _chunk_pages=1,
                     _after_plan=change) as run:
        (r,) = list(run)
    print(json.dumps([r.status, r.error.type, run._log]))
""")
    assert result == ["failed", "ChangedDuringRun", [["chunk", 0], ["chunk", 0]]]


def test_an_already_stored_document_is_not_split_unless_forced(tmp_path):
    """같은 문서 ID에 같은 원본이 이미 저장돼 있으면 쪼개지 않고 보통 작업으로 unchanged다. force면 다시 쪼갠다."""
    require_split()
    text_pdf(tmp_path / "in" / "big.pdf", 3)
    result = scenario(tmp_path, r"""
import json
from hanji.batch import ingest_many

if __name__ == "__main__":
    out = []
    for force in (False, False, True):
        with ingest_many(["in"], db="s.db", workers=2, ocr=False, force=force, _split_min_pages=1,
                         _chunk_pages=1) as run:
            out.append([[r.status for r in run], [kind for kind, _ in run._log]])
    print(json.dumps(out))
""")
    assert result == [[["created"], ["chunk"] * 3 + ["assemble"]], [["unchanged"], ["doc"]],
                      [["unchanged"], ["chunk"] * 3 + ["assemble"]]]


def test_a_copy_with_the_same_bytes_waits_for_the_split_document_and_keeps_the_first_name(tmp_path):
    """같은 바이트의 두 파일: 앞 파일이 쪼개진 동안 뒤 파일은 같은 sha라 기다리고(쪼갠 문서가 sha를 갖는다), 앞 파일이
    저장된 뒤 쪼개지 않은 보통 작업으로 unchanged다. 저장된 이름은 차례 실행처럼 앞 파일이다."""
    require_split()
    text_pdf(tmp_path / "in" / "a.pdf", 3)
    shutil.copy(tmp_path / "in" / "a.pdf", tmp_path / "in" / "b.pdf")
    result = scenario(tmp_path, r"""
import json
from hanji import SqliteStore
from hanji.batch import ingest_many

if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, _split_min_pages=1, _chunk_pages=1) as run:
        results = sorted(run, key=lambda r: r.index)
    with SqliteStore("s.db") as store:
        name = store.get_tree(results[0].document_id).source.name
    print(json.dumps([[r.status for r in results], run._log, name]))
""")
    assert result == [["created", "unchanged"], [["chunk", 0]] * 3 + [["assemble", 0], ["doc", 1]], "a.pdf"]



def test_the_stored_check_only_reads_and_gives_up_on_a_lock_within_about_a_second(tmp_path):
    """이미 저장됐나는 읽기 전용 연결로 최신 원본 해시 하나만 읽는다: 없는 상태 파일을 만들지 않고, 다른 연결이 쓰기
    잠금을 쥐고 있어도(WAL) 바로 읽는다. 읽을 수 없으면(표 없음, 배타 잠금 STORED_TIMEOUT 넘김) 저장되지 않은 것으로
    본다(쪼개도 조립이 unchanged를 낸다)."""
    import sqlite3

    from hanji import LocalEngine, SqliteStore

    pdf = text_pdf(tmp_path / "a.pdf", 3)
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    db, doc = tmp_path / "s.db", "doc_" + sha[:24]
    assert not batch._stored(db, doc, sha) and not db.exists()
    with SqliteStore(db) as store:
        LocalEngine(store, default_parsers(ocr=False, layout=False)).ingest(str(pdf))
    writer = sqlite3.connect(db)
    writer.execute("BEGIN IMMEDIATE")  # 작업 프로세스가 커밋 중인 것처럼
    try:
        start = time.monotonic()
        assert batch._stored(db, doc, sha) and not batch._stored(db, doc, "0" * 64)
        assert not batch._stored(db, "doc_other", sha)
        # 넉넉한 상한: 저장소를 쓰기로 열면 60초 넘게 막힌다(느린 CI 러너에서 2.2초를 봤다)
        assert time.monotonic() - start < 10
    finally:
        writer.rollback()
        writer.close()
    other = tmp_path / "other.db"  # 표가 없는 롤백 저널 파일을 배타 잠금
    holder = sqlite3.connect(other, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        assert not batch._stored(other, doc, sha)
        assert time.monotonic() - start < batch.STORED_TIMEOUT + 9  # 넉넉한 상한(느린 CI 러너)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_failed_generation_with_a_final_document_result_does_not_count_toward_the_abort(tmp_path):
    """작업 2개, 2건마다 새 풀. 초기화가 실패하는 큰 MD 다섯과 계획 뒤 바뀐 PDF(쪼갬), 끝에 보통 MD 하나. 셋째 세대는
    초기화 실패(실패 세대)와 바뀐 PDF 묶음의 최종 문서 결과(ChangedDuringRun)를 함께 낸다: 문서의 확정 결과가 있으니
    연속 실패로 세지 않아 배치는 멈추지 않고 끝 문서가 created다(묶음 결과만 낸 실패 세대는 세지 않는다)."""
    require_split()
    for i in range(1, 6):
        write(tmp_path / "in" / f"{i}-bad.md", f"# 지어낸 제목 {i}\n\n" + "지어낸 문단이다. " * 40000)  # 추정이 PDF보다 크다
    text_pdf(tmp_path / "in" / "big.pdf", 3)
    write(tmp_path / "in" / "ok.md")
    result = scenario(tmp_path, r"""
import json
from hanji.batch import ingest_many


def change(path):
    if path.endswith("big.pdf"):
        with open(path, "ab") as f:
            f.write(b"%% appended")


if __name__ == "__main__":
    with ingest_many(["in"], db="s.db", workers=2, ocr=False, _recycle_every=2, _split_min_pages=1, _chunk_pages=1,
                     _after_plan=change) as run:
        results = sorted(run, key=lambda r: r.index)
    print(json.dumps([[r.status if r.error is None else r.error.type for r in results], run.aborted, run._log]))
""", env={"HANJI_TEST_WORKER_EXIT": "*-bad.md:init"})
    statuses, aborted, log = result
    assert statuses == ["WorkerInitFailed"] * 5 + ["ChangedDuringRun", "created"] and not aborted
    assert log[:6] == [["doc", 0], ["doc", 1], ["doc", 2], ["doc", 3], ["doc", 4], ["chunk", 5]]
