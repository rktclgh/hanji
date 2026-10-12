"""SQLite 상태 파일 저장소(CLI 기본). 버전 스냅숏을 검증된 계약 JSON으로 통째 저장한다(D4). 여러 프로세스가 같은 상태
파일에 쓸 수 있다(ingest_many 작업 프로세스): 잠금 오류는 기다렸다 다시 한다(_retry_locked)."""

import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from time import monotonic, sleep
from types import TracebackType
from typing import Any, Self

from platformdirs import user_data_dir

from hanji_contracts import ChangeBatch, DocRef, DocumentChange, DocumentTree, ProcessingHistory

from ..errors import AssetNotFound, DocumentNotFound, HanjiError, VersionNotFound
from .base import NO_ASSETS, check_commit, check_query, make_batch

DB_ENV = "HANJI_DB"
BUSY_TIMEOUT = 60.0  # 쓰기 잠금을 기다리는 초(그림이 많은 커밋은 잠금을 몇 초 쥘 수 있다)
RETRY_LIMIT = 120.0  # 잠금 오류를 다시 하는 총 초(처음 호출부터)
RETRY_PAUSES = (0.05, 0.1, 0.2, 0.4, 0.8)  # 다시 하기 전 쉬는 초(마지막 값을 되풀이)
_LOCK_CODES = (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED(확장 코드는 아래 8비트가 같다). 6은 같은 연결의 "table is locked"도 덮어
# 기다려도 풀리지 않는 경우가 있다(저장소 경로에 DROP·ALTER가 없어 지금은 문제 없다)
_LOCK_MESSAGES = ("database is locked", "database is busy")
FORMAT_VERSION = "4"  # 4 = 계약 0.4 스키마(쪽 글자 장부). 다른 형식은 다시 수집해야 한다
_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS documents (document_id TEXT PRIMARY KEY, current_version INTEGER NOT NULL,"
    " layer_state TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS versions (document_id TEXT NOT NULL, version INTEGER NOT NULL,"
    " tree_json TEXT NOT NULL, PRIMARY KEY (document_id, version))",
    "CREATE TABLE IF NOT EXISTS change_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,"
    " version INTEGER NOT NULL, change_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS histories (document_id TEXT NOT NULL, version INTEGER NOT NULL,"
    " history_json TEXT NOT NULL, PRIMARY KEY (document_id, version))",
    "CREATE TABLE IF NOT EXISTS assets (sha TEXT PRIMARY KEY, mime TEXT NOT NULL, bytes BLOB NOT NULL)",
)


def resolve_db(option: str | os.PathLike[str] | None) -> Path:
    """상태 파일 경로: 주어진 값 > 환경변수 HANJI_DB > 사용자 데이터 폴더/hanji/state.db(CLI와 ingest_many가 같이 쓴다)."""
    if option:
        return Path(option)
    env = os.environ.get(DB_ENV)
    if env:
        return Path(env)
    return Path(user_data_dir("hanji", appauthor=False)) / "state.db"


def _locked(exc: sqlite3.OperationalError) -> bool:
    """잠금 오류인가: SQLite 오류 코드(아래 8비트)가 BUSY·LOCKED, 코드가 없으면 문구."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in _LOCK_CODES
    return any(text in str(exc) for text in _LOCK_MESSAGES)


def _busy(conn: Any, seconds: float) -> None:
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(seconds * 1000))}")


def _retry_locked[T](fn: Callable[[], T], conn: Any = None) -> T:
    """fn을 부르고, 잠금 오류면 쉬었다 다시 한다. 마감은 처음 호출부터 RETRY_LIMIT초 하나다: 실패하면 남은 시간을 보고
    (없으면 원래 오류), 쉬는 시간은 남은 시간까지, 쉰 뒤 남은 시간이 없으면 원래 오류. 남은 시간이 BUSY_TIMEOUT보다
    짧으면 다음 시도 동안 conn의 busy_timeout을 남은 시간으로 줄이고 끝나면 되돌린다. 다른 오류는 그대로. fn은 다시
    불러도 되는 것(롤백된 트랜잭션 전체, PRAGMA)만 넘긴다."""
    deadline = monotonic() + RETRY_LIMIT
    attempt, shortened = 0, False
    try:
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not _locked(exc):
                    raise
                left = deadline - monotonic()
                if left > 0:
                    sleep(min(RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)], left))
                    left = deadline - monotonic()
                if left <= 0:
                    raise
            attempt += 1
            if conn is not None and left < BUSY_TIMEOUT:
                _busy(conn, left)
                shortened = True
    finally:
        if shortened:
            with suppress(sqlite3.Error):  # 되돌리기가 실패해도 원래 오류를 가리지 않는다
                _busy(conn, BUSY_TIMEOUT)


class SqliteStore:
    """WAL 모드, 쓰기는 BEGIN IMMEDIATE. 읽을 때 계약 모델로 검증한다."""

    def __init__(self, path: str | Path) -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db, isolation_level=None, timeout=BUSY_TIMEOUT)
        try:
            # WAL 전환은 바쁨 대기를 타지 않는다(여러 프로세스가 새 상태 파일을 함께 열면 바로 잠금 오류)
            _retry_locked(lambda: self._conn.execute("PRAGMA journal_mode=WAL"), self._conn)
            found = _retry_locked(self._create_tables, self._conn)
            if found != FORMAT_VERSION:
                raise HanjiError(f"unsupported store format {found!r} in {db} (expected {FORMAT_VERSION!r});"
                                    " delete the state file and ingest again")
        except BaseException:
            self._conn.close()
            raise

    def _create_tables(self) -> str:
        """표를 만들고(이미 있으면 그대로) 저장된 형식 판을 돌려준다."""
        with self._tx("BEGIN IMMEDIATE") as conn:
            for statement in _TABLES:
                conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('format', ?)", (FORMAT_VERSION,))
            (found,) = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        return found

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    @contextmanager
    def _tx(self, begin: str = "BEGIN") -> Iterator[sqlite3.Connection]:
        self._conn.execute(begin)
        try:
            yield self._conn
        except BaseException:
            self._rollback_if_active()  # SQLite가 이미 롤백했으면 원래 오류가 가려지지 않게 건너뛴다
            raise
        try:
            self._conn.execute("COMMIT")
        except BaseException:
            self._rollback_if_active()  # COMMIT 실패 뒤에도 트랜잭션을 남기지 않는다
            raise

    def _rollback_if_active(self) -> None:
        if self._conn.in_transaction:
            with suppress(sqlite3.Error):  # 정리 중 오류가 원래 오류를 덮지 않게 한다
                self._conn.execute("ROLLBACK")

    def latest(self, document_id: str) -> DocumentTree | None:
        row = self._conn.execute(
            "SELECT tree_json FROM versions WHERE document_id = ? ORDER BY version DESC LIMIT 1",
            (document_id,)).fetchone()
        return None if row is None else DocumentTree.model_validate_json(row[0])

    def get_tree(self, document_id: str, version: int | None = None) -> DocumentTree:
        return DocumentTree.model_validate_json(self._fetch("versions", "tree_json", document_id, version))

    def documents(self) -> tuple[DocRef, ...]:
        rows = self._conn.execute(
            "SELECT document_id, current_version, layer_state FROM documents ORDER BY document_id").fetchall()
        return tuple(DocRef(document_id=d, version=v, layer_state=s) for d, v, s in rows)

    def commit(self, tree: DocumentTree, change: DocumentChange, history: ProcessingHistory,
               assets: Mapping[str, bytes] = NO_ASSETS) -> int:
        """트랜잭션 하나. 잠금 오류로 롤백되면 트랜잭션 전체를 다시 한다(_retry_locked: 행이 겹치지 않는다)."""
        return _retry_locked(lambda: self._commit(tree, change, history, assets), self._conn)

    def _commit(self, tree: DocumentTree, change: DocumentChange, history: ProcessingHistory,
                assets: Mapping[str, bytes]) -> int:
        with self._tx("BEGIN IMMEDIATE") as conn:
            row = conn.execute("SELECT current_version FROM documents WHERE document_id = ?",
                               (tree.document_id,)).fetchone()
            mimes = check_commit(tree, change, history, None if row is None else row[0], assets,
                                 lambda asset: self._stored_size(conn, asset))
            conn.executemany("INSERT OR IGNORE INTO assets (sha, mime, bytes) VALUES (?, ?, ?)",
                             [(key, mimes[key], sqlite3.Binary(data)) for key, data in assets.items()])
            conn.execute("INSERT INTO versions (document_id, version, tree_json) VALUES (?, ?, ?)",
                         (tree.document_id, tree.version, tree.model_dump_json()))
            seq = conn.execute("INSERT INTO change_log (document_id, version, change_json) VALUES (?, ?, ?)",
                               (tree.document_id, tree.version, change.model_dump_json())).lastrowid
            conn.execute("INSERT INTO histories (document_id, version, history_json) VALUES (?, ?, ?)",
                         (tree.document_id, tree.version, history.model_dump_json()))
            conn.execute(
                "INSERT INTO documents (document_id, current_version, layer_state) VALUES (?, ?, ?)"
                " ON CONFLICT(document_id) DO UPDATE SET current_version = excluded.current_version,"
                " layer_state = excluded.layer_state",
                (tree.document_id, tree.version, tree.layer_state))
        assert seq is not None
        return seq

    @staticmethod
    def _stored_size(conn: sqlite3.Connection, asset: str) -> int | None:
        """이미 저장된 이미지의 바이트 수(없으면 None)."""
        row = conn.execute("SELECT length(bytes) FROM assets WHERE sha = ?", (asset,)).fetchone()
        return None if row is None else row[0]

    def get_asset(self, asset: str) -> bytes:
        row = self._conn.execute("SELECT bytes FROM assets WHERE sha = ?", (asset,)).fetchone()
        if row is None:
            raise AssetNotFound(asset)
        return bytes(row[0])

    def changes_after(self, cursor: int | None, limit: int) -> ChangeBatch:
        check_query(cursor, limit)
        with self._tx() as conn:  # 같은 스냅숏에서 최신 번호와 로그를 읽는다
            (last_seq,) = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM change_log").fetchone()
            rows = conn.execute("SELECT seq, change_json FROM change_log WHERE seq > ? ORDER BY seq LIMIT ?",
                                (cursor or 0, limit)).fetchall()
        return make_batch(cursor, last_seq, [(seq, DocumentChange.model_validate_json(raw)) for seq, raw in rows])

    def history(self, document_id: str, version: int | None = None) -> ProcessingHistory:
        return ProcessingHistory.model_validate_json(self._fetch("histories", "history_json", document_id, version))

    def _fetch(self, table: str, column: str, document_id: str, version: int | None) -> str:
        """table·column은 이 모듈의 상수만 넘긴다."""
        with self._tx() as conn:
            found = conn.execute("SELECT current_version FROM documents WHERE document_id = ?",
                                 (document_id,)).fetchone()
            if found is None:
                raise DocumentNotFound(document_id)
            row = conn.execute(f"SELECT {column} FROM {table} WHERE document_id = ? AND version = ?",
                               (document_id, found[0] if version is None else version)).fetchone()
        if row is None:
            raise VersionNotFound(f"{document_id} v{version}")
        return row[0]
