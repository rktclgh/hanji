"""PPTX 표(스펙 §6.2): 물리 격자의 네 가지 병합, 참 값 1·true, 이어진 칸 글자, firstRow, 검증 실패·상한, 1×1 상자.
합성 문서는 build_ooxml_fixtures.py로 만든다."""

import importlib.util
from pathlib import Path

from hanji.formats.ooxml.pptx.parser import PptxParser

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


def parse(*shapes: str):
    parsed, counts = PptxParser()._parse(B.pptx(B.slide(*shapes)), "a.pptx")
    assert all(c.ok for c in counts) and all(c.rescued == 0 for c in counts)
    return parsed


def cells(block) -> list[tuple]:
    return [(c.row, c.col, c.rowspan, c.colspan, c.header, c.text) for c in block["table"].cells]


def test_four_merge_kinds_true_values_merged_text_and_first_row_header():
    table = B.a_tbl(
        B.a_tr(B.a_tc("구분"), B.a_tc("가로 둘", span=2), B.a_tc(h="1"), B.a_tc("넷째")),
        B.a_tr(B.a_tc("세로 둘", rows=2), B.a_tc("네모", span=2, rows=2), B.a_tc(h="true"), B.a_tc("값 1")),
        B.a_tr(B.a_tc("이어진 칸 글", v="1"), B.a_tc(v="true"), B.a_tc(h="1", v="1"), B.a_tc("값 2")),
        cols=4, first=True)
    parsed = parse(table)
    (block,) = parsed.blocks
    assert (block["table"].n_rows, block["table"].n_cols, block["locator"].shape_index) == (3, 4, 0)
    assert cells(block) == [(0, 0, 1, 1, "column", "구분"), (0, 1, 1, 2, "column", "가로 둘"),
                            (0, 3, 1, 1, "column", "넷째"), (1, 0, 2, 1, "none", "세로 둘\n이어진 칸 글"),
                            (1, 1, 2, 2, "none", "네모"), (1, 3, 1, 1, "none", "값 1"), (2, 3, 1, 1, "none", "값 2")]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("merged_cell_text_kept", "slide1-sh0-table-0")]


def test_invalid_grids_fall_back_to_paragraphs_and_large_spans_fail_before_allocation():
    orphan = B.a_tbl(B.a_tr(B.a_tc("가"), B.a_tc("고아", h="1")), B.a_tr(B.a_tc("다"), B.a_tc("라")), cols=2)
    overlap = B.a_tbl(B.a_tr(B.a_tc("넓은", span=2), B.a_tc("겹침", rows=2)), B.a_tr(B.a_tc("아래"), B.a_tc(v="1")),
                      cols=2)
    short = B.a_tbl(B.a_tr(B.a_tc("한 칸만")), B.a_tr(B.a_tc("둘"), B.a_tc("셋")), cols=2)
    word = B.a_tbl(B.a_tr(B.a_tc("글자 폭", span="두"), B.a_tc(h="1")), B.a_tr(B.a_tc("마"), B.a_tc("바")), cols=2)
    outside = B.a_tbl(B.a_tr(B.a_tc("밖으로", rows=3), B.a_tc("옆")), B.a_tr(B.a_tc(v="1"), B.a_tc("끝")), cols=2)
    huge = B.a_tbl(B.a_tr(B.a_tc("아주 넓은", span=10**9), B.a_tc(h="1")), B.a_tr(B.a_tc("사"), B.a_tc("아")), cols=2)
    parsed = parse(orphan, overlap, short, word, outside, huge)
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [
        ("paragraph", "가\n고아\n다\n라"), ("paragraph", "넓은\n겹침\n아래"), ("paragraph", "한 칸만\n둘\n셋"),
        ("paragraph", "글자 폭\n\n마\n바"), ("paragraph", "밖으로\n옆\n\n끝"), ("paragraph", "아주 넓은\n\n사\n아")]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("table_invalid", "slide1-sh0-table-0"), ("table_invalid", "slide1-sh1-table-0"),
        ("table_invalid", "slide1-sh2-table-0"), ("table_invalid", "slide1-sh3-table-0"),
        ("table_invalid", "slide1-sh4-table-0"), ("table_too_large", "slide1-sh5-table-0")]


def test_a_one_by_one_box_is_unpacked_and_a_band_is_a_table_and_hidden_tables_give_nothing():
    box = B.a_tbl(B.a_tr(B.a_tc("상자 첫 문단", B.para("상자 글머리", bullet="char"))), cols=1)
    band = B.a_tbl(B.a_tr(B.a_tc("띠 하나"), B.a_tc("띠 둘"), B.a_tc("띠 셋")), cols=3)
    hidden = B.a_tbl(B.a_tr(B.a_tc("숨긴 표 칸"), B.a_tc("숨긴 옆 칸")), cols=2, hidden=True)
    parsed = parse(box, band, hidden)
    assert [(b["kind"], b["text"], b["locator"].shape_index) for b in parsed.blocks] == [
        ("paragraph", "상자 첫 문단", 0), ("list_item", "상자 글머리", 0), ("table", "띠 하나\t띠 둘\t띠 셋", 1)]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("hidden_text", "slide1-sh2-hidden-text-0"), ("hidden_text", "slide1-sh2-hidden-text-1")]
