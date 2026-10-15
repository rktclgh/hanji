"""DOCX 표 격자(스펙 §6, §6.1). 행(w:tr)마다 gridBefore부터 칸(w:tc)을 gridSpan만큼 열 위치에 놓고, 세로 병합은 열
위치마다 활성 사슬로 잇는다. 정수 검사와 크기 상한은 칸 목록·빈 칸을 만들기 전에 본다(작은 속성 하나로 큰 할당이 일어나지
않게). 계약 Table 조립은 hwpx/tables.py의 build를 쓴다(부르는 쪽)."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from hanji_contracts import MAX_TABLE_CELLS

from ...hwpx.tables import TABLE_INVALID, TABLE_TOO_LARGE
from .styles import on

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TR, TC, SDT, SDT_CONTENT, CUSTOM_XML = f"{W}tr", f"{W}tc", f"{W}sdt", f"{W}sdtContent", f"{W}customXml"
ITEMS = frozenset({f"{W}p", f"{W}tbl"})  # 칸 항목


@dataclass(slots=True)
class Slot:
    """격자 칸 하나. tc가 None이면 빈자리 채움 칸, merged는 이 칸에 이어 붙은(세로 병합) w:tc들."""

    tc: ET.Element | None
    row: int
    col: int
    rowspan: int
    colspan: int
    header: bool
    merged: list[ET.Element] = field(default_factory=list)


@dataclass(slots=True)
class Grid:
    rows: int
    cols: int
    slots: list[Slot]
    repaired: bool  # 짝 없는 continue를 보통 칸으로 두었다
    padded: bool  # gridBefore·gridAfter·짧은 행을 빈 칸으로 채웠다


def _children(element: ET.Element, tags: frozenset[str]) -> list[ET.Element]:
    """element 아래 tags 요소들(w:sdt/w:sdtContent·w:customXml은 벗긴다, 스택 순회)."""
    out: list[ET.Element] = []
    stack = list(reversed(element))
    while stack:
        child = stack.pop()
        if child.tag in tags:
            out.append(child)
        elif child.tag == SDT:
            content = child.find(SDT_CONTENT)
            if content is not None:
                stack.extend(reversed(content))
        elif child.tag == CUSTOM_XML:
            stack.extend(reversed(child))
    return out


def rows(tbl: ET.Element) -> list[tuple[ET.Element, list[ET.Element]]]:
    """(w:tr, 그 행의 w:tc들)."""
    return [(tr, _children(tr, frozenset({TC}))) for tr in _children(tbl, frozenset({TR}))]


def cell_items(tc: ET.Element) -> list[ET.Element]:
    """칸 안 w:p·w:tbl(문서 순서)."""
    return _children(tc, ITEMS)


def _number(element: ET.Element | None, default: int) -> int:
    """w:val 정수(요소가 없으면 default, 정수가 아니거나 음수면 -1: 부르는 쪽이 table_invalid로 본다)."""
    if element is None:
        return default
    try:
        return max(int(element.get(f"{W}val", "")), -1)
    except ValueError:
        return -1


def is_box(tbl: ET.Element) -> ET.Element | None:
    """정말 1×1인 표(행 하나, 칸 하나, 선언 격자 1열 이하, gridBefore·gridAfter 0, gridSpan 1, 가로 병합 없음)의 칸.
    아니면 None(보통 표 검증으로)."""
    found = rows(tbl)
    if len(found) != 1 or len(found[0][1]) != 1 or len(tbl.findall(f"{W}tblGrid/{W}gridCol")) > 1:
        return None
    tr, (tc,) = found[0]
    if _number(tr.find(f"{W}trPr/{W}gridBefore"), 0) != 0 or _number(tr.find(f"{W}trPr/{W}gridAfter"), 0) != 0:
        return None
    if _number(tc.find(f"{W}tcPr/{W}gridSpan"), 1) != 1 or tc.find(f"{W}tcPr/{W}hMerge") is not None:
        return None
    return tc


def grid(tbl: ET.Element) -> Grid | str:
    """격자 또는 사유(table_invalid·table_too_large). 옛 가로 병합 w:hMerge는 지원하지 않는다(table_invalid)."""
    found = rows(tbl)
    shapes: list[tuple[int, list[int]]] = []
    width = len(tbl.findall(f"{W}tblGrid/{W}gridCol"))
    for tr, tcs in found:
        before = _number(tr.find(f"{W}trPr/{W}gridBefore"), 0)
        after = _number(tr.find(f"{W}trPr/{W}gridAfter"), 0)
        spans = [_number(tc.find(f"{W}tcPr/{W}gridSpan"), 1) for tc in tcs]
        if any(tc.find(f"{W}tcPr/{W}hMerge") is not None for tc in tcs):
            return TABLE_INVALID
        if before < 0 or after < 0 or min(spans, default=1) < 1:
            return TABLE_INVALID
        if max(before, after, *spans) > MAX_TABLE_CELLS:
            return TABLE_TOO_LARGE
        shapes.append((before, spans))
        width = max(width, before + sum(spans) + after)
    if not found or width == 0:
        return TABLE_INVALID
    if len(found) * width > MAX_TABLE_CELLS:
        return TABLE_TOO_LARGE
    heads = 0
    while heads < len(found) and on(found[heads][0].find(f"{W}trPr/{W}tblHeader")):
        heads += 1
    slots: list[Slot] = []
    active: dict[int, Slot] = {}  # 열 위치 → 세로 병합 사슬의 시작 칸
    repaired = padded = False
    for r, ((_, tcs), (before, spans)) in enumerate(zip(found, shapes)):
        header = r < heads
        touched: set[int] = set()
        slots += [Slot(None, r, c, 1, 1, header) for c in range(before)]
        col = before
        for tc, span in zip(tcs, spans):
            merge = tc.find(f"{W}tcPr/{W}vMerge")
            kind = None if merge is None else merge.get(f"{W}val", "continue")
            chain = active.get(col)
            if kind is not None and kind != "restart" and chain is not None and chain.colspan == span:
                chain.rowspan += 1
                chain.merged.append(tc)
            else:
                repaired = repaired or (kind is not None and kind != "restart")
                slot = Slot(tc, r, col, 1, span, header)
                slots.append(slot)
                if kind is not None:  # restart(와 짝 없는 continue)는 새 사슬
                    active[col] = slot
            if kind is not None:
                touched.add(col)
            col += span
        slots += [Slot(None, r, c, 1, 1, header) for c in range(col, width)]
        padded = padded or before > 0 or col < width
        active = {c: s for c, s in active.items() if c in touched}  # 이 행에서 잇거나 시작하지 않은 사슬은 끝난다
    return Grid(len(found), width, slots, repaired, padded)
