"""hanji DocumentTree → 공통 블록 목록. 계약 블록 종류 여덟은 이름 그대로 옮긴다(other는 없다). 쪽은 PageLocator만
(흐름·슬라이드·줄 위치는 None)."""

from hanji_contracts import DocumentTree, PageLocator

from ..blocks import Block, Cell, Table

ADAPTER = "ADAPTER_V1"


def blocks(tree: DocumentTree, doc: str) -> list[Block]:
    out: list[Block] = []
    for b in tree.blocks:
        table = None
        if b.table is not None:
            table = Table(b.table.n_rows, b.table.n_cols,
                          tuple(Cell(c.row, c.col, c.rowspan, c.colspan, c.text) for c in b.table.cells))
        page = b.locator.page if isinstance(b.locator, PageLocator) else None
        out.append(Block(doc, b.order, page, b.kind, b.text, table, b.confidence, b.text_source, b.level))
    return out
