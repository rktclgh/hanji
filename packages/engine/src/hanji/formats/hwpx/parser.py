"""HWPX(한컴 OWPML, zip + XML) → ParsedSource. PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다(모델·OCR 없음).

구역(spine 순서)마다 트리를 만들고 블록 명세로 바꾼 뒤 버린다. 최상위 문단(hs:sec 바로 아래 hp:p)마다 문단 글자
블록(개요 수준 → 제목, 번호·글머리 → 목록 항목, 그 밖 문단)을 낸다. 위치는 FlowLocator(제목 경로, 최상위 문단 순번).
글자 장부(ledger.py)가 모든 hp:t·글맵시 글자를 세고, 한 번도 넣지 않은 글자는 구역 끝 구조 문단(신뢰도 0.3)으로
살린다(처리 이력 unassigned_text). 장부가 맞지 않으면 처리 이력 coverage_mismatch(파싱은 실패하지 않는다).
개요 수준을 쓰지 않는 문서는 PDF와 같은 글꼴 크기 규칙으로 제목을 고른다(_finish).

문단에 박힌 개체는 문단 글자 블록 뒤에 나타난 순서대로 블록이 된다(스펙 §5.2): 표(1×1은 상자라 풀고, 칸 안 개체는
칸 글자로, 칸 안 그림은 표 뒤 그림 블록), 그림과 캡션(PNG 자산), 머리말·꼬리말, 글상자·도형 글자, 수식 스크립트,
글맵시, OLE 등은 이미지 없는 그림. 각주·미주는 구역 끝(닻 문단 위치). 개체 조립은 개체 중첩만큼만 재귀한다(XML 깊이
상한 256이 묶는다)."""

import bisect
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hanji_contracts import Attempt, FlowLocator, GateCheck, GateResult, RegionRecord, Table

from ...errors import ParseError
from ..base import ParsedSource
from ..pdf.figures import AssetBudget
from ..pdf.group import (
    BOLD_HEADING_RATIO, CONFIDENCE, HEADING_MAX_LINES, HEADING_RATIO, MAX_LEVEL, RESCUED_CONFIDENCE, step,
)
from ..pdf.parser import COVERAGE_MISMATCH, UNASSIGNED_TEXT
from . import images, tables
from .container import HC, HH, HP, HS, MIME, OverBudget, Package, TooLarge
from .header import CharShape, Styles, read_styles
from .ledger import INLINE, T, Bundle, Counts, Ledger, ink, source_chars
from .tables import RawCell, number

P, RUN, SUBLIST = f"{HP}p", f"{HP}run", f"{HP}subList"
LINESEGS, LINESEG, LINE_BREAK = f"{HP}linesegarray", f"{HP}lineseg", f"{HP}lineBreak"
TEXTART, HIDDEN = f"{HP}textart", f"{HP}hiddenComment"
TBL, TR, TC, CELL_ADDR, CELL_SPAN = f"{HP}tbl", f"{HP}tr", f"{HP}tc", f"{HP}cellAddr", f"{HP}cellSpan"
PIC, IMG, CUR_SZ, CAPTION, CONTAINER = f"{HP}pic", f"{HC}img", f"{HP}curSz", f"{HP}caption", f"{HP}container"
CTRL, DRAW_TEXT, EQUATION, SCRIPT = f"{HP}ctrl", f"{HP}drawText", f"{HP}equation", f"{HP}script"
SHAPES = frozenset(f"{HP}{n}" for n in ("rect", "ellipse", "arc", "polygon", "curve", "connectLine", "line"))
EMBEDDED = frozenset(f"{HP}{n}" for n in ("ole", "chart", "video"))  # 그림 성격 개체(이미지 없는 그림)
MARGINS = {f"{HP}header": "page_header", f"{HP}footer": "page_footer"}
NOTES = (f"{HP}footNote", f"{HP}endNote")  # 구역 끝에 이 차례로
FIGURE_LINKED = "figure_image_linked"  # isEmbeded="0"(외부 연결 그림: 경로를 따라가지 않는다)
FIGURE_MISSING = "figure_image_missing"  # id가 manifest에 없거나 href 멤버가 zip에 없다
FIGURE_BUDGET = "figure_image_budget"  # MAX_BINARY_TOTAL 또는 문서 그림 바이트 상한을 넘었다
EMBEDDED_OBJECT = "embedded_object"
NESTED_TABLE = "nested_table_flattened"  # 칸 안 표를 칸 글자로 펼쳤다(계약에 중첩 표가 없다)
SHAPE_TEXT = "shape_text"  # 글상자·도형·글맵시 글자를 문단으로 냈다(v1 범위 밖)
EQUATION_SCRIPT = "equation_script"  # 수식 스크립트 원문(한컴 수식 문법)을 문단으로 냈다
UNKNOWN_INLINE = "unknown_inline_element"  # hp:t 안 모르는 자식 요소의 글자를 그 자리에 넣었다
HIDDEN_COMMENT = "hidden_comment"  # 숨은 설명(인쇄되지 않음): 블록에 넣지 않았다
MASTERPAGE_TEXT = "masterpage_text_skipped"  # 마스터 쪽 글자(v1은 읽지 않는다)
NO_SHAPE = CharShape(10.0, False)  # 모르는 charPrIDRef


@dataclass(slots=True)
class _Region:
    """처리 이력 한 줄(제목 경로는 문서를 다 읽은 뒤 닻 문단의 것으로 채운다)."""

    tag: str
    kind: str
    reason: str
    section: int
    anchor: int
    gate: GateResult | None = None
    region_id: str | None = None  # 정해진 id(구역 단위 기록)


@dataclass(slots=True)
class _Doc:
    """문서 전체에서 모으는 것: 블록 명세(내부 키 _anchor 등은 _finish가 지운다), 처리 이력, 장부 숫자."""

    name: str
    styles: Styles
    package: Package
    budget: AssetBudget = field(default_factory=AssetBudget)
    pictures: dict[str, images.Png | str] = field(default_factory=dict)  # href → PNG 또는 이미지 없음 사유
    specs: list[dict[str, Any]] = field(default_factory=list)
    regions: list[_Region] = field(default_factory=list)
    counts: list[Counts] = field(default_factory=list)
    outline: bool = False  # 개요 수준 제목을 하나라도 썼나(그러면 크기 규칙을 쓰지 않는다)


def _t_text(t: ET.Element) -> tuple[str, bool]:
    """hp:t의 글자(스펙 §5.1)와 모르는 자식 요소에 글자가 있었는지."""
    parts = [t.text or ""]
    unknown = False
    for child in t:
        # 아는 인라인 요소는 대치 글자를 넣고, 안에 글자가 있으면(모르는 자식처럼) 그 자리에 잇고 기록한다
        inner = "".join(child.itertext())
        parts.append(INLINE.get(child.tag, "") + inner)
        unknown = unknown or bool(inner.strip())
        parts.append(child.tail or "")
    return "".join(parts), unknown


def _lines(p: ET.Element) -> int:
    """저장된 표시 줄 수(linesegarray의 lineseg 수), 없으면 줄바꿈 수 + 1."""
    segs = p.find(LINESEGS)
    if segs is not None and (count := len(segs.findall(LINESEG))):
        return count
    return sum(1 for run in p.iterfind(RUN) for t in run.iterfind(T) for _ in t.iterfind(LINE_BREAK)) + 1


def _join(bundles: list[Bundle]) -> str:
    return "".join(text for _, text in bundles)


def _box(tbl: ET.Element) -> ET.Element | None:
    """정말 1×1인 표(rowCnt·colCnt 1, 칸 하나, 주소 (0,0), span (1,1))의 칸 subList(없으면 빈 subList: 낼 글자가 없다).
    아니면 None(보통 표 검증으로)."""
    if number(tbl.get("rowCnt")) != 1 or number(tbl.get("colCnt")) != 1:
        return None
    cells = tbl.findall(f"{TR}/{TC}")
    if len(cells) != 1:
        return None
    addr, span = cells[0].find(CELL_ADDR), cells[0].find(CELL_SPAN)
    where = (number(addr.get("rowAddr")), number(addr.get("colAddr"))) if addr is not None else (None, None)
    size = (number(span.get("rowSpan"), 1), number(span.get("colSpan"), 1)) if span is not None else (1, 1)
    if where != (0, 0) or size != (1, 1):
        return None
    sub = cells[0].find(SUBLIST)
    return ET.Element(SUBLIST) if sub is None else sub


class _Section:
    """구역 하나를 블록 명세로 바꾼다. base는 이 구역 첫 최상위 문단의 문서 전체 순번."""

    def __init__(self, doc: _Doc, index: int, root: ET.Element, base: int) -> None:
        self.doc = doc
        self.index = index
        self.root = root
        self.base = base
        self.ledger = Ledger()
        self.late = False  # 구역 끝 블록을 내는 중(닻 문단 자리의 제목 경로를 쓴다)
        self.unknown: set[int] = set()  # 모르는 인라인 요소 기록을 남긴 문단(id)
        self.notes: list[tuple[ET.Element, int]] = []  # (각주·미주, 닻)
        self.inner = False  # 머리말·꼬리말·각주·미주·글상자 안(1×1 상자 문단도 제목 판정 없이 plain)
        self._sources()

    def _sources(self) -> None:
        """장부 출처를 문서 순서로 센다(스택 순회, 재귀 없음). 숨은 설명마다 처리 이력을 남긴다."""
        anchor = self.base - 1
        for top in self.root:
            if top.tag == P:
                anchor += 1
            stack: list[tuple[ET.Element, bool, int]] = [(top, False, id(top))]
            while stack:
                element, hidden, para = stack.pop()
                if element.tag == P:
                    para = id(element)
                if element.tag == T:
                    self.ledger.add(element, source_chars(element), max(anchor, self.base), para, hidden)
                    continue
                if element.tag == TEXTART:
                    self.ledger.add(element, ink(element.get("text", "")), max(anchor, self.base), para, hidden)
                if element.tag == HIDDEN and not hidden:
                    chars = sum(source_chars(t) for t in element.iter(T))
                    self.region("hidden-comment", "paragraph", HIDDEN_COMMENT, max(anchor, self.base),
                                _gate("hidden_chars", chars, None))
                    hidden = True
                stack.extend((child, hidden, para) for child in reversed(element))

    def region(self, tag: str, kind: str, reason: str, anchor: int, gate: GateResult | None = None) -> None:
        self.doc.regions.append(_Region(tag, kind, reason, self.index, anchor, gate))

    def bundles(self, p: ET.Element, anchor: int) -> list[Bundle]:
        """문단 hp:run들의 hp:t 글자를 순서대로(개체 안 글자는 들지 않는다). 출처마다 묶음 하나."""
        out: list[Bundle] = []
        for run in p.iterfind(RUN):
            for t in run.iterfind(T):
                out.append(self.source_bundle(t, anchor, p))
        return out

    def source_bundle(self, t: ET.Element, anchor: int, p: ET.Element) -> Bundle:
        text, unknown = _t_text(t)
        if unknown and id(p) not in self.unknown:
            self.unknown.add(id(p))
            self.region("unknown-inline", "paragraph", UNKNOWN_INLINE, anchor)
        return self.ledger.source_of(t), text

    def emit(self, kind: str, bundles: list[Bundle], anchor: int, **extra: Any) -> bool:
        """블록 하나를 낸다(공백만이면 내지 않는다). 낸 묶음을 장부에 넣는다."""
        text = _join(bundles).strip()
        if not text:
            return False
        self.doc.specs.append({"kind": kind, "text": text, "confidence": extra.pop("confidence", 1.0), "state": "det",
                               "text_source": "native", "_anchor": anchor, "_late": self.late, **extra})
        self.ledger.put(bundles)
        return True

    def paragraph(self, p: ET.Element, anchor: int) -> None:
        """최상위·1×1 상자 문단: 개요 수준 → 제목, 번호·글머리 → 목록 항목, 그 밖 문단(크기 규칙 후보). 그 뒤 박힌
        개체."""
        bundles = self.bundles(p, anchor)
        kind, level = self.doc.styles.headings.get(p.get("paraPrIDRef", ""), ("NONE", 0))
        if kind == "OUTLINE":
            if self.emit("heading", bundles, anchor, level=max(1, min(level + 1, MAX_LEVEL))):  # 음수 수준도 1~6 안으로
                self.doc.outline = True
        elif kind in ("NUMBER", "BULLET"):
            self.emit("list_item", bundles, anchor, _sizes=self.sizes(p))
        else:
            sizes = self.sizes(p)
            bold = sum(n for (_, b), n in sizes.items() if b)
            total = sum(sizes.values())
            by_size = Counter[float]()
            for (size, _), n in sizes.items():
                by_size[size] += n
            size = max(by_size, key=lambda s: (by_size[s], s)) if by_size else None
            self.emit("paragraph", bundles, anchor, _sizes=sizes, _size=size, _bold=bold * 2 > total,
                      _lines=_lines(p))
        self.objects(p, anchor)

    def plain(self, p: ET.Element, anchor: int) -> None:
        """제목·목록 판정 없는 문단(각주·미주·글상자) 하나와 그 안 개체."""
        self.emit("paragraph", self.bundles(p, anchor), anchor)
        self.objects(p, anchor)

    def objects(self, p: ET.Element, anchor: int) -> None:
        """문단에 박힌 개체를 나타난 순서대로 블록으로."""
        for run in p.iterfind(RUN):
            for child in run:
                self.obj(child, anchor)

    def obj(self, element: ET.Element, anchor: int) -> None:
        tag = element.tag
        if tag == TBL:
            self.table(element, anchor)
        elif tag == PIC:
            self.picture(element, anchor)
        elif tag == CONTAINER:
            for child in element:
                self.obj(child, anchor)
        elif tag in SHAPES:
            self.shape(element, anchor)
        elif tag == EQUATION:
            if self.emit("paragraph", [(None, element.findtext(SCRIPT) or "")], anchor):
                self.region("equation", "paragraph", EQUATION_SCRIPT, anchor)
        elif tag == TEXTART:
            if self.emit("paragraph", [(self.ledger.source_of(element), element.get("text", ""))], anchor):
                self.region("shape", "paragraph", SHAPE_TEXT, anchor)
        elif tag in EMBEDDED:
            self.figure(None, anchor)
            self.region("object", "figure", EMBEDDED_OBJECT, anchor)
        elif tag in MARGINS:
            self.margin(element, anchor)
        elif tag in NOTES:
            self.notes.append((element, anchor))
        elif tag == CTRL:
            for child in element:
                if child.tag in MARGINS or child.tag in NOTES:
                    self.obj(child, anchor)

    def has_text(self, element: ET.Element) -> bool:
        return any(self.ledger.sources[self.ledger.source_of(t)].n for t in element.iter(T))

    def shape(self, element: ET.Element, anchor: int) -> None:
        """도형의 글상자(drawText/subList) 문단마다 문단 블록. 글자가 있는 도형마다 shape_text."""
        sub = element.find(f"{DRAW_TEXT}/{SUBLIST}")
        if sub is None:
            return
        if self.has_text(sub):
            self.region("shape", "paragraph", SHAPE_TEXT, anchor)
        inner, self.inner = self.inner, True
        for p in sub.iterfind(P):
            self.plain(p, anchor)
        self.inner = inner

    def margin(self, element: ET.Element, anchor: int) -> None:
        """머리말·꼬리말: subList 문단 글자를 \\n으로 이은 블록 하나(놓인 자리에 한 번), 그 뒤 안의 개체."""
        sub = element.find(SUBLIST)
        paras = sub.findall(P) if sub is not None else []
        bundles: list[Bundle] = []
        for i, p in enumerate(paras):
            if i:
                bundles.append((None, "\n"))
            bundles += self.bundles(p, anchor)
        self.emit(MARGINS[element.tag], bundles, anchor)
        inner, self.inner = self.inner, True
        for p in paras:
            self.objects(p, anchor)
        self.inner = inner

    def end_notes(self) -> None:
        """구역 끝: 각주 전체, 그다음 미주 전체(본문 참조 순서). 문단마다 블록, 그 뒤 안의 개체."""
        inner, self.inner = self.inner, True
        seen: set[int] = set()
        while len(seen) < len(self.notes):  # 미주 안 각주처럼 지난 차례 뒤에 붙은 주석은 한 바퀴 더 돈다
            for tag in NOTES:
                k = 0
                while k < len(self.notes):  # 각주 안 각주가 뒤에 붙어도 돈다
                    note, anchor = self.notes[k]
                    k += 1
                    if note.tag != tag or k in seen:
                        continue
                    seen.add(k)
                    sub = note.find(SUBLIST)
                    if sub is not None:
                        for p in sub.iterfind(P):
                            self.plain(p, anchor)
        self.inner = inner

    def figure(self, figure: dict[str, Any] | None, anchor: int) -> dict[str, Any]:
        """그림 블록(글자 없음). figure가 None이면 이미지 없는 그림."""
        spec: dict[str, Any] = {"kind": "figure", "text": "", "confidence": 1.0, "state": "det",
                                "text_source": "native", "_anchor": anchor, "_late": self.late}
        if figure is not None:
            spec["figure"] = figure
        self.doc.specs.append(spec)
        return spec

    def caption(self, caption: ET.Element, anchor: int) -> dict[str, Any] | None:
        """캡션 subList 문단 글자를 \\n으로 이은 caption 블록(낸 명세, 공백뿐이면 None). 캡션 문단에 박힌 개체는
        캡션 블록 뒤에 블록으로 낸다(조용히 잃지 않는다)."""
        sub = caption.find(SUBLIST)
        paras = list(sub.iterfind(P)) if sub is not None else []
        bundles: list[Bundle] = []
        for i, p in enumerate(paras):
            if i:
                bundles.append((None, "\n"))
            bundles += self.bundles(p, anchor)
        found = self.doc.specs[-1] if self.emit("caption", bundles, anchor) else None
        for p in paras:
            self.objects(p, anchor)
        return found

    def picture(self, pic: ET.Element, anchor: int) -> None:
        """그림 블록과 캡션(TOP이면 앞, 아니면 뒤). 이미지가 있을 때만 caption_ref로 잇는다(스펙 §7.4)."""
        caption = pic.find(CAPTION)
        top = caption is not None and caption.get("side") == "TOP"
        found = self.caption(caption, anchor) if caption is not None and top else None
        image = self.image(pic, anchor)
        spec = self.figure(image, anchor)
        if caption is not None and not top:
            found = self.caption(caption, anchor)
        if image is not None and found is not None:
            spec["_caption"] = found

    def image(self, pic: ET.Element, anchor: int) -> dict[str, Any] | None:
        """hc:img → manifest → 멤버 → PNG 자산. 못 담으면 처리 이력을 남기고 None."""
        img = pic.find(IMG)
        item = self.doc.package.items.get(img.get("binaryItemIDRef", "") if img is not None else "")
        reason: str
        if item is not None and not item.embedded:
            reason = FIGURE_LINKED
        elif item is None or not self.doc.package.has(item.href):
            reason = FIGURE_MISSING
        else:
            result = self.doc.pictures.get(item.href)
            if result is None:
                try:
                    result = images.to_png(self.doc.package.binary(item.href))
                except OverBudget:
                    result = FIGURE_BUDGET
                except TooLarge:
                    result = images.TOO_LARGE
                except ParseError:  # 그림 멤버의 zip 오류(CRC 등): 그 그림만 잃는다
                    result = images.UNREADABLE
                self.doc.pictures[item.href] = result
            if isinstance(result, images.Png):
                key = self.doc.budget.add(result.data)
                if key is not None:
                    size = pic.find(CUR_SZ)
                    return {"asset": key, "mime": "image/png", "width_px": result.width, "height_px": result.height,
                            "dpi": images.dpi(result.width, size.get("width") if size is not None else None),
                            "category": "image"}
                reason = FIGURE_BUDGET
                self.doc.pictures[item.href] = reason  # 담지 못한 PNG 바이트는 들고 있지 않는다(used는 줄지 않는다)
            else:
                reason = result
        self.region("figure", "figure", reason, anchor)
        return None

    def table(self, tbl: ET.Element, anchor: int) -> None:
        """1×1은 상자: 칸 문단을 이 자리의 본문 문단처럼(컨트롤·글상자 안이면 plain). 그 밖은 표 블록(칸 안 그림 등은
        표 뒤). 캡션은 TOP·LEFT면 앞."""
        caption = tbl.find(CAPTION)
        before = caption is not None and caption.get("side") in ("TOP", "LEFT")
        if caption is not None and before:
            self.caption(caption, anchor)
        sub = _box(tbl)
        if sub is not None:
            for p in sub.iterfind(P):
                (self.plain if self.inner else self.paragraph)(p, anchor)  # 컨트롤·글상자 안 상자는 제목 판정 밖
        else:
            self.grid(tbl, number(tbl.get("rowCnt")), number(tbl.get("colCnt")), anchor)
        if caption is not None and not before:
            self.caption(caption, anchor)

    def grid(self, tbl: ET.Element, rows: int | None, cols: int | None, anchor: int) -> None:
        after: list[ET.Element] = []
        cells: list[RawCell] = []
        parts: list[list[Bundle]] = []
        for tc in tbl.iterfind(f"{TR}/{TC}"):
            addr, span, sub = tc.find(CELL_ADDR), tc.find(CELL_SPAN), tc.find(SUBLIST)
            bundles = self.cell(sub, anchor, after) if sub is not None else []
            parts.append(bundles)
            cells.append(RawCell(
                row=number(addr.get("rowAddr") if addr is not None else None),
                col=number(addr.get("colAddr") if addr is not None else None),
                rowspan=number(span.get("rowSpan") if span is not None else None, 1),
                colspan=number(span.get("colSpan") if span is not None else None, 1),
                header=tc.get("header") == "1", filled=self.doc.styles.fills.get(tc.get("borderFillIDRef", ""), False),
                text=_join(bundles).strip()))
        table = tables.build(rows, cols, cells)
        if isinstance(table, Table):
            self.doc.specs.append({"kind": "table", "table": table, "text": table.plain_text(), "confidence": 1.0,
                                   "state": "det", "text_source": "native", "_anchor": anchor, "_late": self.late})
            for bundles in parts:
                self.ledger.put(bundles)
        else:
            joined: list[Bundle] = []
            for i, bundles in enumerate(parts):
                joined += [(None, "\n")] * bool(i) + bundles
            self.emit("paragraph", joined, anchor)
            self.region("table", "table", table, anchor)
        for element in after:
            self.obj(element, anchor)

    def cell(self, sub: ET.Element, anchor: int, after: list[ET.Element]) -> list[Bundle]:
        """칸 글자: 문단을 \\n으로 잇고, 박힌 개체는 문서 순서대로 그 자리에 글자로(앞뒤 \\n). 그림·OLE·머리말은 after로
        (표 뒤 블록), 각주·미주는 구역 끝."""
        out: list[Bundle] = []
        for i, p in enumerate(sub.iterfind(P)):
            if i:
                out.append((None, "\n"))
            for run in p.iterfind(RUN):
                for child in run:
                    if child.tag == T:
                        out.append(self.source_bundle(child, anchor, p))
                    else:
                        self.inline(child, anchor, after, out)
        return out

    def inline(self, element: ET.Element, anchor: int, after: list[ET.Element], out: list[Bundle]) -> None:
        tag = element.tag
        inner: list[Bundle] = []
        if tag == TBL:
            inner = self.flatten(element, anchor, after)
        elif tag == CONTAINER:
            for child in element:
                self.inline(child, anchor, after, out)
        elif tag in SHAPES:
            sub = element.find(f"{DRAW_TEXT}/{SUBLIST}")
            if sub is not None:
                if self.has_text(sub):
                    self.region("shape", "paragraph", SHAPE_TEXT, anchor)
                inner = self.cell(sub, anchor, after)
        elif tag == EQUATION:
            script = element.findtext(SCRIPT) or ""
            if script.strip():
                self.region("equation", "paragraph", EQUATION_SCRIPT, anchor)
                inner = [(None, script)]
        elif tag == TEXTART:
            if element.get("text", "").strip():
                self.region("shape", "paragraph", SHAPE_TEXT, anchor)
            inner = [(self.ledger.source_of(element), element.get("text", ""))]
        elif tag == PIC or tag in EMBEDDED or tag in MARGINS:
            after.append(element)
        elif tag in NOTES:
            self.notes.append((element, anchor))
        elif tag == CTRL:
            for child in element:
                self.inline(child, anchor, after, out)
        if inner:
            out += [(None, "\n"), *inner, (None, "\n")]

    def flatten(self, tbl: ET.Element, anchor: int, after: list[ET.Element]) -> list[Bundle]:
        """칸 안 표의 글자: 1×1은 그 문단 글자(기록 없음), 그 밖은 Table.plain_text()와 같은 모양(칸 \\t, 행 \\n)과
        nested_table_flattened. 캡션 글자는 TOP·LEFT면 앞, 아니면 뒤."""
        out: list[Bundle] = []
        caption = tbl.find(CAPTION)
        words = self.cell(caption.find(SUBLIST), anchor, after) if caption is not None and caption.find(
            SUBLIST) is not None else []
        before = caption is not None and caption.get("side") in ("TOP", "LEFT")
        if before and words:
            out += words + [(None, "\n")]
        sub = _box(tbl)
        if sub is not None:
            out += self.cell(sub, anchor, after)
        else:
            self.region("nested-table", "table", NESTED_TABLE, anchor)
            rows: dict[int, list[tuple[int, list[Bundle]]]] = {}
            for tc in tbl.iterfind(f"{TR}/{TC}"):
                addr, sub = tc.find(CELL_ADDR), tc.find(SUBLIST)
                row = number(addr.get("rowAddr") if addr is not None else None) or 0
                col = number(addr.get("colAddr") if addr is not None else None) or 0
                rows.setdefault(row, []).append((col, self.cell(sub, anchor, after) if sub is not None else []))
            for i, row in enumerate(sorted(rows)):
                if i:
                    out.append((None, "\n"))
                for k, (_, bundles) in enumerate(sorted(rows[row], key=lambda item: item[0])):
                    out += [(None, "\t")] * bool(k) + bundles
        if not before and words:
            out += [(None, "\n")] + words
        return out

    def sizes(self, p: ET.Element) -> Counter[tuple[float, bool]]:
        """문단 런들의 (0.5pt 단위 크기, 굵기)별 공백 아닌 글자 수."""
        out = Counter[tuple[float, bool]]()
        for run in p.iterfind(RUN):
            shape = self.doc.styles.chars.get(run.get("charPrIDRef", ""), NO_SHAPE)
            n = sum(ink(_t_text(t)[0]) for t in run.iterfind(T))
            if n:
                out[(step(shape.size), shape.bold)] += n
        return out

    def build(self) -> None:
        anchor = self.base - 1
        for top in self.root:
            if top.tag != P:
                continue
            anchor += 1
            self.paragraph(top, anchor)
        self.late = True
        self.end_notes()
        self.rescue()
        counts = self.ledger.count()
        self.doc.counts.append(counts)
        if not counts.ok:
            self.doc.regions.append(_Region("coverage", "paragraph", COVERAGE_MISMATCH, self.index, self.base,
                                            _ledger_gate(counts), f"s{self.index}-coverage"))

    def rescue(self) -> None:
        """한 번도 넣지 않은 출처를 닻 문단별로 모아 구조 문단으로(문서 순서). 같은 문단 출처는 잇고 문단은 \\n."""
        groups: dict[int, list[int]] = {}
        for k in self.ledger.unplaced():
            groups.setdefault(self.ledger.sources[k].anchor, []).append(k)
        rescued = 0
        for anchor, keys in groups.items():
            bundles: list[Bundle] = []
            for i, k in enumerate(keys):
                source = self.ledger.sources[k]
                if i and source.para != self.ledger.sources[keys[i - 1]].para:
                    bundles.append((None, "\n"))
                if source.element.tag == TEXTART:
                    bundles.append((k, source.element.get("text", "")))
                else:
                    bundles.append((k, _t_text(source.element)[0]))
            if self.emit("paragraph", bundles, anchor, confidence=RESCUED_CONFIDENCE):
                rescued += sum(ink(text) for _, text in bundles)
        if rescued:
            self.ledger.rescued = rescued
            first = next(iter(groups))
            self.region("unassigned-text", "paragraph", UNASSIGNED_TEXT, first, _gate("rescued", rescued, "==0"))


def _gate(name: str, value: int, threshold: str | None) -> GateResult:
    """근거 숫자 하나. 기준이 있으면 실패 검사(버그 신호), 없으면 기록용 통과 검사."""
    passed = threshold is None
    return GateResult(passed=passed, checks=(GateCheck(name=name, passed=passed, value=value, threshold=threshold),))


def _ledger_gate(counts: Counts) -> GateResult:
    """coverage_mismatch 근거: in_blocks(기준 source − hidden), doubled·partial(기준 0), hidden(기록)."""
    expected = counts.source - counts.hidden
    checks = (GateCheck(name="in_blocks", passed=counts.in_blocks == expected, value=counts.in_blocks,
                        threshold=f"=={expected}"),
              GateCheck(name="doubled", passed=counts.doubled == 0, value=counts.doubled, threshold="==0"),
              GateCheck(name="partial", passed=counts.partial == 0, value=counts.partial, threshold="==0"),
              GateCheck(name="hidden", passed=True, value=counts.hidden))
    return GateResult(passed=all(c.passed for c in checks), checks=checks)


def _headings_by_size(specs: list[dict[str, Any]]) -> None:
    """개요 수준이 없는 문서: PDF와 같은 상수로 제목을 고른다. 본문 크기 = 대상 문단 글자 수로 센 크기 최빈값(같으면
    작은 쪽), 제목 = 대표 크기 ≥ 본문 × 1.15(굵으면 × 1.05)이고 표시 줄 ≤ 2. 단계는 큰 크기부터 1, 2, …(6에서 접음)."""
    counts = Counter[float]()
    for spec in specs:
        for (size, _), n in spec.get("_sizes", Counter()).items():
            counts[size] += n
    if not counts:
        return
    body = max(counts, key=lambda s: (counts[s], -s))
    if body <= 0:  # 크기가 0 이하인 문서는 크기 규칙을 쓰지 않는다(모든 문단이 제목이 되지 않게)
        return
    chosen = [spec for spec in specs if spec.get("_size") is not None and spec["_lines"] <= HEADING_MAX_LINES
              and spec["_size"] >= body * (BOLD_HEADING_RATIO if spec["_bold"] else HEADING_RATIO)]
    ranks = sorted({spec["_size"] for spec in chosen}, reverse=True)
    for spec in chosen:
        # 제목은 한 줄로(PDF처럼 줄을 공백으로 잇는다): 줄바꿈이 목록·제목 경로·마크다운 제목을 깨지 않게
        spec.update(kind="heading", level=min(ranks.index(spec["_size"]) + 1, MAX_LEVEL),
                    text=" ".join(spec["text"].split()), confidence=CONFIDENCE["heading"])


def _finish(doc: _Doc) -> tuple[tuple[dict[str, Any], ...], tuple[RegionRecord, ...]]:
    """제목을 정하고 제목 경로를 채운다. 구역 끝 블록(_late)과 처리 이력은 닻 문단 자리의 제목 경로를 쓴다."""
    if not doc.outline:
        _headings_by_size(doc.specs)
    index = {id(spec): k for k, spec in enumerate(doc.specs)}
    stack: list[tuple[int, str]] = []
    marks: list[tuple[int, tuple[str, ...]]] = []  # (닻, 그 닻까지 처리한 뒤 제목 경로)
    blocks: list[dict[str, Any]] = []
    for spec in doc.specs:
        anchor = spec["_anchor"]
        if spec["_late"]:
            path = _path_at(marks, anchor)
        else:
            if spec["kind"] == "heading":
                while stack and stack[-1][0] >= spec["level"]:
                    stack.pop()
            path = tuple(text for _, text in stack)
            if spec["kind"] == "heading":
                stack.append((spec["level"], spec["text"]))
            marks.append((anchor, tuple(text for _, text in stack)))
        block = {k: v for k, v in spec.items() if not k.startswith("_")}
        if "_caption" in spec:  # 짝 캡션 명세의 순번(core/build.py가 caption_block_id로 바꾼다)
            block["figure"] = {**block["figure"], "caption_ref": index[id(spec["_caption"])]}
        block["section_path"] = path
        block["locator"] = FlowLocator(section_path=path, paragraph_index=anchor)
        blocks.append(block)
    seen = Counter[tuple[int, int, str]]()
    records = []
    for region in doc.regions:
        region_id = region.region_id
        if region_id is None:
            key = (region.section, region.anchor, region.tag)
            region_id = f"s{region.section}-p{region.anchor}-{region.tag}-{seen[key]}"
            seen[key] += 1
        records.append(RegionRecord(region_id=region_id, kind=region.kind, chosen="det",
                                    attempts=(Attempt(layer="det"),), fallback_reason=region.reason, gate=region.gate,
                                    locator=FlowLocator(section_path=_path_at(marks, region.anchor),
                                                        paragraph_index=region.anchor)))
    return tuple(blocks), tuple(records)


def _path_at(marks: list[tuple[int, tuple[str, ...]]], anchor: int) -> tuple[str, ...]:
    """닻 문단까지 처리한 뒤의 제목 경로(그 닻 이하의 마지막 표시)."""
    k = bisect.bisect_right(marks, anchor, key=lambda m: m[0])
    return marks[k - 1][1] if k else ()


class HwpxParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".hwpx",)

    def parse(self, data: bytes, name: str) -> ParsedSource:
        return self._parse(data, name)[0]

    def _parse(self, data: bytes, name: str) -> tuple[ParsedSource, tuple[Counts, ...]]:
        """ParsedSource와 구역별 장부 숫자(테스트가 본다)."""
        package = Package(data, name)
        doc = _Doc(name, read_styles(package.xml("Contents/header.xml", f"{HH}head")), package)
        base = 0
        for index, member in enumerate(package.sections):
            root = package.xml(member, f"{HS}sec")
            section = _Section(doc, index, root, base)
            section.build()
            base += sum(1 for child in root if child.tag == P)
            del section, root  # 다음 구역 전에 트리를 버린다
        for k, member in enumerate(package.masterpages):
            chars = sum(source_chars(t) for t in package.xml(member, None).iter(T))
            if chars:
                doc.regions.append(_Region("masterpage-text", "paragraph", MASTERPAGE_TEXT, 0, 0,
                                           _gate("chars", chars, None), f"masterpage-{k}"))
        blocks, regions = _finish(doc)
        return (ParsedSource(mime=MIME, blocks=blocks, assets=doc.budget.assets, regions=regions),
                tuple(doc.counts))
