"""HWPX 표 → 계약 Table(스펙 §6). 칸 자리는 cellAddr·cellSpan 그대로, 머리 행은 작성자 지정(tc@header) → PDF와 같은
채움 규칙 → 없음. 계약 검증에 실패하거나 상한을 넘으면 사유 문자열을 돌려준다(부르는 쪽이 문단으로 낸다)."""

from collections import defaultdict
from dataclasses import dataclass

from hanji_contracts import MAX_TABLE_CELLS, MAX_TABLE_EXPANDED_CHARS, Cell, Table
from pydantic import ValidationError

TABLE_INVALID = "table_invalid"
TABLE_TOO_LARGE = "table_too_large"


@dataclass(frozen=True, slots=True)
class RawCell:
    """칸 하나: 정수가 아닌 속성은 None."""

    row: int | None
    col: int | None
    rowspan: int | None
    colspan: int | None
    header: bool  # tc@header="1"
    filled: bool  # borderFill에 배경이 있다
    text: str


def number(value: str | None, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return None


def header_rows(cells: list[RawCell]) -> set[int]:
    """열 머리 행: 위에서부터 그 행에서 시작하는 칸이 모두 tc@header인 행들. 없으면 PDF _header와 같은 규칙(0행 칸이
    모두 한 행 높이이고 모두 채움, 1행에서 시작하는 칸이 모두 채움은 아님)이면 0행. 그 밖은 없음."""
    starts: dict[int | None, list[RawCell]] = defaultdict(list)
    for cell in cells:
        starts[cell.row].append(cell)
    marked: set[int] = set()
    while len(marked) in starts and all(c.header for c in starts[len(marked)]):
        marked.add(len(marked))
    if marked:
        return marked
    top, below = starts.get(0, []), starts.get(1, [])
    if top and all(c.rowspan == 1 and c.filled for c in top) and not all(c.filled for c in below):
        return {0}
    return set()


def build(n_rows: int | None, n_cols: int | None, cells: list[RawCell]) -> Table | str:
    """계약 Table 또는 사유(table_invalid·table_too_large)."""
    if n_rows is None or n_cols is None or any(
            None in (c.row, c.col, c.rowspan, c.colspan) for c in cells):
        return TABLE_INVALID
    if n_rows * n_cols > MAX_TABLE_CELLS or len(cells) > MAX_TABLE_CELLS:
        return TABLE_TOO_LARGE
    expanded = sum(len(c.text) * max(c.rowspan, 0) * max(c.colspan, 0) for c in cells)
    if expanded > MAX_TABLE_EXPANDED_CHARS:
        return TABLE_TOO_LARGE
    heads = header_rows(cells)
    try:
        return Table(n_rows=n_rows, n_cols=n_cols, cells=tuple(
            Cell(row=c.row, col=c.col, rowspan=c.rowspan, colspan=c.colspan, text=c.text,
                 header="column" if c.row in heads else "none", text_source="native") for c in cells))
    except ValidationError:
        return TABLE_INVALID
