"""구역 레코드 트리 → OWPML 부분집합 요소 트리(HWP 스펙 §7)와 옮겨 쓰기 장부(§6).

HWPX 구역 처리부(hwpx/owpml.py)가 읽는 요소·속성만 만든다: 최상위 PARA_HEADER마다 hs:sec 아래 hp:p(paraPrIDRef·
styleIDRef), PARA_CHAR_SHAPE 경계마다 hp:run(charPrIDRef), 글자는 hp:t(탭·줄바꿈·묶음 빈칸·고정폭 빈칸·제목 차례
표시는 hp:t의 자식, 뒤 글자는 그 tail), PARA_LINE_SEG 항목마다 hp:lineseg. 확장 컨트롤은 문단의 k번째 자식 CTRL_HEADER와
짝짓는다. HP-A의 컨트롤: 글자 없는 자리 컨트롤(필드·단 정의·쪽 번호 등)은 빈 hp:ctrl, 덧말·글자 겹침은 글자 수만
(control_text_skipped), 그 밖(표·그리기 개체·머리말 등과 모르는 ID)은 hp:ctrl/hp:unknownObject/hp:subList 아래로 그
아래 문단을 옮긴다(처리부가 모르는 요소라 블록이 없고 장부가 글자를 살린다, unknown_control).

옮겨 쓰기 장부: 글자 레코드(PARA_TEXT 전부와 덧말·글자 겹침 CTRL_HEADER)마다 원본에서 따로 디코드한 정규 문자열 R(r)과
트리에서 r로 표시된 hp:t들의 글자를 문서 순서로 이은 T(r)이 정확히 같거나, r을 기록과 함께 건너뛰었어야 한다. 처리부가
읽지 않는 자리(hp:t 자신의 tail, 그 밖 요소의 text·tail)의 글자는 떠돌이(stray)로 센다. 표시는 요소 속성이 아니라 옆
사전(id(요소) → 레코드 순번)이다(처리부가 보는 트리를 바꾸지 않는다). 요소 수는 문서 전체 MAX_NODES, 트리 깊이는
MAX_XML_DEPTH 안이다(넘으면 ParseError). 보통 글자(≥ 0x20)는 글자마다 객체를 만들지 않고 구간째 디코드한다."""

import re
import struct
import sys
import xml.etree.ElementTree as ET
from array import array
from dataclasses import dataclass, field

from hanji_contracts import GateCheck, GateResult

from ...errors import ParseError
from ..flowdoc import Region, gate
from ..flowledger import ink
from ..hwpx import container
from ..hwpx.container import HP, HS
from ..pdf.parser import COVERAGE_MISMATCH
from .stream import Record

PARA_HEADER, PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG, CTRL_HEADER, LIST_HEADER = 0x42, 0x43, 0x44, 0x45, 0x47, 0x48
# 사양 표에 있는 태그(DocInfo 0x10–0x20·0x5C·0x5E·0x60·0x61, 본문 0x42–0x5D·0x5F·0x62·0x73): 그 밖은 unknown_record
KNOWN_TAGS = frozenset([*range(0x10, 0x21), *range(0x42, 0x5E), 0x5E, 0x5F, 0x60, 0x61, 0x62, 0x73])
MAX_NODES = 4_000_000  # 문서 전체 옮겨 쓴 요소 수
LINE_SEG = 36  # PARA_LINE_SEG 항목 크기(실측 43,744개 모두)
# 제어 문자 너비 표는 runs(옮겨 쓰기)와 canonical(R(r))이 함께 쓴다. 표는 사양·실측과 맞다. 틀리면 양쪽이 같이
# 삼키므로 장부는 못 잡는다(쌍둥이 골든·실문서 확장 컨트롤 100% 짝이 막는다). 디코드는 따로 한다
INLINE8 = frozenset((4, 5, 6, 7, 8, 9, 19, 20))  # 인라인 컨트롤(8 WCHAR)
EXTENDED = frozenset((1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23))  # 확장 컨트롤(8 WCHAR, CTRL_HEADER와 짝)
INLINE_TAGS = {9: f"{HP}tab", 8: f"{HP}titleMark", 10: f"{HP}lineBreak", 30: f"{HP}nbSpace", 31: f"{HP}fwSpace"}
P, RUN, T, CTRL = f"{HP}p", f"{HP}run", f"{HP}t", f"{HP}ctrl"
# 글자 없는 자리 컨트롤(빈 hp:ctrl). 필드(%로 시작)도 같다. 리스트·문단이 달리면 모르는 컨트롤 경로다
PLACES = frozenset(("secd", "cold", "atno", "nwno", "pghd", "pgct", "pgnp", "idxm", "bokm", "form", "tcps", "tdut"))
TEXT_CONTROLS = frozenset(("tcps", "tdut"))  # 글자가 CTRL_HEADER 몸에 있다(HWPX도 쓰지 않는다): 수만 남긴다
INVALID_UTF16 = "invalid_utf16"
UNKNOWN_CONTROL_CHAR = "unknown_control_char"
UNKNOWN_CONTROL = "unknown_control"
CONTROL_TEXT = "control_text_skipped"
RECORD_INCONSISTENT = "record_inconsistent"
UNKNOWN_RECORD = "unknown_record"
# 보통 글자(≥ 0x20) WCHAR의 이어진 구간(LE 바이트 두 개씩, 소유 반복이라 되짚기 기억이 없다)
SPAN = re.compile(rb"(?:[\x20-\xff][\x00-\xff]|[\x00-\x1f][\x01-\xff])*+")
LONE = re.compile(r"[\ud800-\udfff]")  # surrogatepass 디코드 뒤 남은 짝 없는 대리 문자
NATIVE16 = "utf-16-le" if sys.byteorder == "little" else "utf-16-be"


def wchars(body: bytes) -> array:
    """리틀 엔디언 WCHAR(UINT16) 배열(홀수 바이트의 끝은 버린다)."""
    out = array("H", body[:len(body) - len(body) % 2])
    if sys.byteorder == "big":
        out.byteswap()
    return out


def control_id(body: bytes) -> str:
    """CTRL_HEADER 몸 앞 UINT32를 큰 엔디언 네 글자로("tbl ", "secd" 등)."""
    return body[3::-1].decode("latin-1") if len(body) >= 4 else ""


def decode16(raw: bytes | memoryview, codec: str = "utf-16-le") -> tuple[str, int]:
    """UTF-16 바이트 → 글자(대리쌍 묶음)와 짝 없는 대리 문자 수(U+FFFD로 바꾼다). runs·utf16이 쓴다(canonical의
    PARA_TEXT는 따로 디코드한다)."""
    return LONE.subn("�", str(raw, codec, "surrogatepass"))


def utf16(units: array) -> tuple[str, int]:
    """WCHAR들 → 글자(대리쌍 묶음)와 짝 없는 대리 문자 수(U+FFFD로 바꾼다)."""
    return decode16(units.tobytes(), NATIVE16)


def control_text(body: bytes, ident: str, location: str) -> str:
    """덧말(tdut: 본문 + 덧말 글자)·글자 겹침(tcps: 겹칠 글자)의 글자. 길이(WORD) 뒤 WCHAR들(사양 표 150·151)."""
    out = []
    at = 4
    for _ in range(2 if ident == "tdut" else 1):
        if len(body) < at + 2:
            raise ParseError(f"CTRL_HEADER {ident} record too short", location)
        (n,) = struct.unpack_from("<H", body, at)
        if len(body) < at + 2 + 2 * n:
            raise ParseError(f"CTRL_HEADER {ident} record too short", location)
        out.append(utf16(wchars(body[at + 2:at + 2 + 2 * n]))[0])
        at += 2 + 2 * n
    return "".join(out)


@dataclass(slots=True)
class Nodes:
    """문서 전체 옮겨 쓴 요소 수(MAX_NODES)."""

    count: int = 0


@dataclass(frozen=True, slots=True)
class Audit:
    """옮겨 쓰기 장부 숫자(구역 하나): 어긋난 레코드, 표시도 건너뛴 기록도 없는 레코드, 떠돌이 글자."""

    mismatched: int
    orphans: int
    stray: int

    @property
    def ok(self) -> bool:
        return self.mismatched == 0 and self.orphans == 0 and self.stray == 0


@dataclass(slots=True)
class Section:
    """옮겨 쓴 구역: 트리, 처리 이력(처리부 앞), 장부 숫자, 장부 처리 이력(있으면)."""

    root: ET.Element
    regions: list[Region] = field(default_factory=list)
    audit: Audit = Audit(0, 0, 0)


class _Writer:
    def __init__(self, index: int, base: int, location: str, nodes: Nodes) -> None:
        self.index = index
        self.base = base
        self.location = location
        self.nodes = nodes
        self.marks: dict[int, int] = {}  # id(hp:t) → PARA_TEXT 레코드 순번(트리가 요소를 붙잡고 있다)
        self.skipped: set[int] = set()  # 기록과 함께 건너뛴 글자 레코드
        self.regions: list[Region] = []
        self.unknown: set[int] = set()  # unknown_control을 남긴 문단(id)

    def node(self, parent: ET.Element, tag: str, depth: int, **attrs: str) -> ET.Element:
        self.nodes.count += 1
        if self.nodes.count > MAX_NODES:
            raise ParseError(f"more than {MAX_NODES} transcoded elements in the document", self.location)
        if depth > container.MAX_XML_DEPTH:
            raise ParseError(f"transcoded tree deeper than {container.MAX_XML_DEPTH}", self.location)
        return ET.SubElement(parent, tag, attrs)

    def region(self, tag: str, reason: str, anchor: int, name: str | None = None, value: int = 0) -> None:
        self.regions.append(Region(tag, "paragraph", reason, self.index, anchor,
                                   gate(name, value, None) if name is not None else None))

    def paragraph(self, parent: ET.Element, header: Record, anchor: int, depth: int) -> ET.Element:
        """PARA_HEADER 하나 → hp:p(depth는 hp:p의 깊이)."""
        body = header.body
        if len(body) < 11:
            raise ParseError("PARA_HEADER record too short", f"{self.location}#{header.index}")
        (nchars,) = struct.unpack_from("<I", body, 0)
        (shape,) = struct.unpack_from("<H", body, 8)
        p = self.node(parent, P, depth, paraPrIDRef=str(shape), styleIDRef=str(body[10]))
        kids = header.children
        text = next((r for r in kids if r.tag == PARA_TEXT), None)
        controls = [r for r in kids if r.tag == CTRL_HEADER]
        bounds = self.bounds(next((r for r in kids if r.tag == PARA_CHAR_SHAPE), None))
        units = wchars(text.body) if text is not None else array("H")
        if text is not None and nchars & 0x7FFFFFFF != len(units):
            self.region("record-inconsistent", RECORD_INCONSISTENT, anchor, "nchars", 1)
        self.runs(p, units, text, bounds, controls, anchor, depth)
        segs = next((r for r in kids if r.tag == PARA_LINE_SEG), None)
        if segs is not None:
            array_ = self.node(p, f"{HP}linesegarray", depth + 1)
            for _ in range(len(segs.body) // LINE_SEG):
                self.node(array_, f"{HP}lineseg", depth + 2)
        return p

    def bounds(self, record: Record | None) -> list[tuple[int, str]]:
        """(시작 위치, 글자 모양 id) 쌍(8B). 없으면 [(0, "0")]."""
        if record is None or len(record.body) < 8:
            return [(0, "0")]
        return [struct.unpack_from("<II", record.body, 8 * k) for k in range(len(record.body) // 8)]

    def runs(self, p: ET.Element, units: array, text: Record | None, bounds: list, controls: list[Record],
             anchor: int, depth: int) -> None:
        """글자를 낱말 단위(보통 글자 구간·컨트롤·표지)로 나누고, 글자 모양 경계(컨트롤·대리쌍 안이면 그 시작으로
        당긴다)마다 hp:run을 연다. 같은 자리 경계가 둘이면 뒤 것을 쓴다. 보통 글자 구간은 바이트 조각째 디코드하고
        구간 안 경계에서만 끊는다(당긴 경계는 대리쌍을 가르지 않는다)."""
        tokens: list[tuple[int, int, str, object]] = []  # (시작, 길이, 종류, 값). 보통 글자 구간은 "span" 하나
        body = text.body if text is not None else b""
        view = memoryview(body)
        n = len(units)
        bad = unknown = ext = mismatch = 0
        end = None
        j = 0
        location = f"{self.location}#{text.index}" if text is not None else self.location
        while j < n:
            stop = SPAN.match(body, 2 * j, 2 * n).end() // 2  # type: ignore[union-attr]
            if stop > j:  # 보통 글자 구간(대리 문자도 여기)
                bad += decode16(view[2 * j:2 * stop])[1]
                tokens.append((j, stop - j, "span", None))
                j = stop
                continue
            w = units[j]
            if w in INLINE8 or w in EXTENDED:
                if j + 8 > n:
                    raise ParseError("truncated control in PARA_TEXT", location)
                if w in EXTENDED:
                    ident = units[j + 1] | units[j + 2] << 16
                    if ext < len(controls):
                        mismatch += struct.unpack_from("<I", controls[ext].body.ljust(4, b"\0"))[0] != ident
                        tokens.append((j, 8, "ctrl", controls[ext]))
                    else:
                        mismatch += 1
                    ext += 1
                elif w in INLINE_TAGS:
                    tokens.append((j, 8, "inline", INLINE_TAGS[w]))
                elif w == 4:
                    tokens.append((j, 8, "fieldEnd", None))
                else:
                    unknown += 1
                j += 8
                continue
            if w == 13:
                end = j if end is None else end
            elif w in (10, 30, 31):
                tokens.append((j, 1, "inline", INLINE_TAGS[w]))
            elif w == 24:
                tokens.append((j, 1, "char", "-"))
            else:
                unknown += 1
            j += 1
        mismatch += max(0, len(controls) - ext)
        trailing = n - end - 1 if end is not None else 0
        for name, reason, value in (("chars", INVALID_UTF16, bad), ("chars", UNKNOWN_CONTROL_CHAR, unknown),
                                    ("control_mismatch", RECORD_INCONSISTENT, mismatch),
                                    ("trailing_chars", RECORD_INCONSISTENT, trailing)):
            if value:
                self.region(reason.replace("_", "-"), reason, anchor, name, value)
        starts = sorted({(self.snap(int(pos), tokens, units), k) for k, (pos, _) in enumerate(bounds)})
        chosen: dict[int, str] = {}
        for pos, k in starts:
            chosen[pos] = str(bounds[k][1])
        order = sorted(chosen)
        run: ET.Element | None = None
        t: ET.Element | None = None
        last: ET.Element | None = None  # 다음 글자가 붙을 요소(t면 text, 인라인 자식이면 tail)
        buffer: list[str] = []

        def flush() -> None:
            if last is not None and buffer:
                if last is t:
                    last.text = "".join(buffer)
                else:
                    last.tail = "".join(buffer)
            buffer.clear()

        cursor = 0
        for start, length, kind, value in tokens:
            if run is None or (cursor < len(order) and order[cursor] <= start):
                flush()
                t = last = None
                while cursor < len(order) and order[cursor] <= start:
                    cursor += 1
                run = self.node(p, RUN, depth + 1, charPrIDRef=chosen[order[max(cursor - 1, 0)]] if order else "0")
            if kind in ("span", "char", "inline"):
                if t is None:
                    t = last = self.node(run, T, depth + 2)
                    if text is not None:
                        self.marks[id(t)] = text.index
                if kind == "span":
                    a, stop = start, start + length
                    while cursor < len(order) and order[cursor] < stop:  # 구간 안 경계: 끊고 새 런
                        cut = order[cursor]
                        buffer.append(decode16(view[2 * a:2 * cut])[0])
                        flush()
                        cursor += 1
                        run = self.node(p, RUN, depth + 1, charPrIDRef=chosen[cut])
                        t = last = self.node(run, T, depth + 2)
                        if text is not None:
                            self.marks[id(t)] = text.index
                        a = cut
                    buffer.append(decode16(view[2 * a:2 * stop])[0])
                elif kind == "char":
                    buffer.append(str(value))
                else:
                    flush()
                    last = self.node(t, str(value), depth + 3)
            else:
                flush()
                t = last = None
                if kind == "fieldEnd":
                    self.node(self.node(run, CTRL, depth + 2), f"{HP}fieldEnd", depth + 3)
                else:
                    self.control(run, value, p, anchor, depth + 2)  # type: ignore[arg-type]
        flush()
        while cursor < len(order):  # 글자 뒤 경계(빈 런)
            run = self.node(p, RUN, depth + 1, charPrIDRef=chosen[order[cursor]])
            cursor += 1
        if run is None:
            run = self.node(p, RUN, depth + 1, charPrIDRef=chosen[order[0]] if order else "0")
        for record in controls[ext:]:  # 짝 없는 컨트롤은 문단 끝 자리 개체로
            self.control(run, record, p, anchor, depth + 2)

    @staticmethod
    def snap(pos: int, tokens: list[tuple[int, int, str, object]], units: array) -> int:
        """경계가 낱말(컨트롤 8 WCHAR, 보통 글자 구간 안의 대리쌍) 안이면 그 시작으로."""
        lo, hi = 0, len(tokens)
        while lo < hi:  # pos 이하에서 시작하는 마지막 낱말
            mid = (lo + hi) // 2
            if tokens[mid][0] <= pos:
                lo = mid + 1
            else:
                hi = mid
        if lo and tokens[lo - 1][0] < pos < tokens[lo - 1][0] + tokens[lo - 1][1]:
            if tokens[lo - 1][2] != "span":
                return tokens[lo - 1][0]
            # 구간 안: 앞이 상위·여기가 하위 대리 문자면 대리쌍 한가운데(구간 안 상위 대리 문자는 늘 짝의 앞이다)
            if 0xD800 <= units[pos - 1] < 0xDC00 and 0xDC00 <= units[pos] < 0xE000:
                return pos - 1
        return pos

    def control(self, run: ET.Element, record: Record, p: ET.Element, anchor: int, depth: int) -> None:
        """CTRL_HEADER 하나(depth는 hp:ctrl의 깊이). 자리 컨트롤은 빈 hp:ctrl, 덧말·글자 겹침은 글자 수만, 그 밖은
        hp:unknownObject 아래로 그 아래 문단들(레코드 차례)을 옮긴다."""
        ident = control_id(record.body)
        paras = self.paragraphs(record)
        lists = any(r.tag == LIST_HEADER for r in self.descendants(record))
        ctrl = self.node(run, CTRL, depth)
        simple = ident in PLACES or ident.startswith("%")
        if simple and not paras and not lists:
            if ident in TEXT_CONTROLS:
                chars = ink(control_text(record.body, ident, f"{self.location}#{record.index}"))
                self.skipped.add(record.index)
                if chars:
                    self.region("control-text", CONTROL_TEXT, anchor, "chars", chars)
            return
        if id(p) not in self.unknown:
            self.unknown.add(id(p))
            self.region("unknown-control", UNKNOWN_CONTROL, anchor)
        sub = self.node(self.node(ctrl, f"{HP}unknownObject", depth + 1), f"{HP}subList", depth + 2)
        for header in paras:
            self.paragraph(sub, header, anchor, depth + 3)

    @staticmethod
    def descendants(record: Record) -> list[Record]:
        """컨트롤 아래 레코드(문단 안으로는 들어가지 않는다), 앞 순회 차례."""
        out: list[Record] = []
        stack = list(reversed(record.children))
        while stack:
            r = stack.pop()
            out.append(r)
            if r.tag != PARA_HEADER:
                stack.extend(reversed(r.children))
        return out

    def paragraphs(self, record: Record) -> list[Record]:
        return [r for r in self.descendants(record) if r.tag == PARA_HEADER]


def canonical(record: Record, location: str) -> str:
    """R(r): 옮겨 쓰기와 따로 디코드한 정규 문자열. PARA_TEXT는 보통 글자(대리쌍 묶음, 짝 없는 대리 문자 → U+FFFD)와
    표지 글자(탭 \\t, 줄바꿈 \\n, 묶음 빈칸 \\x1e, 고정폭 빈칸 \\x1f, 하이픈 -), 그 밖 제어 문자·컨트롤은 뺀다.
    runs와 다른 길: 제어 문자 자리를 WCHAR마다 찾고 그 사이 바이트를 errors="replace"로 디코드한다(짝 없는 대리
    WCHAR 하나 → U+FFFD 하나). location은 스트림 위치(덧말·글자 겹침 몸이 짧으면 ParseError에 #순번을 붙인다)."""
    if record.tag == CTRL_HEADER:
        return control_text(record.body, control_id(record.body), f"{location}#{record.index}")
    raw = record.body
    even = memoryview(raw)[:len(raw) - len(raw) % 2]
    units = even.cast("H") if sys.byteorder == "little" else wchars(raw)  # 복사 없이 WCHAR로 본다
    out: list[str] = []
    marks = {9: "\t", 10: "\n", 30: "\x1e", 31: "\x1f", 24: "-"}
    a = j = 0
    while j < len(units):
        w = units[j]
        if w >= 0x20:
            j += 1
            continue
        if j > a:
            out.append(str(even[2 * a:2 * j], "utf-16-le", "replace"))
        if w in marks:
            out.append(marks[w])
        j += 8 if w in INLINE8 or w in EXTENDED else 1
        a = j
    if len(units) > a:
        out.append(str(even[2 * a:], "utf-16-le", "replace"))
    return "".join(out)


MARKS = {f"{HP}tab": "\t", f"{HP}lineBreak": "\n", f"{HP}nbSpace": "\x1e", f"{HP}fwSpace": "\x1f",
         f"{HP}titleMark": ""}


def tree_texts(root: ET.Element, marks: dict[int, int]) -> dict[int, str]:
    """T(r): r로 표시된 hp:t들을 문서 순서로 훑어 처리부가 읽는 자리(text, 자식마다 표지 글자 또는 그 안 글자, 자식
    tail)만 이은 것."""
    out: dict[int, list[str]] = {}
    for element in root.iter():
        k = marks.get(id(element))
        if k is None:
            continue
        parts = [element.text or ""]
        for child in element:
            parts.append(MARKS[child.tag] if child.tag in MARKS else "".join(child.itertext()))
            parts.append(child.tail or "")
        out.setdefault(k, []).append("".join(parts))
    return {k: "".join(v) for k, v in out.items()}


def stray(root: ET.Element) -> int:
    """처리부가 읽지 않는 자리(hp:t 자신의 tail, hp:t 자식의 text, 그 밖 요소의 text·tail)의 공백 아닌 글자 수."""
    count = 0
    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    while stack:
        element, in_t = stack.pop()
        if element.tag == T and not in_t:
            count += ink(element.tail or "")
        elif in_t:
            count += ink(element.text or "")
        else:
            count += ink(element.text or "") + ink(element.tail or "")
        stack.extend((child, element.tag == T and not in_t) for child in element)
    return count


def audit(records: list[Record], root: ET.Element, marks: dict[int, int], skipped: set[int], location: str) -> Audit:
    """모든 글자 레코드(PARA_TEXT, 덧말·글자 겹침 CTRL_HEADER)를 레벨·부모와 상관없이 훑어 판정한다. location은 구역
    스트림 위치("{파일}:{스트림}", 오류에 #순번을 붙인다)."""
    found = tree_texts(root, marks)
    mismatched = orphans = 0
    stack = list(reversed(records))
    while stack:
        record = stack.pop()
        stack.extend(reversed(record.children))
        if record.tag != PARA_TEXT and not (record.tag == CTRL_HEADER and control_id(record.body) in TEXT_CONTROLS):
            continue
        text = found.get(record.index)
        if record.index in skipped:
            mismatched += text is not None
            continue
        expected = canonical(record, location)
        if text == expected or (text is None and expected == ""):
            continue
        if text is None:
            orphans += 1
        else:
            mismatched += 1
    return Audit(mismatched, orphans, stray(root))


def unknown_records(records: list[Record]) -> int:
    count = 0
    stack = list(records)
    while stack:
        record = stack.pop()
        count += record.tag not in KNOWN_TAGS
        stack.extend(record.children)
    return count


def section(records: list[Record], index: int, base: int, location: str, nodes: Nodes) -> Section:
    """구역 레코드 트리 → hs:sec 트리. 레벨 0 PARA_HEADER마다 최상위 hp:p(닻 = base + 차례). 레벨 0의 다른 레코드는
    옮기지 않는다(글자 레코드면 장부가 잡는다)."""
    writer = _Writer(index, base, location, nodes)
    root = ET.Element(f"{HS}sec")
    anchor = base - 1
    for record in records:
        if record.tag == PARA_HEADER:
            anchor += 1
            writer.paragraph(root, record, anchor, 2)
    result = Section(root, writer.regions)
    unknown = unknown_records(records)
    if unknown:
        result.regions.append(Region("unknown-record", "paragraph", UNKNOWN_RECORD, index, base,
                                     gate("records", unknown, None), f"s{index}-unknown-record"))
    result.audit = audit(records, root, writer.marks, writer.skipped, location)
    if not result.audit.ok:
        checks = tuple(GateCheck(name=name, passed=value == 0, value=value, threshold="==0") for name, value in (
            ("mismatched", result.audit.mismatched), ("orphans", result.audit.orphans),
            ("stray", result.audit.stray)))
        result.regions.append(Region("transcode", "paragraph", COVERAGE_MISMATCH, index, base,
                                     GateResult(passed=False, checks=checks), f"s{index}-transcode"))
    return result
