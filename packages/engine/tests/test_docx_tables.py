"""DOCX 표(스펙 §6, §6.1): 격자·gridSpan·세로 병합 사슬·빈자리 채움·가로 병합 대체·머리 행·1×1 상자·칸 안 표."""

import importlib.util
import time
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.ooxml.docx import tables
from hanji.formats.ooxml.docx.parser import DocxParser

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
STYLES = B.styles(B.style("H1", "heading 1"))


def parse(*items: str):
    return DocxParser()._parse(B.docx(B.document(*items), styles_xml=STYLES), "a.docx")


def shape(parsed, k: int = 0) -> list[tuple]:
    table = [b for b in parsed.blocks if b["kind"] == "table"][k]["table"]
    return [(c.row, c.col, c.rowspan, c.colspan, c.header, c.text) for c in table.cells]


def reasons(parsed) -> list[str]:
    return [r.fallback_reason for r in parsed.regions]


def test_a_plain_table_with_header_rows_is_a_table_block():
    parsed, counts = parse(B.p("앞 문단"), B.tbl(B.tr(B.tc("구분"), B.tc("금액"), header=True),
                                                  B.tr(B.tc("가", "나"), B.tc("1,234.5억원"))), B.p("뒤 문단"))
    assert [(b["kind"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("paragraph", 0), ("table", 1), ("paragraph", 2)]
    assert shape(parsed) == [(0, 0, 1, 1, "column", "구분"), (0, 1, 1, 1, "column", "금액"),
                             (1, 0, 1, 1, "none", "가\n나"), (1, 1, 1, 1, "none", "1,234.5억원")]
    assert parsed.blocks[1]["text"] == "구분\t금액\n가\n나\t1,234.5억원"
    assert counts[0].ok and reasons(parsed) == []


def test_grid_span_beyond_the_declared_grid_widens_it():
    parsed, counts = parse(B.tbl(B.tr(B.tc("넓은 칸", span=3)), B.tr(B.tc("가"), B.tc("나"), B.tc("다")), cols=2))
    assert shape(parsed) == [(0, 0, 1, 3, "none", "넓은 칸"), (1, 0, 1, 1, "none", "가"), (1, 1, 1, 1, "none", "나"),
                             (1, 2, 1, 1, "none", "다")]
    assert counts[0].ok and reasons(parsed) == []


def test_vertical_merge_chains_take_restart_bare_and_explicit_continue_and_keep_hidden_cell_text():
    parsed, counts = parse(B.tbl(B.tr(B.tc("병합", merge="restart"), B.tc("오른쪽 1")),
                                 B.tr(B.tc(merge=""), B.tc("오른쪽 2")),
                                 B.tr(B.tc("남은 글", merge="continue"), B.tc("오른쪽 3"))))
    assert shape(parsed) == [(0, 0, 3, 1, "none", "병합\n남은 글"), (0, 1, 1, 1, "none", "오른쪽 1"),
                             (1, 1, 1, 1, "none", "오른쪽 2"), (2, 1, 1, 1, "none", "오른쪽 3")]
    assert counts[0].ok and reasons(parsed) == ["merged_cell_text_kept"]


def test_unmatched_continues_are_repaired_and_chains_end_without_vmerge_or_at_gaps():
    parsed, counts = parse(B.tbl(B.tr(B.tc("가", merge="restart"), B.tc("나", merge="restart", span=2)),
                                 B.tr(B.tc("다"), B.tc(merge="continue")),
                                 B.tr(B.tc("라", merge="continue"), B.tc("마", span=2)),
                                 B.tr(B.tc(merge="restart"), B.tc("바"), B.tc("사")),
                                 B.tr(B.tc("아", merge="continue"), B.tc("자", span=2), before=0),
                                 cols=3))
    got = shape(parsed)
    assert got[:2] == [(0, 0, 1, 1, "none", "가"), (0, 1, 1, 2, "none", "나")]
    assert (1, 1, 1, 1, "none", "") in got  # 너비가 다른 continue는 보통 칸
    assert (2, 0, 1, 1, "none", "라") in got  # 다(vMerge 없음)가 사슬을 끝냈다
    assert (3, 0, 2, 1, "none", "아") in got  # restart → continue는 이어진다
    assert reasons(parsed) == ["table_merge_repaired", "table_cells_padded", "merged_cell_text_kept"]
    assert counts[0].ok
    gap, _ = parse(B.tbl(B.tr(B.tc("시작", merge="restart"), B.tc("가")), B.tr(B.tc("나"), before=1),
                         B.tr(B.tc("이음", merge="continue"), B.tc("다"))))
    assert (2, 0, 1, 1, "none", "이음") in shape(gap)  # restart → 빈자리 → continue는 잇지 않는다
    assert "table_merge_repaired" in reasons(gap)


def test_grid_before_after_and_short_rows_are_padded_with_empty_cells():
    parsed, counts = parse(B.tbl(B.tr(B.tc("가"), B.tc("나"), B.tc("다")), B.tr(B.tc("라"), before=1, after=1),
                                 B.tr(B.tc("마")), cols=3))
    assert shape(parsed) == [(0, 0, 1, 1, "none", "가"), (0, 1, 1, 1, "none", "나"), (0, 2, 1, 1, "none", "다"),
                             (1, 0, 1, 1, "none", ""), (1, 1, 1, 1, "none", "라"), (1, 2, 1, 1, "none", ""),
                             (2, 0, 1, 1, "none", "마"), (2, 1, 1, 1, "none", ""), (2, 2, 1, 1, "none", "")]
    assert counts[0].ok and reasons(parsed) == ["table_cells_padded"]


def test_legacy_horizontal_merge_and_bad_integers_fall_back_to_a_paragraph():
    hmerge = '<w:tc><w:tcPr><w:hMerge w:val="restart"/></w:tcPr>' + B.p("옛 병합") + "</w:tc>"
    parsed, counts = parse(B.tbl(f"<w:tr>{hmerge}{B.tc('옆')}</w:tr>", B.tr(B.tc("아래"), B.tc("칸"))),
                           B.tbl(B.tr(B.tc("가", span="둘")), B.tr(B.tc("나"))))
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [("paragraph", "옛 병합\n옆\n아래\n칸"),
                                                               ("paragraph", "가\n나")]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("table_invalid", "s0-p0-table-0"), ("table_invalid", "s0-p1-table-0")]
    assert counts[0].ok


@pytest.mark.parametrize("row", [B.tr(B.tc("가", span=10**12)), B.tr(B.tc("가"), before=10**9),
                                 B.tr(B.tc("가", span=60_000), B.tc("나", span=60_000))])
def test_huge_integers_are_too_large_before_any_cell_is_allocated(row, monkeypatch):
    monkeypatch.setattr(tables, "Slot", lambda *a, **k: pytest.fail("allocated a cell"))
    started = time.perf_counter()
    parsed, counts = parse(B.tbl(row, B.tr(B.tc("나"))))
    assert reasons(parsed) == ["table_too_large"] and counts[0].ok and time.perf_counter() - started < 5


def test_a_true_one_by_one_table_is_a_box_whose_items_are_body_items():
    inner = B.tbl(B.tr(B.tc("안 가"), B.tc("안 나")), B.tr(B.tc("안 다"), B.tc("안 라")))
    box = B.tbl(B.tr(B.tc(B.p("상자 제목", style="H1"), B.p("상자 문단"), inner)), cols=1)
    parsed, counts = parse(box, B.tbl(B.tr(B.tc("띠 1"), B.tc("띠 2"))))
    assert [(b["kind"], b["text"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("heading", "상자 제목", 0), ("paragraph", "상자 문단", 0), ("table", "안 가\t안 나\n안 다\t안 라", 0),
        ("table", "띠 1\t띠 2", 1)]
    assert counts[0].ok and reasons(parsed) == []


def test_tables_inside_cells_are_flattened_and_inner_boxes_keep_only_text():
    inner = B.tbl(B.tr(B.tc("안 1"), B.tc("안 2")), B.tr(B.tc("안 3"), B.tc("안 4")))
    one = B.tbl(B.tr(B.tc("상자 글")))
    parsed, counts = parse(B.tbl(B.tr(B.tc(B.p("바깥"), inner), B.tc(one)), B.tr(B.tc("가"), B.tc("나"))))
    assert shape(parsed)[:2] == [(0, 0, 1, 1, "none", "바깥\n안 1\t안 2\n안 3\t안 4"), (0, 1, 1, 1, "none", "상자 글")]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [("nested_table_flattened",
                                                                          "s0-p0-nested-table-0")]
    assert counts[0].ok


def test_rows_and_cells_wrapped_in_content_controls_are_unwrapped():
    wrapped = (f"<w:sdt><w:sdtContent>{B.tr(B.tc('가'), B.tc('나'))}</w:sdtContent></w:sdt>"
               f"<w:tr><w:sdt><w:sdtContent>{B.tc('다')}</w:sdtContent></w:sdt>{B.tc('라')}</w:tr>")
    parsed, counts = parse(B.tbl(wrapped))
    assert [c[-1] for c in shape(parsed)] == ["가", "나", "다", "라"] and counts[0].ok


def test_tables_nested_in_cells_up_to_the_depth_cap_do_not_hit_the_recursion_limit():
    nested = B.tbl(B.tr(B.tc("가장 안"), B.tc("옆")))
    for _ in range(62):
        nested = B.tbl(B.tr(B.tc(nested), B.tc("옆")))
    parsed, counts = parse(nested)
    assert [b["kind"] for b in parsed.blocks] == ["table"] and counts[0].ok
    for _ in range(22):
        nested = B.tbl(B.tr(B.tc(nested), B.tc("옆")))
    with pytest.raises(ParseError, match="deeper than 256"):
        parse(nested)
