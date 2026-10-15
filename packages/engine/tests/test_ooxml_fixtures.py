"""DOCX 골든 예제(build_ooxml_fixtures.py --check): 정확 출력(숫자·단위·부호·순서·칸 사이 이동)과 엔진 수집."""

import importlib.util
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from hanji import LocalEngine, MemoryStore
from hanji.formats.detect import default_parsers
from hanji_contracts import DocumentTree, ProcessingHistory

HERE = Path(__file__).resolve().parent
ROOT = HERE / "fixtures" / "ooxml"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()
NAMES = sorted(BUILDER.SAMPLES)


def _expected(name: str) -> DocumentTree:
    return DocumentTree.model_validate_json((ROOT / "expected" / f"{Path(name).stem}.json").read_bytes())


def _history(name: str) -> ProcessingHistory:
    return ProcessingHistory.model_validate_json((ROOT / "expected" / f"{Path(name).stem}.history.json").read_bytes())


def _regions(name: str) -> list[tuple[str, str]]:
    return [(r.fallback_reason, r.region_id) for r in _history(name).regions]


def test_fixtures_are_up_to_date():
    built = BUILDER.build_all()
    assert {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()} == set(built)
    for rel, data in built.items():
        assert (ROOT / rel).read_bytes() == data, rel


@pytest.mark.parametrize("name", NAMES)
def test_golden_zip_members_pin_create_system_for_cross_os_bytes(name):
    with zipfile.ZipFile(BytesIO(BUILDER.SAMPLES[name])) as z:
        assert {info.create_system for info in z.infolist()} == {3}


@pytest.mark.parametrize("name", NAMES)
def test_engine_ingest_matches_golden_tree_and_history(name):
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(ROOT / "inputs" / name), document_id=BUILDER.document_id(name))
    assert engine.get_tree(ref.document_id) == _expected(name)
    assert engine.history(ref.document_id) == _history(name)
    assert engine.ingest(str(ROOT / "inputs" / name), document_id=BUILDER.document_id(name)) == ref


def test_basic_keeps_numbers_units_symbols_and_order_exactly():
    tree = _expected("basic.docx")
    assert [(b.kind, b.level, b.text, b.locator.paragraph_index) for b in tree.blocks] == [
        ("heading", 1, "2026년 지어낸 사업 계획", 0),
        ("paragraph", None, "본 문서는 합성 예제로 만든 글이다.", 1),
        ("heading", 2, "1. 추진 배경", 3),
        ("paragraph", None, "구분\t내용\n둘째 줄 A‑B 기호 \uf0e0", 4),
        ("list_item", None, "첫째 번호 항목", 5), ("list_item", None, "스타일 목록 항목", 6),
        ("heading", 6, "아주 깊은 개요", 7),
        ("paragraph", None, "총액 1,234.5억원, 증가율 12.3%, 면적 84㎡, 변동 △0.5, GDP 대비 2.1%p(OECD 평균)", 8),
        ("heading", 3, "순환 스타일 제목", 9),
        ("paragraph", None, "모르는 자식 글 끝", 10), ("paragraph", None, "보이는 글 표시 기호 글", 11),
        ("paragraph", None, "넣은 글 옮겨 온 글", 12), ("paragraph", None, "수식 x=12", 13),
        ("heading", 1, "2. 다음 절", 14), ("paragraph", None, "끝 문단이다.", 15)]
    assert tree.blocks[6].section_path == ("2026년 지어낸 사업 계획", "1. 추진 배경")
    assert tree.blocks[-1].section_path == ("2. 다음 절",)
    assert tree.source.mime.endswith("wordprocessingml.document") and tree.source.page_count is None
    assert _regions("basic.docx") == [
        ("unknown_inline_element", "s1-p10-unknown-inline-0"), ("symbol_char", "s0-p4-symbol-0"),
        ("hidden_text", "s1-p11-hidden-text-0"), ("equation_text", "s1-p13-equation-0"),
        ("tracked_deletion_skipped", "doc-tracked-deletion")]


def test_headings_follow_the_pdf_size_rule_without_heading_styles():
    tree = _expected("headings.docx")
    assert [(b.kind, b.level, b.confidence) for b in tree.blocks] == [
        ("heading", 1, 0.6), ("paragraph", None, 1.0), ("heading", 2, 0.6), ("paragraph", None, 1.0),
        ("heading", 3, 0.6), ("paragraph", None, 1.0), ("heading", 2, 0.6), ("paragraph", None, 1.0)]
    assert _history("headings.docx").regions == ()


def test_table_golden_cells_merges_padding_fallbacks_and_boxes():
    tree = _expected("table.docx")
    tables = [b.table for b in tree.blocks if b.kind == "table"]
    first = [(c.row, c.col, c.rowspan, c.colspan, c.header, c.text) for c in tables[0].cells]
    assert first[:4] == [(0, 0, 1, 1, "column", "구분"), (0, 1, 1, 1, "column", "2025년"),
                         (0, 2, 1, 1, "column", "2026년"), (1, 0, 3, 1, "none", "지역\n이어진 칸 글")]
    assert (tables[1].n_rows, tables[1].n_cols) == (6, 3)
    assert [(c.row, c.col, c.text) for c in tables[2].cells if not c.text] == [(1, 0, ""), (3, 1, "")]
    assert [(b.kind, b.text, b.locator.paragraph_index) for b in tree.blocks if b.kind != "table"] == [
        ("paragraph", "표 예제 문단", 0), ("paragraph", "옛 가로 병합\n옆 칸\n아래 칸\n끝 칸", 4),
        ("heading", "상자 안 제목", 5), ("paragraph", "상자 안 문단", 5), ("paragraph", "아주 넓은 칸\n큰 값 아래", 8),
        ("paragraph", "표 뒤 문단", 9)]
    assert tables[4].cells[0].text == "바깥 칸\n안 1\t안 2\n안 3\t안 4" and tables[4].cells[1].text == "작은 상자"
    assert _regions("table.docx") == [
        ("merged_cell_text_kept", "s0-p1-table-0"), ("table_merge_repaired", "s0-p2-table-0"),
        ("table_merge_repaired", "s0-p3-table-0"), ("table_cells_padded", "s0-p3-table-1"),
        ("table_invalid", "s0-p4-table-0"), ("nested_table_flattened", "s0-p6-nested-table-0"),
        ("table_too_large", "s0-p8-table-0")]


def test_fields_golden_keeps_results_and_one_alternate_branch():
    tree = _expected("fields.docx")
    assert [(b.text, b.locator.paragraph_index) for b in tree.blocks] == [
        ("1. 첫째 절\t1", 0), ("2. 둘째 절\t3", 1), ("3. 셋째 절\t5", 2), ("앞 참 뒤", 3), ("필드 밖 명령 글자", 4),
        ("쪽 수 12", 5), ("고른 쪽 글", 6), ("대체 쪽 글", 7), ("무시 표시 벗긴 글", 9), ("끝나지 않은", 10)]
    assert {b.kind for b in tree.blocks} == {"paragraph"}
    history = _history("fields.docx")
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in history.regions] == [
        ("field_unterminated", "s0-p10-field-unterminated-0", 10), ("alternate_content_skipped",
                                                                    "doc-alternate-content", 24)]
