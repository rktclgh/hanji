"""계약 Engine의 로컬 구현. ingest는 동기(D6), VLM 경로는 없다."""

import hashlib
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from hanji_contracts import (
    ChangeBatch, DocFilter, DocRef, DocumentTree, JobRef, JobStatus, ProcessingHistory, SourceInfo,
)

from .core import build_tree, check_assets, diff_trees
from .errors import StoreConflict, VlmUnavailable
from .formats.base import Parser
from .formats.detect import default_parsers, detect_parser
from .store.base import Store


def _ref(tree: DocumentTree) -> DocRef:
    return DocRef(document_id=tree.document_id, version=tree.version, layer_state=tree.layer_state)


class Outcome(NamedTuple):
    """수집 한 번의 결과: 저장된 최신 버전, 이번에 새 버전을 저장했는가, 그 버전의 쪽 수(PDF, 그 밖은 None)."""

    ref: DocRef
    created: bool
    pages: int | None


def _kept(tree: DocumentTree) -> Outcome:
    return Outcome(_ref(tree), False, tree.source.page_count)


class LocalEngine:
    def __init__(self, store: Store, parsers: Sequence[Parser] | None = None) -> None:
        self._store = store
        self._parsers = default_parsers() if parsers is None else tuple(parsers)

    def ingest(self, path: str, document_id: str | None = None, force: bool = False) -> DocRef:
        """document_id가 없으면 doc_ + 원본 sha256 앞 24자리(D1). 원본이 같으면 force가 아닌 한 그대로(D2)."""
        file = Path(path)
        return self.ingest_bytes(file.read_bytes(), file.name, document_id, force)  # 전체 경로는 저장하지 않는다

    def ingest_bytes(self, data: bytes, name: str, document_id: str | None = None, force: bool = False) -> DocRef:
        """ingest와 같되 원본 바이트와 파일 이름을 직접 받는다(이름은 NFC로 저장)."""
        return self._ingest_outcome(data, name, document_id, force).ref

    def _ingest_outcome(self, data: bytes, name: str, document_id: str | None = None,
                        force: bool = False) -> Outcome:
        """ingest_bytes와 같은 경로. 새 버전을 저장했는지와 쪽 수를 함께 돌려준다(ingest_many 작업 프로세스가 쓴다)."""
        name = unicodedata.normalize("NFC", name)
        digest = hashlib.sha256(data).hexdigest()
        doc_id = "doc_" + digest[:24] if document_id is None else document_id
        if not doc_id:
            raise ValueError("document_id must not be empty")
        try:
            return self._ingest_once(data, name, doc_id, "sha256:" + digest, force)
        except StoreConflict:  # 다른 쓰기와 겹쳤다: 최신 버전을 다시 읽고 한 번만 재시도
            return self._ingest_once(data, name, doc_id, "sha256:" + digest, force)

    def _ingest_once(self, data: bytes, name: str, doc_id: str, content_hash: str, force: bool) -> Outcome:
        latest = self._store.latest(doc_id)
        if latest is not None and latest.source.content_hash == content_hash and not force:
            return _kept(latest)
        parsed = detect_parser(name, self._parsers).parse(data, name)  # 형식 판별은 원본이 달라진 뒤에만
        source = SourceInfo(name=name, mime=parsed.mime, content_hash=content_hash,
                            page_count=len(parsed.pages) or None)
        tree = build_tree(parsed, doc_id, 1 if latest is None else latest.version + 1, source)
        check_assets(tree, parsed.assets)  # 그림 참조와 이미지가 어긋나면(파서 버그) 저장하지 않는다
        change = diff_trees(latest, tree)
        if change is None:  # 원본은 달라도 블록과 쪽 정보가 같다
            assert latest is not None
            return _kept(latest)
        history = ProcessingHistory(document_id=doc_id, version=tree.version, regions=parsed.regions)
        if parsed.assets:
            self._store.commit(tree, change, history, parsed.assets)
        else:  # 이미지가 없으면 0.2와 같은 모양으로 부른다(이미지를 모르는 저장소 구현도 글자 문서는 받는다)
            self._store.commit(tree, change, history)
        return Outcome(_ref(tree), True, tree.source.page_count)

    def get_asset(self, asset: str) -> bytes:
        """그림 블록 figure.asset의 이미지 바이트(PNG). 없으면 AssetNotFound."""
        return self._store.get_asset(asset)

    def documents(self) -> tuple[DocRef, ...]:
        return self._store.documents()

    def get_tree(self, document_id: str, version: int | None = None) -> DocumentTree:
        return self._store.get_tree(document_id, version)

    def run_vlm(self, target: DocFilter) -> JobRef:
        raise VlmUnavailable("VLM layer is not available in this engine")

    def job(self, job_id: str) -> JobStatus:
        raise VlmUnavailable("VLM layer is not available in this engine")

    def changes(self, cursor: int | None, limit: int = 100) -> ChangeBatch:
        return self._store.changes_after(cursor, limit)

    def history(self, document_id: str, version: int | None = None) -> ProcessingHistory:
        return self._store.history(document_id, version)
