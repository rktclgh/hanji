"""PPTX 표 격자(스펙 §6.2): 물리 격자. a:tblGrid/a:gridCol 수가 열 수, a:tr 수가 행 수이고 a:tc 하나가 격자 한 칸을
차지한다(이어진 칸도 자리를 차지한다). 시작 칸은 hMerge·vMerge가 참이 아닌 칸이고 크기는 @gridSpan·@rowSpan(없으면 1).
시작 칸 직사각형 안의 다른 칸이 모두 이어진 칸이고(같은 행은 hMerge, 같은 열은 vMerge, 오른쪽 아래는 둘 중 하나), 이어진
칸마다 정확히 한 직사각형에 들며, 직사각형이 격자 안이어야 한다. 정수·크기는 칸 지도를 만들기 전에 본다."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from hanji_contracts import MAX_TABLE_CELLS

from ...hwpx.tables import TABLE_INVALID, TABLE_TOO_LARGE, number
from ..drawing import A

TRUE = ("1", "true")


@dataclass(slots=True)
class Slot:
    """시작 칸 하나: 자리·크기, 시작 a:tc, 그 직사각형 안의 이어진 a:tc들(문서 순서)."""

    row: int
    col: int
    rowspan: int
    colspan: int
    tc: ET.Element
    merged: list[ET.Element] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Grid:
    rows: int
    cols: int
    slots: list[Slot]
    header: bool  # a:tblPr@firstRow가 참: 0행이 머리


def _flag(tc: ET.Element, name: str) -> bool:
    return tc.get(name, "").lower() in TRUE


def grid(tbl: ET.Element) -> Grid | str:
    """격자 또는 사유(table_invalid·table_too_large)."""
    cols = len(tbl.findall(f"{A}tblGrid/{A}gridCol"))
    trs = tbl.findall(f"{A}tr")
    if len(trs) * cols > MAX_TABLE_CELLS:
        return TABLE_TOO_LARGE
    cells = [tr.findall(f"{A}tc") for tr in trs]
    if any(len(row) != cols for row in cells):
        return TABLE_INVALID
    starts: list[tuple[int, int, int, int]] = []
    for r, row in enumerate(cells):
        for c, tc in enumerate(row):
            if _flag(tc, "hMerge") or _flag(tc, "vMerge"):
                continue
            rowspan, colspan = number(tc.get("rowSpan"), 1), number(tc.get("gridSpan"), 1)
            if rowspan is None or colspan is None or rowspan < 1 or colspan < 1:
                return TABLE_INVALID
            if rowspan > MAX_TABLE_CELLS or colspan > MAX_TABLE_CELLS:
                return TABLE_TOO_LARGE
            if r + rowspan > len(cells) or c + colspan > cols:
                return TABLE_INVALID
            starts.append((r, c, rowspan, colspan))
    owner: list[list[Slot | None]] = [[None] * cols for _ in cells]
    slots = []
    for r, c, rowspan, colspan in starts:
        slot = Slot(r, c, rowspan, colspan, cells[r][c])
        for i in range(r, r + rowspan):
            for j in range(c, c + colspan):
                if (i, j) == (r, c):
                    continue
                tc = cells[i][j]
                h, v = _flag(tc, "hMerge"), _flag(tc, "vMerge")
                ok = (h if i == r else v if j == c else h or v)
                if not ok or owner[i][j] is not None:
                    return TABLE_INVALID
                owner[i][j] = slot
                slot.merged.append(tc)
        owner[r][c] = slot
        slots.append(slot)
    if any(owner[r][c] is None for r in range(len(cells)) for c in range(cols)):
        return TABLE_INVALID  # 어느 직사각형에도 들지 않은 이어진 칸(고아)
    first = tbl.find(f"{A}tblPr")
    return Grid(len(cells), cols, slots, first is not None and first.get("firstRow", "").lower() in TRUE)
