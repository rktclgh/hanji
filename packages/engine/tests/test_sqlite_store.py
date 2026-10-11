import hashlib
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from hanji.core import build_tree, diff_trees
from hanji.errors import HanjiError, StoreConflict
from hanji.formats.base import ParsedSource
from hanji.store import SqliteStore
from hanji.store import sqlite as sqlite_store
from hanji_contracts import DocumentTree, ProcessingHistory, SourceInfo

SOURCE = SourceInfo(name="메모.md", mime="text/markdown", content_hash="sha256:" + "0" * 64)


def make_tree(doc: str, version: int, *texts: str) -> DocumentTree:
    blocks = [{"kind": "paragraph", "text": t, "confidence": 1.0, "state": "det", "text_source": "native",
               "locator": {"kind": "lines", "line_start": i + 1, "line_end": i + 1}} for i, t in enumerate(texts)]
    return build_tree(ParsedSource(mime="text/markdown", blocks=blocks), doc, version, SOURCE)


def commit(store: SqliteStore, doc: str, *texts: str) -> int:
    prev = store.latest(doc)
    tree = make_tree(doc, 1 if prev is None else prev.version + 1, *texts)
    return store.commit(tree, diff_trees(prev, tree), ProcessingHistory(document_id=doc, version=tree.version))


@pytest.fixture
def db(tmp_path):
    return tmp_path / "상태 폴더" / "state.db"


def test_reopen_gives_same_results(db):
    with SqliteStore(db) as store:
        commit(store, "d1", "가")
        commit(store, "d2", "나")
        commit(store, "d1", "가", "다")
        before = (store.documents(), store.changes_after(None, 10), store.get_tree("d1", 1), store.history("d1"))
    with SqliteStore(db) as store:
        after = (store.documents(), store.changes_after(None, 10), store.get_tree("d1", 1), store.history("d1"))
        assert after == before
        assert commit(store, "d2", "라") == 4  # 커서는 재시작 뒤에도 이어진다


def test_wal_mode_and_format_meta(db):
    with SqliteStore(db):
        pass
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "4"
    finally:
        conn.close()


def test_unknown_format_rejected(db):
    with SqliteStore(db):
        pass
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'format'")
    conn.close()
    with pytest.raises(HanjiError, match="unsupported store format"):
        SqliteStore(db)


@pytest.mark.parametrize("old", ["1", "2", "3"])  # 계약 0.1·0.2·0.3 시절 상태 파일
def test_older_contract_store_asks_to_ingest_again(db, old):
    with SqliteStore(db):
        pass
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (old,))
    conn.close()
    with pytest.raises(HanjiError, match="ingest again"):
        SqliteStore(db)


def test_failure_inside_commit_rolls_back_everything(db):
    with SqliteStore(db) as store:
        commit(store, "d1", "가")
        raw = sqlite3.connect(db)
        with raw:  # 버전 2의 이력 자리를 미리 차지해 커밋 중간(histories 삽입)에서 실패시킨다
            raw.execute("INSERT INTO histories VALUES ('d1', 2, '{}')")
        raw.close()
        with pytest.raises(sqlite3.IntegrityError):
            commit(store, "d1", "나")
        assert store.latest("d1").version == 1
        assert store.changes_after(None, 10).next_cursor == 1
        assert [r.version for r in store.documents()] == [1]
        assert commit(store, "d2", "다") == 2  # 실패한 커밋은 커서 번호를 쓰지 않는다


def test_two_instances_conflict_on_same_version(db):
    with SqliteStore(db) as a, SqliteStore(db) as b:
        commit(a, "d1", "가")
        prev = b.latest("d1")
        mine = make_tree("d1", 2, "나")
        theirs = make_tree("d1", 2, "다")
        a.commit(theirs, diff_trees(prev, theirs), ProcessingHistory(document_id="d1", version=2))
        with pytest.raises(StoreConflict):
            b.commit(mine, diff_trees(prev, mine), ProcessingHistory(document_id="d1", version=2))
        assert b.latest("d1") == theirs


def test_tx_surfaces_original_error_after_sqlite_auto_rollback(db):
    with SqliteStore(db) as store:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            with store._tx("BEGIN IMMEDIATE") as conn:
                conn.execute("ROLLBACK")  # SQLite가 스스로 롤백한 상황을 흉내 낸다
                raise sqlite3.OperationalError("disk I/O error")
        assert commit(store, "d1", "가") == 1  # 저장소는 계속 쓸 수 있다


def test_tx_failed_commit_leaves_no_open_transaction(db):
    with SqliteStore(db) as store:
        store._conn.execute("PRAGMA foreign_keys=ON")
        store._conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        store._conn.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
        with pytest.raises(sqlite3.IntegrityError):
            with store._tx("BEGIN IMMEDIATE") as conn:
                conn.execute("INSERT INTO c VALUES (1)")  # COMMIT 시점에 외래 키 위반으로 실패한다
        assert not store._conn.in_transaction
        assert commit(store, "d1", "가") == 1


def test_assets_table_keeps_one_row_per_hash_and_survives_reopen(db):
    data = b"\x89PNG\r\n\x1a\n-figure-"
    asset = "sha256:" + hashlib.sha256(data).hexdigest()
    figure = {"kind": "figure", "text": "", "confidence": 0.7, "state": "det", "text_source": "native",
              "locator": {"kind": "lines", "line_start": 1, "line_end": 1},
              "figure": {"asset": asset, "mime": "image/png", "width_px": 2, "height_px": 2, "dpi": 72,
                         "category": "chart"}}
    with SqliteStore(db) as store:
        for doc in ("d1", "d2"):
            tree = build_tree(ParsedSource(mime="text/markdown", blocks=[figure]), doc, 1, SOURCE)
            store.commit(tree, diff_trees(None, tree), ProcessingHistory(document_id=doc, version=1), {asset: data})
    with SqliteStore(db) as store:
        assert store.get_asset(asset) == data
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*), MIN(mime) FROM assets").fetchone() == (1, "image/png")
    finally:
        conn.close()


def scenario(tmp_path, source: str, timeout: float = 120) -> object:
    """시나리오 스크립트를 자식 Python으로 돌리고 마지막 줄 JSON을 돌려준다. spawn 자식은 테스트 모듈을 import할 수 없어
    작업 함수를 스크립트에 둔다. 멈추면 시간 초과로 실패한다."""
    script = tmp_path / "scenario.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def lock_error(code: int | None = 5, message: str = "database is locked") -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(message)
    if code is not None:
        exc.sqlite_errorcode = code
    return exc


def test_open_waits_while_another_connection_holds_a_new_state_file(db):
    """다른 연결이 새 상태 파일의 쓰기 잠금을 쥐고 있으면 WAL 전환이 바쁨 대기 없이 바로 잠금 오류를 낸다. 여는 쪽은
    잠금이 놓일 때까지 다시 해서 연다."""
    db.parent.mkdir(parents=True)
    holder = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.5, lambda: holder.execute("ROLLBACK"))
    release.start()
    try:
        with SqliteStore(db) as store:
            assert store.documents() == ()
    finally:
        release.join()
        holder.close()


def test_eight_processes_open_a_new_state_file_together(tmp_path):
    """장벽으로 맞춘 spawn 프로세스 8개가 새 상태 파일을 동시에 연다(3라운드). 모두 연다."""
    outcomes = scenario(tmp_path, r"""
        import json, multiprocessing as mp, os, sys


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
            for round_ in range(3):
                path = os.path.join(os.getcwd(), f"race{round_}", "state.db")
                barrier, queue = ctx.Barrier(8), ctx.Queue()
                procs = [ctx.Process(target=open_store, args=(path, barrier, queue)) for _ in range(8)]
                try:
                    for p in procs:
                        p.start()
                    outcomes += [queue.get(timeout=60) for _ in procs]
                except BaseException:  # 실패해도 자식을 남기지 않는다
                    for p in procs:
                        if p.is_alive():
                            p.terminate()
                    raise
                finally:
                    for p in procs:
                        if p.pid is not None:
                            p.join()
            print(json.dumps(outcomes))
    """)
    assert outcomes == ["ok"] * 24


def test_retry_locked_pauses_longer_each_time_then_succeeds(monkeypatch):
    pauses, results = [], [lock_error(), lock_error(6), lock_error(), lock_error(), lock_error(), lock_error(), "done"]
    monkeypatch.setattr(sqlite_store, "sleep", pauses.append)

    def call():
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    assert sqlite_store._retry_locked(call) == "done"
    assert pauses == [0.05, 0.1, 0.2, 0.4, 0.8, 0.8]


class FakeClock:
    """가짜 시계: sleep은 시간을 그만큼 앞으로 민다."""

    def __init__(self, monkeypatch):
        self.now, self.pauses = 0.0, []
        monkeypatch.setattr(sqlite_store, "monotonic", lambda: self.now)
        monkeypatch.setattr(sqlite_store, "sleep", self.sleep)

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


def test_retry_locked_keeps_one_deadline_for_busy_waits_and_pauses(monkeypatch):
    """처음 호출부터 120초가 한 마감이다. 시도 앞마다 남은 시간을 보고, 남은 시간이 바쁨 대기(60초)보다 짧으면 그
    연결의 busy_timeout을 남은 시간으로 줄인다. 마감에 닿으면 마지막 잠금 오류를 던지고 busy_timeout을 되돌린다
    (예전 코드는 실패 뒤에만 시간을 봐서 179초 넘게 걸렸다)."""
    clock = FakeClock(monkeypatch)

    class Conn:
        busy = sqlite_store.BUSY_TIMEOUT
        settings = []

        def execute(self, sql):
            assert sql.startswith("PRAGMA busy_timeout = ")
            self.busy = int(sql.rsplit(" ", 1)[1]) / 1000
            self.settings.append(self.busy)

    conn, errors = Conn(), []

    def call():
        clock.now += conn.busy  # 바쁨 대기를 끝까지 기다린 뒤 잠금 오류
        errors.append(lock_error())
        raise errors[-1]

    with pytest.raises(sqlite3.OperationalError) as caught:
        sqlite_store._retry_locked(call, conn)
    assert caught.value is errors[-1] and len(errors) == 2
    assert clock.now == pytest.approx(120.0) and clock.pauses == [0.05]
    assert conn.settings == [pytest.approx(59.95), sqlite_store.BUSY_TIMEOUT]


def test_retry_locked_cuts_the_last_pause_to_the_deadline_and_stops(monkeypatch):
    """마감 직전 실패: 쉬는 시간은 남은 시간까지만이고, 쉰 뒤 남은 시간이 없으면 다시 하지 않는다."""
    clock = FakeClock(monkeypatch)
    errors = []

    def call():
        clock.now += 119.96875  # 남은 0.03125초 < 첫 쉼 0.05초
        errors.append(lock_error())
        raise errors[-1]

    with pytest.raises(sqlite3.OperationalError) as caught:
        sqlite_store._retry_locked(call)
    assert caught.value is errors[0] and len(errors) == 1
    assert clock.pauses == [0.03125] and clock.now == 120.0


def test_retry_locked_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(sqlite_store, "sleep", lambda seconds: pytest.fail("must not retry"))
    calls = []

    def call():
        calls.append(1)
        raise lock_error(1, "no such table: versions")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        sqlite_store._retry_locked(call)
    assert calls == [1]


@pytest.mark.parametrize(("code", "message", "locked"), [
    (5, "x", True), (6, "x", True), (261, "x", True), (517, "x", True), (1, "database is locked", False),
    (None, "database is locked", True), (None, "database is busy", True), (None, "disk I/O error", False),
])
def test_lock_errors_are_told_by_the_sqlite_code_first(code, message, locked):
    """SQLite 오류 코드의 아래 8비트가 BUSY(5)·LOCKED(6)이면 잠금 오류(확장 코드 261·517 포함). 코드가 없을 때만 문구로 본다."""
    assert sqlite_store._locked(lock_error(code, message)) is locked


def test_commit_retried_after_a_lock_error_writes_each_row_once(db, monkeypatch):
    """커밋 트랜잭션이 도중에 잠금 오류로 롤백된 뒤 다시 해서 성공해도 버전·변경 로그·처리 이력·그림 이미지는 한 번씩."""
    data = b"\x89PNG\r\n\x1a\n-figure-"
    asset = "sha256:" + hashlib.sha256(data).hexdigest()
    figure = {"kind": "figure", "text": "", "confidence": 0.7, "state": "det", "text_source": "native",
              "locator": {"kind": "lines", "line_start": 1, "line_end": 1},
              "figure": {"asset": asset, "mime": "image/png", "width_px": 2, "height_px": 2, "dpi": 72,
                         "category": "chart"}}
    monkeypatch.setattr(sqlite_store, "sleep", lambda seconds: None)

    class Flaky:  # histories 행을 넣을 때 한 번 잠금 오류
        def __init__(self, conn):
            self.conn, self.failed = conn, False

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO histories") and not self.failed:
                self.failed = True
                raise lock_error()
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    with SqliteStore(db) as store:
        store._conn = Flaky(store._conn)
        tree = build_tree(ParsedSource(mime="text/markdown", blocks=[figure]), "d1", 1, SOURCE)
        assert store.commit(tree, diff_trees(None, tree), ProcessingHistory(document_id="d1", version=1),
                            {asset: data}) == 1
        assert store._conn.failed
        store._conn = store._conn.conn
    conn = sqlite3.connect(db)
    try:
        counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("documents", "versions", "change_log", "histories", "assets")]
    finally:
        conn.close()
    assert counts == [1, 1, 1, 1, 1]


def test_two_processes_ingesting_the_same_file_make_one_version(tmp_path):
    """두 프로세스가 같은 내용을 동시에 수집하면 하나는 새 버전, 하나는 같은 원본(StoreConflict를 한 번 다시 해서)이고
    변경 로그는 한 줄이다."""
    result = scenario(tmp_path, r"""
        import json, multiprocessing as mp, os, sqlite3
        from pathlib import Path


        def ingest(path, db, barrier, queue):
            from hanji import LocalEngine, SqliteStore
            from hanji.formats.detect import default_parsers
            with SqliteStore(db) as store:
                engine = LocalEngine(store, default_parsers(ocr=False, layout=False))
                data = Path(path).read_bytes()
                barrier.wait()
                queue.put(engine._ingest_outcome(data, "같은.md").created)


        if __name__ == "__main__":
            ctx = mp.get_context("spawn")
            path, db = os.path.join(os.getcwd(), "같은.md"), os.path.join(os.getcwd(), "state.db")
            Path(path).write_text("# 지어낸 제목\n\n지어낸 문단\n", encoding="utf-8")
            barrier, queue = ctx.Barrier(2), ctx.Queue()
            procs = [ctx.Process(target=ingest, args=(path, db, barrier, queue)) for _ in range(2)]
            try:
                for p in procs:
                    p.start()
                created = sorted(queue.get(timeout=60) for _ in procs)
            except BaseException:  # 실패해도 자식을 남기지 않는다
                for p in procs:
                    if p.is_alive():
                        p.terminate()
                raise
            finally:
                for p in procs:
                    if p.pid is not None:
                        p.join()
            conn = sqlite3.connect(db)
            rows = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("versions", "change_log")]
            conn.close()
            print(json.dumps([created, rows]))
    """)
    assert result == [[False, True], [1, 1]]
