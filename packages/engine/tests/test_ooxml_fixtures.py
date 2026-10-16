"""DOCX·PPTX 골든 예제(build_ooxml_fixtures.py --check): 정확 출력(숫자·단위·부호·순서·칸 사이 이동)과 엔진 수집. 그림은
화소 정보(pixels.json)로 비교한다(트리는 그림 자산 키에서 나온 값을 가린다)."""

import importlib.util
import json
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
    return DocumentTree.model_validate_json((ROOT / "expected" / f"{BUILDER.key(name)}.json").read_bytes())


def _history(name: str) -> ProcessingHistory:
    return ProcessingHistory.model_validate_json((ROOT / "expected" / f"{BUILDER.key(name)}.history.json").read_bytes())


def _regions(name: str) -> list[tuple[str, str]]:
    return [(r.fallback_reason, r.region_id) for r in _history(name).regions]


def test_fixtures_are_up_to_date():
    built = BUILDER.build_all()
    assert {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()} == set(built)
    for rel, data in built.items():
        assert BUILDER.same(rel, data, ROOT / rel), rel


@pytest.mark.parametrize("name", NAMES)
def test_golden_zip_members_pin_create_system_for_cross_os_bytes(name):
    with zipfile.ZipFile(BytesIO(BUILDER.SAMPLES[name])) as z:
        assert {info.create_system for info in z.infolist()} == {3}


@pytest.mark.parametrize("name", NAMES)
def test_engine_ingest_matches_golden_tree_and_history(name):
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(ROOT / "inputs" / name), document_id=BUILDER.document_id(name))
    tree = engine.get_tree(ref.document_id)
    assert BUILDER.portable(tree.model_dump(mode="json")) == BUILDER.portable(_expected(name).model_dump(mode="json"))
    order = list(dict.fromkeys(b.figure.asset for b in tree.blocks if b.figure is not None))
    shown = json.loads((ROOT / "expected" / f"{BUILDER.key(name)}.pixels.json").read_text(encoding="utf-8"))
    assert [BUILDER.pixels(engine.get_asset(key)) for key in order] == shown
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


def test_figures_golden_keeps_pictures_reasons_and_order():
    tree = _expected("figures.docx")
    got = [(b.kind, b.text, b.figure.dpi if b.figure else None, b.locator.paragraph_index) for b in tree.blocks]
    assert got == [
        ("paragraph", "그림 예제 문단이다.", None, 0), ("paragraph", "그림 1. 지어낸 막대 그림", None, 1),
        ("figure", "", 96, 1), ("figure", "", 96, 2), ("figure", "", None, 3), ("figure", "", None, 4),
        ("figure", "", None, 5), ("figure", "", None, 6), ("figure", "", 96, 7), ("figure", "", 96, 8),
        ("figure", "", 96, 9), ("table", "칸 속 그림\t옆 칸\n아래 칸\t끝 칸", None, 10), ("figure", "", 6, 10),
        ("paragraph", "그림 뒤 문단이다.", None, 11)]
    figures = [b.figure for b in tree.blocks if b.figure]
    assert figures[0].asset == figures[4].asset and len({f.asset for f in figures}) == 3  # 같은 그림은 자산 하나
    assert [(f.width_px, f.height_px) for f in figures] == [(4, 4), (8, 8), (6, 6), (6, 6), (4, 4), (6, 6)]
    assert _regions("figures.docx") == [
        ("figure_image_unsupported", "s0-p3-figure-0"), ("figure_image_linked", "s0-p4-figure-0"),
        ("figure_image_missing", "s0-p5-figure-0"), ("figure_image_missing", "s0-p6-figure-0")]
    assert len(json.loads((ROOT / "expected" / "figures.pixels.json").read_text(encoding="utf-8"))) == 3


def test_notes_golden_margins_notes_and_text_boxes():
    tree = _expected("notes.docx")
    assert [(b.kind, b.text, b.locator.paragraph_index) for b in tree.blocks] == [
        ("page_header", "지어낸 머리말\n머리말 둘째 줄", 0), ("paragraph", "머리말 글상자", 0),
        ("page_header", "첫 쪽 머리말", 0), ("page_footer", "쪽 1", 0), ("figure", "", 0),
        ("paragraph", "머리말을 둔 첫 문단이다.", 0), ("paragraph", "각주가 달린 문단 이어지는 글", 1),
        ("table", "각주 든 칸\t둘째 칸\n셋째 칸\t", 2), ("paragraph", "칸 속 글상자", 2),
        ("paragraph", "본문 글상자 첫째", 3), ("paragraph", "본문 글상자 둘째", 3), ("paragraph", "짝 글상자 새 형식", 4),
        ("paragraph", "묶음 속 글상자", 5), ("paragraph", "안 묶음 글상자", 5), ("paragraph", "미주가 달린 문단", 6),
        ("paragraph", "같은 각주를 다시 참조", 7), ("paragraph", "첫째 각주 글", 1), ("paragraph", "칸 속 각주", 2),
        ("paragraph", "첫째 미주 글", 6), ("paragraph", "둘째 구역 문단이다.", 8)]
    history = _history("notes.docx")
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value if r.gate else None) for r in history.regions] == [
        ("shape_text", "s0-p0-shape-0", None), ("shape_text", "s0-p2-shape-0", None),
        ("shape_text", "s0-p3-shape-0", None), ("shape_text", "s0-p4-shape-0", None),
        ("shape_text", "s0-p5-shape-0", None), ("shape_text", "s0-p5-shape-1", None),
        ("alternate_content_skipped", "doc-alternate-content", 14), ("special_note_skipped", "doc-special-note", 6),
        ("unreferenced_part_skipped", "doc-unreferenced-part", 13)]


def test_basic_pptx_slides_placeholders_lists_footers_and_hidden_things():
    tree = _expected("basic.pptx")
    assert [(b.kind, b.text, b.locator.slide, b.locator.shape_index) for b in tree.blocks] == [
        ("heading", "2026년 지어낸 발표", 1, 1), ("paragraph", "지어낸 발표 본문 글상자", 1, 0),
        ("paragraph", "합성 예제 부제", 1, 2), ("page_footer", "2026-10-16", 1, 3), ("page_footer", "지어낸 꼬리말", 1, 4),
        ("page_footer", "1", 1, 5), ("heading", "1. 추진 배경", 2, 0), ("list_item", "기본 글머리 항목", 2, 1),
        ("paragraph", "글머리 없앤 문단", 2, 1), ("list_item", "기호 글머리 항목", 2, 1),
        ("list_item", "종류 없는 자리 항목", 2, 2),
        ("paragraph", "총액 1,234.5억원, 증가율 12.3%, 면적 84㎡, 변동 △0.5, GDP 대비 2.1%p(OECD 평균)", 2, 3),
        ("paragraph", "줄 바꾼\n본문", 2, 7), ("heading", "감춘 슬라이드 제목", 3, 0), ("paragraph", "감춘 슬라이드 본문", 3, 1)]
    assert tree.blocks[7].section_path == ("1. 추진 배경",) and tree.source.page_count is None
    assert tree.source.mime.endswith("presentationml.presentation")
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value if r.gate else None)
            for r in _history("basic.pptx").regions] == [
        ("hidden_slide", "slide3-sh0-hidden-slide-0", None), ("hidden_text", "slide2-sh4-hidden-text-0", 5),
        ("hidden_text", "slide2-sh6-hidden-text-0", 5), ("speaker_notes_skipped", "slide1-sh0-speaker-notes-0", 9),
        ("master_text_skipped", "doc-master-text", 8)]


def test_noplaceholder_pptx_picks_one_topmost_paragraph_per_slide_by_size():
    tree = _expected("noplaceholder.pptx")
    assert [(b.text, b.locator.slide, b.locator.shape_index, b.confidence) for b in tree.blocks
            if b.kind == "heading"] == [
        ("위쪽 큰 글", 1, 2, 0.6), ("같은 높이 앞 글", 2, 1, 0.6), ("그룹 안 큰 글", 3, 3, 0.6),
        ("문단 기본 크기 제목", 4, 1, 0.6), ("도형 목록 스타일 제목", 5, 1, 0.6), ("마스터 둘째 수준 제목", 6, 1, 0.6),
        ("발표 셋째 수준 제목", 7, 1, 0.6)]
    assert [b.kind for b in tree.blocks if b.locator.slide == 8] == ["paragraph", "paragraph"]
    assert _history("noplaceholder.pptx").regions == ()


def test_objects_pptx_tables_groups_pictures_charts_smartart_ole_and_math():
    tree = _expected("objects.pptx")
    got = [(b.kind, b.text, b.figure.width_px if b.figure else None, b.locator.slide, b.locator.shape_index)
           for b in tree.blocks]
    assert got == [
        ("table", "구분\t가로 둘\t넷째\n세로 둘\n이어진 칸 글\t네모\t값 1\n값 2", None, 1, 0),
        ("paragraph", "가\n고아 칸\n다\n라", None, 1, 1), ("paragraph", "상자 안 글", None, 1, 2),
        ("paragraph", "서른 겹 그룹 안 글", None, 1, 33), ("figure", "", 6, 2, 0), ("figure", "", None, 2, 1),
        ("figure", "", 4, 2, 2), ("figure", "", None, 2, 3), ("paragraph", "지어낸 발표 차트\n단위: 건", None, 2, 3),
        ("figure", "", None, 2, 4), ("figure", "", 4, 2, 5), ("paragraph", "수식 x=12", None, 2, 6),
        ("figure", "", None, 3, 0), ("paragraph", "지어낸 발표 차트\n단위: 건", None, 3, 0),
        ("paragraph", "그린 첫 도형", None, 3, 1), ("paragraph", "그린 둘째 도형", None, 3, 1),
        ("paragraph", "둘째 문단", None, 3, 1), ("paragraph", "데이터만 있는 글", None, 3, 2)]
    table = tree.blocks[0].table
    assert [(c.row, c.col, c.rowspan, c.colspan, c.header) for c in table.cells][:4] == [
        (0, 0, 1, 1, "column"), (0, 1, 1, 2, "column"), (0, 3, 1, 1, "column"), (1, 0, 2, 1, "none")]
    assert [(r.fallback_reason, r.region_id) for r in _history("objects.pptx").regions] == [
        ("merged_cell_text_kept", "slide1-sh0-table-0"), ("table_invalid", "slide1-sh1-table-0"),
        ("figure_image_unsupported", "slide2-sh1-figure-0"), ("embedded_object", "slide2-sh3-object-0"),
        ("chart_text", "slide2-sh3-chart-0"), ("embedded_object", "slide2-sh4-object-0"),
        ("embedded_object", "slide2-sh5-object-0"), ("embedded_object", "slide3-sh0-object-0"),
        ("chart_text", "slide3-sh0-chart-0"), ("smartart_text", "slide3-sh1-smartart-0"),
        ("smartart_text", "slide3-sh1-smartart-1"), ("smartart_text", "slide3-sh2-smartart-0"),
        ("equation_text", "slide2-sh6-equation-0"), ("alternate_content_skipped", "doc-alternate-content")]
    assert len(json.loads((ROOT / "expected" / "objects-pptx.pixels.json").read_text(encoding="utf-8"))) == 2
