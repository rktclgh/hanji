"""HWPX 표(스펙 §6): 1×1 상자 풀기, 칸 자리·병합, 머리 행 두 규칙, 띠 표, 칸 안 개체, 캡션, 검증 실패·상한."""

import importlib.util
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwpx import tables
from hanji.formats.hwpx.parser import HwpxParser
from hanji.formats.hwpx.tables import RawCell, header_rows
from hanji_contracts import DocumentTree

HERE = Path(__file__).resolve().parent
ROOT = HERE / "fixtures" / "hwpx"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


def cell(row, col, text, **kw):
    return B.tc(row, col, B.para(B.run(B.t(text))), **kw)


def parse(*paras, head=None):
    return HwpxParser()._parse(B.package(B.section(*paras), head=head), "a.hwpx")


def test_table_golden_cells_headers_captions_boxes_and_nested_content():
    tree = DocumentTree.model_validate_json((ROOT / "expected" / "table.json").read_bytes())
    assert [(b.kind, b.locator.paragraph_index) for b in tree.blocks] == [
        ("paragraph", 0), ("caption", 1), ("table", 1), ("table", 2), ("caption", 2), ("table", 3),
        ("heading", 4), ("paragraph", 4), ("table", 4), ("table", 5), ("figure", 5), ("paragraph", 6), ("table", 6)]
    merged = tree.blocks[2].table
    assert [(c.row, c.col, c.rowspan, c.colspan, c.header, c.text) for c in merged.cells] == [
        (0, 0, 1, 1, "column", "구분"), (0, 1, 1, 1, "column", "상반기"), (0, 2, 1, 1, "column", "하반기"),
        (1, 0, 2, 1, "none", "인건비"), (1, 1, 1, 1, "none", "1,200"), (1, 2, 1, 1, "none", "1,350"),
        (2, 1, 1, 1, "none", "900"), (2, 2, 1, 1, "none", "950")]
    assert [c.header for c in tree.blocks[3].table.cells] == ["column", "column", "none", "none"]  # 채움 규칙
    assert (tree.blocks[5].table.n_rows, tree.blocks[5].table.n_cols) == (1, 3)  # 띠도 표
    assert {c.header for c in tree.blocks[5].table.cells} == {"none"}
    assert tree.blocks[6].level == 1 and tree.blocks[8].section_path == ("상자 안 개요 제목",)  # 1×1 상자를 풀었다
    nested = tree.blocks[9].table
    assert [c.text for c in nested.cells] == ["바깥 칸\n\n안1\t안2\n안3\t안4", "상자 앞\n칸 속 상자", "그림 칸", "끝 칸"]
    assert tree.blocks[10].figure is not None  # 칸 안 그림은 표 바로 뒤
    assert tree.blocks[11].text == "혼합 문단 앞  혼합 문단 뒤"  # 글자처럼 둔 표는 문단 글자 뒤(스펙 §5.2 한계)
    history = (ROOT / "expected" / "table.history.json").read_text(encoding="utf-8")
    assert history.count("nested_table_flattened") == 1 and "coverage_mismatch" not in history
    assert "unassigned_text" not in history


def test_the_table_sample_ledger_balances_without_rescue():
    _, (count,) = HwpxParser()._parse(B.SAMPLES["table.hwpx"], "table.hwpx")
    assert count.ok and count.rescued == 0 and count.source == count.in_blocks > 0


@pytest.mark.parametrize(("cells", "expected"), [
    ([RawCell(0, 0, 1, 1, True, False, ""), RawCell(0, 1, 1, 1, True, False, ""),
      RawCell(1, 0, 1, 1, True, False, ""), RawCell(1, 1, 1, 1, True, False, ""),
      RawCell(2, 0, 1, 2, False, False, "")], {0, 1}),
    ([RawCell(0, 0, 1, 1, True, False, ""), RawCell(0, 1, 1, 1, False, True, ""),
      RawCell(1, 0, 1, 2, False, False, "")], set()),
    ([RawCell(0, 0, 1, 1, False, True, ""), RawCell(0, 1, 1, 1, False, True, ""),
      RawCell(1, 0, 1, 1, False, True, ""), RawCell(1, 1, 1, 1, False, True, "")], set()),
    ([RawCell(0, 0, 2, 1, False, True, ""), RawCell(0, 1, 1, 1, False, True, ""),
      RawCell(1, 1, 1, 1, False, False, "")], set()),
    ([RawCell(0, 0, 1, 1, False, True, ""), RawCell(0, 1, 1, 1, False, True, ""),
      RawCell(1, 0, 1, 1, False, False, ""), RawCell(1, 1, 1, 1, False, True, "")], {0}),
])
def test_header_rows_follow_the_author_mark_then_the_pdf_fill_rule(cells, expected):
    """작성자 지정 행들(위에서부터) → 없으면 0행이 모두 한 행 높이·채움이고 1행이 모두 채움은 아니면 0행 → 없음."""
    assert header_rows(cells) == expected


def test_invalid_tables_become_one_paragraph_with_a_record():
    overlap = B.tbl(2, 2, [cell(0, 0, "가"), cell(0, 0, "나")], [cell(1, 0, "다"), cell(1, 1, "라")])
    bad = B.tbl(2, 2, [cell(0, 0, "마"), cell(0, 1, "바")], [cell(1, 0, "사"), cell(1, 1, "아")]).replace(
        'rowCnt="2"', 'rowCnt="둘"')
    parsed, (count,) = parse(B.para(B.run(overlap)), B.para(B.run(bad)))
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [("paragraph", "가\n나\n다\n라"),
                                                                ("paragraph", "마\n바\n사\n아")]
    assert [(r.fallback_reason, r.kind, r.region_id) for r in parsed.regions] == [
        ("table_invalid", "table", "s0-p0-table-0"), ("table_invalid", "table", "s0-p1-table-0")]
    assert count.ok


def test_only_a_true_one_by_one_table_is_unwrapped_as_a_box():
    """칸 하나·주소 (0,0)·span (1,1)인 1×1만 상자다. 칸이 둘이거나 주소·span이 다르면 보통 표 검증(문단 + table_invalid)."""
    two = B.tbl(1, 1, [cell(0, 0, "가"), cell(0, 0, "나")])
    moved = B.tbl(1, 1, [cell(0, 1, "다")])
    wide = B.tbl(1, 1, [cell(0, 0, "라", span=(1, 2))])
    parsed, (count,) = parse(B.para(B.run(two)), B.para(B.run(moved)), B.para(B.run(wide)),
                             B.para(B.run(B.tbl(1, 1, [cell(0, 0, "상자")]))))
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [("paragraph", "가\n나"), ("paragraph", "다"),
                                                                ("paragraph", "라"), ("paragraph", "상자")]
    assert [r.fallback_reason for r in parsed.regions] == ["table_invalid"] * 3 and count.ok


def test_tables_nested_in_cells_up_to_the_depth_cap_do_not_hit_the_recursion_limit():
    """칸 안 표 조립은 중첩만큼 재귀한다: XML 깊이 상한(256) 안의 가장 깊은 중첩(40겹)은 돌고, 넘으면 ParseError."""
    def nest(levels: int) -> str:
        para = B.para(B.run(B.t("가장 안 칸")))
        for _ in range(levels):
            para = B.para(B.run(B.tbl(1, 2, [B.tc(0, 0, para), cell(0, 1, "옆")])))
        return para

    parsed, (count,) = parse(nest(40))
    assert [b["kind"] for b in parsed.blocks] == ["table"] and count.ok
    assert [r.fallback_reason for r in parsed.regions] == ["nested_table_flattened"] * 39
    with pytest.raises(ParseError, match="deeper than 256"):
        parse(nest(43))


def test_tables_over_the_contract_limits_become_paragraphs(monkeypatch):
    monkeypatch.setattr(tables, "MAX_TABLE_CELLS", 3)
    parsed, _ = parse(B.para(B.run(B.tbl(2, 2, [cell(0, 0, "가"), cell(0, 1, "나")],
                                         [cell(1, 0, "다"), cell(1, 1, "라")]))))
    assert [r.fallback_reason for r in parsed.regions] == ["table_too_large"]
    monkeypatch.setattr(tables, "MAX_TABLE_CELLS", 100_000)
    monkeypatch.setattr(tables, "MAX_TABLE_EXPANDED_CHARS", 3)
    parsed, _ = parse(B.para(B.run(B.tbl(1, 2, [cell(0, 0, "가나"), cell(0, 1, "다라")]))))
    assert [r.fallback_reason for r in parsed.regions] == ["table_too_large"]
    assert parsed.blocks[0]["text"] == "가나\n다라"


def test_column_strips_and_captions_on_the_left_and_right():
    left = B.caption(B.para(B.run(B.t("왼쪽 캡션"))), side="LEFT")
    right = B.caption(B.para(B.run(B.t("오른쪽 캡션"))), side="RIGHT")
    parsed, _ = parse(B.para(B.run(B.tbl(3, 1, [cell(0, 0, "가")], [cell(1, 0, "나")], [cell(2, 0, "다")],
                                         caption=left))),
                      B.para(B.run(B.tbl(1, 2, [cell(0, 0, "라"), cell(0, 1, "마")], caption=right))))
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [
        ("caption", "왼쪽 캡션"), ("table", "가\n나\n다"), ("table", "라\t마"), ("caption", "오른쪽 캡션")]
