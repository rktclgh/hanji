from pathlib import Path

import pytest

from hanji import LocalEngine, MemoryStore
from hanji.formats.detect import default_parsers
from hanji_eval.adapters import hanji as adapter
from hanji_eval.blocks import Block, Cell, Table, structure_match

FIXTURES = Path(__file__).resolve().parents[3] / "packages" / "engine" / "tests" / "fixtures"


def _tree(path: Path):
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(path))
    return engine.get_tree(ref.document_id)


def test_pdf_blocks_keep_their_page_kind_text_table_and_confidence():
    tree = _tree(FIXTURES / "pdf" / "inputs" / "table.pdf")
    out = adapter.blocks(tree, "pdf-0001")
    assert len(out) == len(tree.blocks) and adapter.ADAPTER == "ADAPTER_V1"
    for got, src in zip(out, tree.blocks):
        assert (got.doc, got.order, got.page, got.kind, got.text) == (
            "pdf-0001", src.order, src.locator.page, src.kind, src.text)
        assert (got.confidence, got.text_source, got.level) == (src.confidence, src.text_source, src.level)
    tables = [(b, s) for b, s in zip(out, tree.blocks) if s.kind == "table"]
    assert tables
    for got, src in tables:
        assert (got.table.n_rows, got.table.n_cols) == (src.table.n_rows, src.table.n_cols)
        assert [(c.row, c.col, c.rowspan, c.colspan, c.text) for c in got.table.cells] == [
            (c.row, c.col, c.rowspan, c.colspan, c.text) for c in src.table.cells]


def test_flow_blocks_have_no_page_and_headings_keep_their_level():
    tree = _tree(FIXTURES / "hwpx" / "inputs" / "headings.hwpx")
    out = adapter.blocks(tree, "hwpx-0001")
    assert out and all(b.page is None for b in out)
    levels = [b.level for b in out if b.kind == "heading"]
    assert levels and levels == [s.level for s in tree.blocks if s.kind == "heading"] and None not in levels


def test_the_table_pdf_fixture_gives_the_golden_block_kinds_and_table_shape():
    # 기대값은 엔진 골든(fixtures/pdf/expected/table.json)에서 옮겨 적은 글자 그대로의 수다(어댑터를 신탁으로 쓰지 않는다)
    out = adapter.blocks(_tree(FIXTURES / "pdf" / "inputs" / "table.pdf"), "pdf-0001")
    assert [b.kind for b in out] == ["heading", "paragraph", "table", "paragraph"]
    (table,) = [b.table for b in out if b.table is not None]
    assert (table.n_rows, table.n_cols, len(table.cells)) == (4, 4, 14)


@pytest.mark.parametrize(("path", "n"), [(FIXTURES / "ooxml" / "inputs" / "basic.pptx", 15),
                                         (FIXTURES / "inputs" / "report.md", 17)], ids=["slide", "lines"])
def test_slide_and_line_locators_have_no_page(path, n):
    out = adapter.blocks(_tree(path), "pptx-0001")
    assert len(out) == n and all(b.page is None for b in out)


def test_a_table_kind_needs_a_table_and_kinds_are_fixed():
    with pytest.raises(ValueError, match="table must be set"):
        Block("pdf-0001", 0, 1, "table", "")
    with pytest.raises(ValueError, match="unknown block kind"):
        Block("pdf-0001", 0, 1, "title", "")


def test_structure_match_needs_the_same_shape_and_cell_layout():
    a = Table(2, 2, (Cell(0, 0, 1, 2, "가"), Cell(1, 0, text="1"), Cell(1, 1, text="2")))
    same = Table(2, 2, (Cell(1, 1, text="x"), Cell(0, 0, 1, 2, "y"), Cell(1, 0, text="z")))
    split = Table(2, 2, (Cell(0, 0, text="가"), Cell(0, 1), Cell(1, 0, text="1"), Cell(1, 1, text="2")))
    assert structure_match(a, same)
    assert not structure_match(a, split)
    assert not structure_match(a, Table(2, 3, a.cells))
    assert a.grid() == {(0, 0): "가", (0, 1): "가", (1, 0): "1", (1, 1): "2"}


@pytest.mark.parametrize(("table", "message"), [
    (lambda: Table(0, 2, ()), "at least one row"),
    (lambda: Table(2, 0, ()), "at least one row"),
    (lambda: Table(2, 2, (Cell(2, 0),)), "outside the grid"),
    (lambda: Table(2, 2, (Cell(0, 1, 1, 2),)), "outside the grid"),
    (lambda: Table(2, 2, (Cell(0, 0), Cell(0, 0, text="x"))), "same position"),
    (lambda: Table(2, 2, (Cell(0, 0, 1, 2), Cell(0, 1))), "overlap"),
    (lambda: Table(2, 2, (Cell(1, 0), Cell(0, 0, 2, 1))), "overlap"),
], ids=["no-rows", "no-cols", "row-outside", "span-outside", "same-start", "span-covers-a-start",
        "later-span-covers-an-earlier-cell"])
def test_a_malformed_table_is_rejected_before_any_metric_sees_it(table, message):
    with pytest.raises(ValueError, match=message):
        table()
