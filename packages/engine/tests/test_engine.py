import hashlib
import inspect
import unicodedata
from pathlib import Path

import pytest

from hanji import LocalEngine, MemoryStore, SqliteStore
from hanji.core import build_tree, diff_trees
from hanji.errors import AssetNotFound, ParseError, StoreConflict, UnsupportedFormat, VlmUnavailable
from hanji.formats.base import ParsedSource
from hanji.formats.detect import default_parsers
from hanji_contracts import Attempt, DocFilter, Engine, ProcessingHistory, RegionRecord, SourceInfo


class FakeParser:
    """테스트용: 비어 있지 않은 줄마다 문단 하나. '!fail' 줄이 있으면 ParseError."""

    mimes = ("text/x-fake",)
    extensions = (".fake",)

    def __init__(self, suffix: str = "") -> None:
        self.calls = 0
        self.suffix = suffix  # 파서 버전이 바뀐 것을 흉내 낸다

    def parse(self, data: bytes, name: str) -> ParsedSource:
        self.calls += 1
        blocks = []
        for i, line in enumerate(data.decode("utf-8").split("\n"), 1):
            if line == "!fail":
                raise ParseError("fake failure", f"{name}:{i}")
            if line.strip():
                blocks.append({"kind": "paragraph", "text": line + self.suffix, "confidence": 1.0, "state": "det",
                               "text_source": "native",
                               "locator": {"kind": "lines", "line_start": i, "line_end": i}})
        return ParsedSource(mime="text/x-fake", blocks=blocks)


@pytest.fixture
def parser():
    return FakeParser()


@pytest.fixture
def engine(parser):
    return LocalEngine(MemoryStore(), parsers=[parser])


def write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_signature_matches_contract():
    ours = inspect.signature(LocalEngine.ingest).parameters
    theirs = inspect.signature(Engine.ingest).parameters
    assert [(p.name, p.default) for p in ours.values()] == [(p.name, p.default) for p in theirs.values()]
    assert {"ingest", "documents", "get_tree", "run_vlm", "job", "changes", "history"} <= set(dir(LocalEngine))


def test_first_ingest_creates_version_one(engine, tmp_path):
    path = write(tmp_path / "보고서.fake", "가\n나\n")
    ref = engine.ingest(str(path))
    data = path.read_bytes()
    assert ref.document_id == "doc_" + hashlib.sha256(data).hexdigest()[:24]
    assert (ref.version, ref.layer_state) == (1, "det")
    tree = engine.get_tree(ref.document_id)
    assert [b.text for b in tree.blocks] == ["가", "나"]
    assert tree.source.name == "보고서.fake" and tree.source.mime == "text/x-fake"
    assert tree.source.content_hash == "sha256:" + hashlib.sha256(data).hexdigest()
    assert tree.source.page_count is None
    assert engine.history(ref.document_id).regions == ()


def test_default_id_is_deterministic_across_paths(engine, tmp_path):
    a = engine.ingest(str(write(tmp_path / "a" / "x.fake", "같은 내용\n")))
    b = engine.ingest(str(write(tmp_path / "b" / "다른 이름.fake", "같은 내용\n")))
    assert a == b and len(engine.documents()) == 1


def test_same_file_again_makes_no_version(engine, parser, tmp_path):
    path = write(tmp_path / "x.fake", "가\n")
    first = engine.ingest(str(path), document_id="문서1")
    again = engine.ingest(str(path), document_id="문서1")
    assert again == first and parser.calls == 1  # 원본 해시가 같으면 파싱하지 않는다
    assert len(engine.changes(None).changes) == 1


def test_modified_file_gives_exact_change_and_keeps_ids(engine, tmp_path):
    path = write(tmp_path / "x.fake", "가\n나\n다\n")
    engine.ingest(str(path), document_id="d")
    v1 = engine.get_tree("d")
    write(path, "가\n\n다\n라\n")  # 나 삭제(빈 줄로 줄 번호 유지), 라 추가
    ref = engine.ingest(str(path), document_id="d")
    v2 = engine.get_tree("d")
    assert ref.version == 2
    change = engine.changes(1).changes[0]
    by_text = {b.text: b.block_id for b in v1.blocks}
    assert change.removed == (by_text["나"],)
    assert change.added == (v2.blocks[2].block_id,)
    assert change.updated == (by_text["다"],)  # order가 2 → 1
    assert v2.blocks[0].block_id == by_text["가"] and v2.blocks[1].block_id == by_text["다"]


def test_changed_source_with_same_blocks_makes_no_version(engine, parser, tmp_path):
    path = write(tmp_path / "x.fake", "가\n")
    engine.ingest(str(path), document_id="d")
    write(path, "가\n\n\n")  # 바이트는 다르지만 블록은 같다
    assert engine.ingest(str(path), document_id="d").version == 1 and parser.calls == 2


def test_force_reparses_and_versions_only_on_difference(tmp_path):
    store = MemoryStore()
    path = write(tmp_path / "x.fake", "가\n")
    old = FakeParser()
    LocalEngine(store, parsers=[old]).ingest(str(path), document_id="d")
    assert LocalEngine(store, parsers=[old]).ingest(str(path), document_id="d", force=True).version == 1
    assert old.calls == 2
    upgraded = LocalEngine(store, parsers=[FakeParser(suffix=".")])
    assert upgraded.ingest(str(path), document_id="d").version == 1  # force 없이는 재파싱하지 않는다
    assert upgraded.ingest(str(path), document_id="d", force=True).version == 2


def test_change_feed_mixes_documents_in_order(engine, tmp_path):
    a = write(tmp_path / "a.fake", "가\n")
    b = write(tmp_path / "b.fake", "나\n")
    engine.ingest(str(a), document_id="A")
    engine.ingest(str(b), document_id="B")
    write(a, "가\n다\n")
    engine.ingest(str(a), document_id="A")
    first = engine.changes(None, limit=2)
    assert [(c.document_id, c.version) for c in first.changes] == [("A", 1), ("B", 1)]
    rest = engine.changes(first.next_cursor)
    assert [(c.document_id, c.version, c.previous_version) for c in rest.changes] == [("A", 2, 1)]
    assert engine.changes(rest.next_cursor).changes == ()


def test_parse_failure_leaves_store_untouched(engine, tmp_path):
    path = write(tmp_path / "x.fake", "가\n")
    engine.ingest(str(path), document_id="d")
    write(path, "가\n!fail\n")
    with pytest.raises(ParseError) as info:
        engine.ingest(str(path), document_id="d")
    assert info.value.location == "x.fake:2"
    assert engine.get_tree("d").version == 1 and engine.changes(None).next_cursor == 1


def test_unsupported_format(engine, tmp_path):
    with pytest.raises(UnsupportedFormat):
        engine.ingest(str(write(tmp_path / "x.txt", "가\n")))
    with pytest.raises(UnsupportedFormat):
        engine.ingest(str(write(tmp_path / "noext", "가\n")))
    assert engine.documents() == ()


def test_unchanged_source_shortcut_runs_before_format_detection(tmp_path):
    store = MemoryStore()
    LocalEngine(store, parsers=[FakeParser()]).ingest(str(write(tmp_path / "x.fake", "가\n")), document_id="d")
    bare = LocalEngine(store, parsers=[])  # 어떤 형식도 모른다
    same = bare.ingest(str(tmp_path / "x.fake"), document_id="d")
    renamed = bare.ingest(str(write(tmp_path / "y.unknown", "가\n")), document_id="d")
    assert same == renamed and same.version == 1
    before = store.changes_after(None, 10)
    with pytest.raises(UnsupportedFormat):
        bare.ingest(str(write(tmp_path / "z.unknown", "나\n")), document_id="d")
    with pytest.raises(UnsupportedFormat):
        bare.ingest(str(tmp_path / "x.fake"), document_id="d", force=True)
    assert store.changes_after(None, 10) == before and store.latest("d").version == 1


def test_extension_match_is_case_insensitive(engine, tmp_path):
    assert engine.ingest(str(write(tmp_path / "X.FAKE", "가\n"))).version == 1


def test_empty_document_id_rejected(engine, tmp_path):
    with pytest.raises(ValueError):
        engine.ingest(str(write(tmp_path / "x.fake", "가\n")), document_id="")


def test_source_name_is_nfc_file_name_only(engine, tmp_path):
    nfd = unicodedata.normalize("NFD", "보고서.fake")
    path = write(tmp_path / "한글 폴더" / nfd, "가\n")
    tree = engine.get_tree(engine.ingest(str(path)).document_id)
    assert tree.source.name == "보고서.fake"
    assert tmp_path.name not in tree.model_dump_json()  # 전체 경로는 저장하지 않는다


def test_ingest_bytes_matches_ingest(parser, tmp_path):
    """바이트로 넣어도 같은 이름·바이트면 파일 경로로 넣은 것과 같은 DocRef와 트리(이름은 NFC)."""
    path = write(tmp_path / "보고서.fake", "가\n나\n")
    by_path, by_bytes = LocalEngine(MemoryStore(), parsers=[parser]), LocalEngine(MemoryStore(), parsers=[parser])
    ref = by_path.ingest(str(path))
    assert by_bytes.ingest_bytes(path.read_bytes(), unicodedata.normalize("NFD", "보고서.fake")) == ref
    assert by_bytes.get_tree(ref.document_id) == by_path.get_tree(ref.document_id)
    assert by_bytes.ingest_bytes(path.read_bytes(), "보고서.fake") == ref  # 원본이 같으면 새 버전 없음
    assert by_bytes.ingest_bytes(b"x\n", "x.fake", document_id="d").document_id == "d"
    with pytest.raises(ValueError):
        by_bytes.ingest_bytes(b"x\n", "x.fake", document_id="")


def test_vlm_paths_unavailable(engine):
    with pytest.raises(VlmUnavailable):
        engine.run_vlm(DocFilter())
    with pytest.raises(VlmUnavailable):
        engine.job("j1")


class ConflictOnce(MemoryStore):
    """첫 커밋 직전에 다른 쓰기가 같은 문서의 버전 1을 먼저 넣은 상황을 흉내 낸다."""

    def __init__(self) -> None:
        super().__init__()
        self.conflicts = 0
        self.raced = False

    def commit(self, tree, change, history):
        if not self.raced:
            self.raced = True
            rival = build_tree(FakeParser().parse("경쟁\n".encode("utf-8"), "r.fake"), tree.document_id, 1,
                               SourceInfo(name="r.fake", mime="text/x-fake", content_hash="sha256:" + "1" * 64))
            super().commit(rival, diff_trees(None, rival),
                           ProcessingHistory(document_id=tree.document_id, version=1))
        try:
            return super().commit(tree, change, history)
        except StoreConflict:
            self.conflicts += 1
            raise


def test_store_conflict_is_retried_once(tmp_path):
    store = ConflictOnce()
    engine = LocalEngine(store, parsers=[FakeParser()])
    ref = engine.ingest(str(write(tmp_path / "x.fake", "가\n")), document_id="d")
    assert store.conflicts == 1 and ref.version == 2  # 최신(경쟁 버전)을 다시 읽고 그다음 버전으로
    assert [(c.version, c.previous_version) for c in engine.changes(None).changes] == [(1, None), (2, 1)]
    assert [b.text for b in engine.get_tree("d").blocks] == ["가"]


def test_store_conflict_twice_propagates(tmp_path):
    class AlwaysConflict(MemoryStore):
        def commit(self, tree, change, history):
            raise StoreConflict("busy")

    engine = LocalEngine(AlwaysConflict(), parsers=[FakeParser()])
    with pytest.raises(StoreConflict):
        engine.ingest(str(write(tmp_path / "x.fake", "가\n")))


def test_reingest_against_sqlite_keeps_unchanged_blocks_out_of_updated(tmp_path):
    path = write(tmp_path / "x.fake", "가\n나\n")
    with SqliteStore(tmp_path / "state.db") as store:
        engine = LocalEngine(store, parsers=[FakeParser()])
        engine.ingest(str(path), document_id="d")
        write(path, "가\n나\n다\n")
        engine.ingest(str(path), document_id="d")
        change = engine.changes(1).changes[0]
        assert change.updated == () and len(change.added) == 1  # JSON 왕복한 블록도 같다고 본다


PNG = b"\x89PNG\r\n\x1a\n-fake-"
PNG_ASSET = "sha256:" + hashlib.sha256(PNG).hexdigest()
EXTRA = b"other"
EXTRA_ASSET = "sha256:" + hashlib.sha256(EXTRA).hexdigest()


class FigureParser:
    """테스트용: 그림 블록 하나(글자 = 파일 내용) + 캡션 블록 하나와 그 이미지. caption이 참이면 짝짓는다."""

    mimes = ("text/x-fig",)
    extensions = (".fig",)

    def __init__(self, assets=None, caption: bool = True, regions=()) -> None:
        self.assets = {PNG_ASSET: PNG} if assets is None else assets
        self.caption = caption
        self.regions = regions

    def parse(self, data: bytes, name: str) -> ParsedSource:
        loc = {"kind": "lines", "line_start": 1, "line_end": 1}
        figure = {"asset": PNG_ASSET, "mime": "image/png", "width_px": 4, "height_px": 3, "dpi": 200,
                  "category": "chart", "caption_ref": 1 if self.caption else None}
        return ParsedSource(mime="text/x-fig", assets=self.assets, regions=self.regions, blocks=[
            {"kind": "figure", "text": data.decode("utf-8").strip(), "confidence": 0.7, "state": "det",
             "text_source": "native", "locator": loc, "figure": figure},
            {"kind": "caption", "text": "그림 1. 현황", "confidence": 0.7, "state": "det", "text_source": "native",
             "locator": loc}])


def test_ingest_stores_parser_assets_and_links_the_caption(tmp_path):
    engine = LocalEngine(MemoryStore(), parsers=[FigureParser()])
    ref = engine.ingest(str(write(tmp_path / "a.fig", "1분기\n")))
    figure, caption = engine.get_tree(ref.document_id).blocks
    assert figure.figure.caption_block_id == caption.block_id and caption.kind == "caption"
    assert engine.get_asset(PNG_ASSET) == PNG
    with pytest.raises(AssetNotFound):
        engine.get_asset(EXTRA_ASSET)


@pytest.mark.parametrize("assets,message", [
    ({}, "missing from the parsed assets"),
    ({PNG_ASSET: EXTRA}, "does not match its bytes"),
    ({PNG_ASSET: PNG, EXTRA_ASSET: EXTRA}, "not referenced by any figure block"),
])
def test_parser_assets_must_match_the_figure_blocks(tmp_path, assets, message):
    engine = LocalEngine(MemoryStore(), parsers=[FigureParser(assets=assets)])
    with pytest.raises(ValueError, match=message):
        engine.ingest(str(write(tmp_path / "a.fig", "가\n")))
    assert engine.documents() == ()


def test_parser_regions_go_to_the_processing_history(tmp_path):
    region = RegionRecord(region_id="p1-table-in-figure-1", kind="table", chosen="det", attempts=[Attempt(layer="det")],
                          locator={"kind": "page", "page": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.3}},
                          fallback_reason="ruled table inside a figure was dropped (figure wins)")
    engine = LocalEngine(MemoryStore(), parsers=[FigureParser(regions=(region,))])
    ref = engine.ingest(str(write(tmp_path / "a.fig", "가\n")))
    assert engine.history(ref.document_id).regions == (region,)


def test_caption_pairing_change_is_an_update_of_the_same_figure(tmp_path):
    """캡션 짝은 해시에 들어가지 않는다: 짝이 바뀌면 같은 그림 블록이 updated."""
    store = MemoryStore()
    path = write(tmp_path / "a.fig", "가\n")
    LocalEngine(store, parsers=[FigureParser()]).ingest(str(path), document_id="d")
    ref = LocalEngine(store, parsers=[FigureParser(caption=False)]).ingest(str(path), document_id="d", force=True)
    change = store.changes_after(1, 10).changes[0]
    assert ref.version == 2 and change.updated == (store.get_tree("d", 1).blocks[0].block_id,)
    assert change.added == change.removed == ()


def test_ingest_outcome_tells_whether_a_new_version_was_stored():
    """_ingest_outcome은 ingest_bytes와 같은 경로이고 새 버전을 저장했는지를 함께 돌려준다: 새 문서, 같은 원본, 원본은
    다르고 블록이 같음, force(차이 없음·있음). ingest_bytes는 그 DocRef 그대로다."""
    store = MemoryStore()
    engine = LocalEngine(store, parsers=[FakeParser()])
    data = "가\n".encode()
    first = engine._ingest_outcome(data, "x.fake", "d")
    assert (first.ref.version, first.created, first.pages) == (1, True, None)
    assert engine._ingest_outcome(data, "x.fake", "d") == (first.ref, False, None)
    assert engine._ingest_outcome(data + b"\n\n", "x.fake", "d") == (first.ref, False, None)  # 블록이 같다
    assert engine._ingest_outcome(data, "x.fake", "d", force=True) == (first.ref, False, None)
    second = LocalEngine(store, parsers=[FakeParser(suffix=".")])._ingest_outcome(data, "x.fake", "d", force=True)
    assert (second.ref.version, second.created) == (2, True)
    assert engine.ingest_bytes(b"x\n", "y.fake") == engine._ingest_outcome(b"x\n", "y.fake").ref


def test_ingest_outcome_counts_pages_for_pdf_only():
    """쪽 수는 그 버전의 source.page_count다: PDF는 쪽 수(새 버전이든 그대로든), Markdown은 None."""
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    pdf = (Path(__file__).resolve().parent / "fixtures" / "pdf" / "inputs" / "report.pdf").read_bytes()
    created = engine._ingest_outcome(pdf, "report.pdf")
    assert (created.created, created.pages) == (True, 2)
    assert engine._ingest_outcome(pdf, "report.pdf") == (created.ref, False, 2)
    assert engine._ingest_outcome("# 제목\n".encode(), "a.md").pages is None
