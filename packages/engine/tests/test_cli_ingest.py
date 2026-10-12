"""hanji ingest: JSON Lines·요약·종료 코드. 풀을 띄우는 실행은 자기 프로세스 그룹의 자식 Python(run_group)으로 돌린다:
멈추거나 실패하면 그룹째(작업 프로세스까지) 끝낸다. 글자는 모두 지어낸 합성 문장이다."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hanji.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLI = "import sys; from hanji.cli import main; sys.exit(main(sys.argv[1:]))"
SUMMARY = re.compile(r"^hanji: (\d+) documents(: [a-z_0-9, ]+)?(; [^;]+)*; \d+\.\ds; workers \d+, "
                     r"ORT threads (\d+|default), memory budget (\d+\.\dGiB|\d+MiB|unknown)$")
STOP_GRACE = 5.0  # 그룹을 끝낼 때 SIGTERM 뒤 SIGKILL까지 기다리는 초


def write(path: Path, text: str = "# 지어낸 제목\n\n지어낸 문단\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _signal_group(pgid: int, sig: int) -> bool:
    """프로세스 그룹에 신호를 보낸다(POSIX). 그룹이 이미 없으면 False."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):  # macOS는 좀비만 남은 그룹에 EPERM을 줄 수 있다
        return False
    return True


def _end_group(child: subprocess.Popen) -> None:
    """자식과 그 그룹(작업 프로세스 포함)을 끝내고 거둔다. POSIX: SIGTERM → 그룹이 빌 때까지 최대 STOP_GRACE초 →
    SIGKILL → 거두기. Windows: taskkill /F /T → 거두기."""
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


def run_group(args: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None
              ) -> subprocess.CompletedProcess:
    """자식 Python을 자기 프로세스 그룹에서 돌린다(subprocess.run과 같은 결과). 시간을 넘기면 그룹째 끝내고 거둔 뒤
    TimeoutExpired, 0이 아닌 코드로 끝나면 남은 작업 프로세스가 다음 테스트로 새지 않게 그룹을 끝낸다."""
    group: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                   else {"start_new_session": True})
    child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             cwd=cwd, env={**os.environ, **(env or {})}, **group)
    try:
        try:
            out, err = child.communicate(timeout=timeout)
        except BaseException as exc:  # 시간 초과·Ctrl+C: 그룹을 끝내고 남은 출력을 붙여 다시 던진다
            _end_group(child)
            try:
                out, err = child.communicate(timeout=STOP_GRACE)
            except subprocess.TimeoutExpired:  # 그룹 밖으로 샌 손주가 파이프를 쥐고 있다
                out, err = "", ""
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            raise subprocess.TimeoutExpired(child.args, timeout, output=out, stderr=err) from None
        if child.returncode != 0:
            _end_group(child)
    finally:
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


def ingest(tmp_path: Path, *args: object, env: dict[str, str] | None = None) -> tuple[int, list[dict], list[str]]:
    """hanji ingest를 자식 Python으로 돌린다: (종료 코드, JSON 줄(순번 순), 표준 오류 줄)."""
    done = run_group([sys.executable, "-c", CLI, "ingest", *map(str, args), "--db", str(tmp_path / "s.db")],
                     tmp_path, 180, env)
    lines = sorted((json.loads(line) for line in done.stdout.splitlines()), key=lambda r: r["index"])
    return done.returncode, lines, done.stderr.splitlines()


def test_ingest_prints_one_json_line_per_document_and_a_summary(tmp_path):
    """문서마다 JSON 한 줄(created는 문서 ID·버전, PDF만 쪽 수, 오류 null), 표준 오류 끝에 요약 한 줄, 종료 코드 0.
    다시 돌리면 모두 unchanged(같은 버전)."""
    write(tmp_path / "in" / "a.md")
    shutil.copy(FIXTURES / "pdf" / "inputs" / "report.pdf", tmp_path / "in")
    write(tmp_path / "in" / "note.txt")
    code, lines, err = ingest(tmp_path, "in", "--workers", "2", "--no-ocr", "--no-layout")
    assert code == 0
    assert [(r["path"], r["status"], r["pages"], r["version"], r["error"]) for r in lines] == [
        (os.path.join("in", "a.md"), "created", None, 1, None),
        (os.path.join("in", "report.pdf"), "created", 2, 1, None)]
    assert all(r["document_id"].startswith("doc_") and isinstance(r["seconds"], float) for r in lines)
    assert set(lines[0]) == {"index", "path", "status", "document_id", "version", "pages", "seconds", "error"}
    assert SUMMARY.match(err[-1]) and err[-1].startswith("hanji: 2 documents: 2 created; 1 files skipped")
    code, again, err = ingest(tmp_path, "in", "--no-ocr", "--no-layout")
    assert code == 0 and [(r["status"], r["version"]) for r in again] == [("unchanged", 1)] * 2
    assert err[-1].startswith("hanji: 2 documents: 2 unchanged;")


def test_failed_documents_have_null_fields_a_reason_and_exit_6(tmp_path):
    """지원하지 않는 형식(파일 인자는 확장자와 상관없이 넣는다)은 코드 3, 없는 경로는 코드 1이고 문서 ID·버전·쪽 수는
    null, 문구는 비지 않는다. 하나라도 failed면 종료 코드 6."""
    write(tmp_path / "a.md")
    write(tmp_path / "x.txt", "지어낸 글자 파일\n")
    code, lines, err = ingest(tmp_path, "a.md", "x.txt", "없는.md", "--no-ocr", "--no-layout")
    assert code == 6
    assert [(r["status"], r["error"] and (r["error"]["code"], r["error"]["type"])) for r in lines] == [
        ("created", None), ("failed", (3, "UnsupportedFormat")), ("failed", (1, "FileNotFoundError"))]
    assert all((r["document_id"], r["version"], r["pages"]) == (None, None, None) and r["error"]["message"]
               for r in lines[1:])
    assert err[-1].startswith("hanji: 3 documents: 1 created, 2 failed;")


def test_three_broken_generations_exit_7_and_crashed_lines_keep_the_document_id(tmp_path):
    for i in range(5):
        write(tmp_path / "in" / f"d{i}.md", f"# 지어낸 제목 {i}\n")
    code, lines, err = ingest(tmp_path, "in", "--workers", "1", "--no-ocr", "--no-layout",
                              env={"HANJI_TEST_WORKER_EXIT": "*:before_commit"})
    assert code == 7
    assert [r["status"] if r["error"] is None else r["error"]["type"] for r in lines] == [
        "WorkerCrashed"] * 3 + ["BatchAborted"] * 2
    assert all(r["document_id"] and r["version"] is None for r in lines[:3])
    assert "3 worker_crashed" in err[-1] and "batch aborted" in err[-1]


@pytest.mark.parametrize("option", [["--workers", "0"], ["--max-memory", "1.5"], ["--max-memory", "lots"]])
def test_bad_workers_or_max_memory_is_a_usage_error(capsys, tmp_path, option):
    assert main(["ingest", str(tmp_path), "--db", str(tmp_path / "s.db"), *option]) == 2
    assert "usage:" in capsys.readouterr().err


def test_a_state_file_that_cannot_be_used_is_a_start_failure(capsys, tmp_path, monkeypatch):
    """상태 파일 형식이 다르거나 HANJI_ORT_THREADS가 잘못되면 문서를 보내기 전에 종료 코드 1(JSON 줄 없음)."""
    (tmp_path / "bad.db").write_bytes(b"not a database file at all" * 10)
    assert main(["ingest", str(tmp_path), "--db", str(tmp_path / "bad.db")]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "file is not a database" in err
    monkeypatch.setenv("HANJI_ORT_THREADS", "0")
    assert main(["ingest", str(tmp_path), "--db", str(tmp_path / "s.db")]) == 1
    assert "HANJI_ORT_THREADS must be a positive integer" in capsys.readouterr().err


CTRL_C = r'''
import json, os, signal, subprocess, sys, time

CLI = "import sys; from hanji.cli import main; sys.exit(main(sys.argv[1:]))"


def stop(signum, frame):  # 바깥 실행기가 시간 초과로 SIGTERM을 보내도 아래 finally가 CLI 그룹을 끝내게
    sys.exit(1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    proc = subprocess.Popen([sys.executable, "-c", CLI, "ingest", "in", "--workers", "1", "--no-ocr", "--no-layout",
                             "--db", "s.db"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env={**os.environ, "HANJI_TEST_WORKER_EXIT": "slow.md:hang"},
                            start_new_session=True)  # CLI와 작업 프로세스는 자기 그룹: 실패하면 그룹째 끝낸다
    try:
        first = proc.stdout.readline()  # a.md가 끝났다: slow.md를 보냈다(CLI가 먼저 끝나면 빈 줄)
        while proc.poll() is None and not os.path.exists("slow.md.started"):  # slow.md가 돌기 시작했다
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)  # CLI pid에만 보낸다. 이미 끝난 프로세스면 아무것도 하지 않는다
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # 무엇이 실패했든 CLI와 그 작업 프로세스를 남기지 않는다
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
    print(json.dumps([proc.returncode, [line for line in [first, *out.splitlines()] if line.strip()],
                      err.splitlines()[-1:]]))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT는 POSIX에서 본다(Windows CTRL_BREAK_EVENT는 다르다)")
def test_ctrl_c_reports_the_running_document_as_interrupted_and_exits_130(tmp_path):
    """Ctrl+C 시나리오 전체를 바깥 자식 Python(run_group, 시간 상한)이 감싼다: 어디서 멈춰도 시간 초과로 실패하고,
    시나리오는 실패하면 CLI 그룹을 죽인다. 느린 문서의 시작 표시를 본 뒤 CLI pid에만 SIGINT를 보낸다."""
    write(tmp_path / "in" / "a.md")
    write(tmp_path / "in" / "slow.md", "# 느린 지어낸 제목\n")
    write(tmp_path / "in" / "z.md", "# 끝\n")  # 크기 a > slow > z: 비용 내림차순으로도 a → slow → z
    (tmp_path / "ctrl_c.py").write_text(CTRL_C, encoding="utf-8")
    done = run_group([sys.executable, str(tmp_path / "ctrl_c.py")], tmp_path, 120)
    assert done.returncode == 0, done.stderr
    code, lines, last = json.loads(done.stdout.strip().splitlines()[-1])
    assert code == 130
    results = [json.loads(line) for line in lines]
    assert [(r["path"], r["status"]) for r in results] == [(os.path.join("in", "a.md"), "created"),
                                                           (os.path.join("in", "slow.md"), "interrupted")]
    assert results[1]["document_id"].startswith("doc_") and results[1]["error"]["type"] == "Interrupted"
    assert last[0].startswith("hanji: 2 documents: 1 created, 1 interrupted; 1 not started;")


def test_no_documents_is_exit_0_with_a_summary(capsys, tmp_path):
    """고를 문서가 없으면(지원하지 않는 확장자만) 작업 프로세스를 띄우지 않고 종료 코드 0, 요약은 0건."""
    write(tmp_path / "in" / "note.txt")
    assert main(["ingest", str(tmp_path / "in"), "--db", str(tmp_path / "s.db")]) == 0
    out, err = capsys.readouterr()
    assert out == "" and SUMMARY.match(err.splitlines()[-1])
    assert err.splitlines()[-1].startswith("hanji: 0 documents; 1 files skipped (unsupported extension); ")
