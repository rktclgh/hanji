"""SQLite 상태 파일 저장소(CLI 기본). 버전 스냅숏을 검증된 계약 JSON으로 통째 저장한다(D4)."""

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from types import TracebackType
from typing import Self

from platformdirs import user_data_dir

from hanji_contracts import ChangeBatch, DocRef, DocumentChange, DocumentTree, ProcessingHistory

from ..errors import AssetNotFound, DocumentNotFound, HanjiError, VersionNotFound
from .base import NO_ASSETS, check_commit, check_query, make_batch

DB_ENV = "HANJI_DB"
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


class SqliteStore:
    """WAL 모드, 쓰기는 BEGIN IMMEDIATE. 읽을 때 계약 모델로 검증한다."""

    def __init__(self, path: str | Path) -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db, isolation_level=None, timeout=5.0)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            with self._tx("BEGIN IMMEDIATE") as conn:
                for statement in _TABLES:
                    conn.execute(statement)
                conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('format', ?)", (FORMAT_VERSION,))
                (found,) = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            if found != FORMAT_VERSION:
                raise HanjiError(f"unsupported store format {found!r} in {db} (expected {FORMAT_VERSION!r});"
                                    " delete the state file and ingest again")
        except BaseException:
            self._conn.close()
            raise

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
