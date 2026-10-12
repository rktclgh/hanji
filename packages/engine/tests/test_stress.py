"""오래 도는 동시성 반복(CI 기본 밖): `uv run pytest -m stress`. 시나리오는 자식 Python에서 돈다."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.stress


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


def scenario(tmp_path: Path, source: str, *args: object, timeout: float = 900,
             env: dict[str, str] | None = None):
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


def test_eight_processes_open_new_state_files_for_twenty_rounds(tmp_path):
    outcomes = scenario(tmp_path, r"""
        import json, multiprocessing as mp, os


        def open_store(path, barrier, queue):
            from hanji import SqliteStore
            barrier.wait()
            try:
                SqliteStore(path).close()
                queue.put("ok")
            except Exception as exc:
                queue.put(type(exc).__name__)


        if __name__ == "__main__":
            ctx = mp.get_context("spawn")
            outcomes = []
            for round_ in range(20):
                path = os.path.join(os.getcwd(), f"race{round_}", "state.db")
                barrier, queue = ctx.Barrier(8), ctx.Queue()
                procs = [ctx.Process(target=open_store, args=(path, barrier, queue)) for _ in range(8)]
                for p in procs:
                    p.start()
                outcomes += [queue.get(timeout=120) for _ in procs]
                for p in procs:
                    p.join()
            print(json.dumps(sorted(set(outcomes)) + [len(outcomes)]))
    """)
    assert outcomes == ["ok", 160]


def test_eight_processes_commit_forty_documents_each(tmp_path):
    result = scenario(tmp_path, r"""
        import json, multiprocessing as mp, os, sqlite3


        def writer(k, barrier, queue):
            from hanji import LocalEngine, SqliteStore
            from hanji.formats.detect import default_parsers
            with SqliteStore("s.db") as store:
                engine = LocalEngine(store, default_parsers(ocr=False, layout=False))
                barrier.wait()
                for i in range(40):
                    text = f"# 지어낸 제목 {k}-{i}\n\n" + "지어낸 문단 " * 2000 + "\n"
                    engine.ingest_bytes(text.encode(), f"w{k}_{i}.md")
            queue.put("ok")


        if __name__ == "__main__":
            ctx = mp.get_context("spawn")
            barrier, queue = ctx.Barrier(8), ctx.Queue()
            procs = [ctx.Process(target=writer, args=(k, barrier, queue)) for k in range(8)]
            for p in procs:
                p.start()
            done = [queue.get(timeout=600) for _ in procs]
            for p in procs:
                p.join()
            conn = sqlite3.connect("s.db")
            counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("documents", "change_log")]
            print(json.dumps([done.count("ok"), counts]))
    """)
    assert result == [8, [320, 320]]


def test_two_hundred_documents_with_crashes_and_recycling(tmp_path):
    """200건, 10건마다 풀을 새로, 20건에 한 번 작업 프로세스가 죽는다(세대가 번갈아 실패해 멈추지 않는다). 죽은 문서와 그
    세대에 함께 돌던 문서만 worker_crashed이고(함께 돌던 문서는 커밋 뒤에 끝났을 수 있다), 다시 돌리면 모두 저장된다."""
    result = scenario(tmp_path, r"""
        import json, multiprocessing, os, sqlite3
        from collections import Counter
        from hanji.batch import ingest_many

        if __name__ == "__main__":
            os.makedirs("in")
            for i in range(200):
                with open(f"in/d{i:03}.md", "w", encoding="utf-8") as f:
                    f.write(f"# 지어낸 제목 {i}\n\n지어낸 문단 {i}\n")
            with ingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False, _recycle_every=10) as run:
                first = Counter(r.status for r in run)
            os.environ.pop("HANJI_TEST_WORKER_EXIT")
            with ingest_many(["in"], db="s.db", workers=2, ocr=False, layout=False, _recycle_every=10) as again:
                second = Counter(r.status for r in again)
            conn = sqlite3.connect("s.db")
            stored = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            print(json.dumps([first["created"] + first["worker_crashed"], first["worker_crashed"] >= 10, run.aborted,
                              second["created"] + second["unchanged"] == 200,
                              10 <= second["created"] <= first["worker_crashed"], stored,
                              multiprocessing.active_children()]))
    """, env={"HANJI_TEST_WORKER_EXIT": "d[0-9][02468]5.md:before_commit"})
    assert result == [200, True, False, True, True, 200, []]
