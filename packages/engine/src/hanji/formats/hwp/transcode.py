"""구역 레코드 트리 → OWPML 부분집합 요소 트리(HWP 스펙 §7)와 옮겨 쓰기 장부(§6).

HWPX 구역 처리부(hwpx/owpml.py)가 읽는 요소·속성만 만든다: 최상위 PARA_HEADER마다 hs:sec 아래 hp:p(paraPrIDRef·
styleIDRef), PARA_CHAR_SHAPE 경계마다 hp:run(charPrIDRef, 글자 뒤 경계는 빈 런 하나), 글자는 hp:t(탭·줄바꿈·묶음
빈칸·고정폭 빈칸·제목 차례 표시는 hp:t의 자식, 뒤 글자는 그 tail), PARA_LINE_SEG 항목마다 hp:lineseg. 확장 컨트롤은 문단의
k번째 자식 CTRL_HEADER와 짝짓는다. 컨트롤: 글자 없는 자리 컨트롤(필드·단 정의·쪽 번호 등)은 빈 hp:ctrl, 덧말·글자 겹침은
글자 수만(control_text_skipped), 표는 hp:tbl(TABLE 레코드의 행·열 수, 그 앞 리스트는 캡션, 뒤 리스트는 칸: LIST_HEADER
오프셋 8부터 열·행 주소·열·행 병합, 32 테두리 채움, 6의 bit 2 제목 칸), 그리기 개체(gso)는 첫 SHAPE_COMPONENT의 종류
ID로 hp:pic(그림 레코드 오프셋 71의 BinData 순번, orgSz는 처음 폭·높이, curSz는 현재 폭·높이로 처음 것과 같은 쪽은 0 —
HWPX와 같다)·도형(첫 리스트는 hp:drawText)·hp:container(자식 SHAPE_COMPONENT마다, ID는 한 번만 적힌다)·hp:ole·hp:chart·
hp:video·hp:textart@text, 그 앞 리스트는 캡션, 수식은 hp:equation/hp:script(EQEDIT 스크립트), 머리말·꼬리말은 hp:ctrl/hp:header·hp:footer
@applyPageType(몸 오프셋 4 bit 0–1: 양쪽·짝·홀), 각주·미주·숨은 설명은 hp:ctrl/hp:footNote·hp:endNote·hp:hiddenComment, 모두
첫 리스트가 hp:subList다. 구역 정의 아래 리스트(바탕쪽)는 옮기지 않고 글자 레코드를 건너뛴 것으로 적고 공백 아닌 글자 수만
센다(Section.masters, 파서가 masterpage_text_skipped로). 리스트는 LIST_HEADER와 그 뒤 같은 레벨 문단들이다.
몸 레코드가 없는 컨트롤과 모르는 ID는 hp:ctrl/hp:unknownObject/hp:subList 아래로 그 아래 문단을 옮기고(처리부가 모르는
요소라 블록이 없고 장부가 글자를 살린다, unknown_control), 아는 컨트롤에서 어느 리스트에도 들지 않은 문단도 그 뒤에 같은
길로 둔다(글자를 조용히 잃지 않는다). gso 아래 둘째 이후 SHAPE_COMPONENT는 옮기지 않고 unknown_control만 남긴다. 글맵시
글자를 읽지 못하면(배치 미검증: 길이 넘침·U+FFFD·제어 문자) 그 개체는 모르는 컨트롤 길이고 레코드는 record_inconsistent
(근거 textart)와 함께 건너뛴다.

옮겨 쓰기 장부: 글자 레코드(PARA_TEXT 전부, 덧말·글자 겹침 CTRL_HEADER, EQEDIT, 글맵시)마다 원본에서 따로 디코드한
정규 문자열 R(r)과 트리에서 r로 표시된 요소(hp:t, hp:script의 글자, hp:textart@text)의 글자를 문서 순서로 이은 T(r)이
정확히 같거나, r을 기록과 함께 건너뛰었어야 한다. 처리부가 읽지 않는 자리(hp:t 자신의 tail, hp:script 밖 요소의
text·tail)의 글자는 떠돌이(stray)로 센다. 표시는 요소 속성이 아니라 옆
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
from ..hwpx.container import HC, HP, HS
from ..pdf.parser import COVERAGE_MISMATCH
from .stream import Record

PARA_HEADER, PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG, CTRL_HEADER, LIST_HEADER = 0x42, 0x43, 0x44, 0x45, 0x47, 0x48
TABLE, SHAPE_COMPONENT, PICTURE, EQEDIT, TEXTART, CHART_DATA, VIDEO_DATA = 0x4D, 0x4C, 0x55, 0x58, 0x5A, 0x5F, 0x62
# 사양 표에 있는 태그(DocInfo 0x10–0x20·0x5C·0x5E·0x60·0x61, 본문 0x42–0x5D·0x5F·0x62·0x73): 그 밖은 unknown_record
KNOWN_TAGS = frozenset([*range(0x10, 0x21), *range(0x42, 0x5E), 0x5E, 0x5F, 0x60, 0x61, 0x62, 0x73])
MAX_NODES = 4_000_000  # 문서 전체 옮겨 쓴 요소 수
LINE_SEG = 36  # PARA_LINE_SEG 항목 크기(실측 43,744개 모두)
# 제어 문자 너비 표는 runs(옮겨 쓰기)와 canonical(R(r))이 함께 쓴다. 표는 사양·실측과 맞다. 틀리면 양쪽이 같이
# 삼키므로 장부는 못 잡는다(쌍둥이 골든·실문서 확장 컨트롤 100% 짝이 막는다). 디코드는 따로 한다
INLINE8 = frozenset((4, 5, 6, 7, 8, 9, 19, 20))  # 인라인 컨트롤(8 WCHAR)
EXTENDED = frozenset((1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23))  # 확장 컨트롤(8 WCHAR, CTRL_HEADER와 짝)
INLINE_TAGS = {9: f"{HP}tab", 8: f"{HP}titleMark", 10: f"{HP}lineBreak", 30: f"{HP}nbSpace", 31: f"{HP}fwSpace"}
P, RUN, T, CTRL, SUBLIST = f"{HP}p", f"{HP}run", f"{HP}t", f"{HP}ctrl", f"{HP}subList"
TBL, TR, TC, CAPTION = f"{HP}tbl", f"{HP}tr", f"{HP}tc", f"{HP}caption"
SIDES = ("LEFT", "RIGHT", "TOP", "BOTTOM")  # 캡션 속성 bit 0–1(짝 표 캡션 3개 모두 2 → TOP)
SCRIPT, TEXTART_TAG = f"{HP}script", f"{HP}textart"
PAGES = ("BOTH", "EVEN", "ODD", "BOTH")  # 머리말·꼬리말 속성 bit 0–1(3은 정의가 없다)
LISTED = {"head": f"{HP}header", "foot": f"{HP}footer", "fn  ": f"{HP}footNote", "en  ": f"{HP}endNote",
          "tcmt": f"{HP}hiddenComment"}  # 리스트 하나를 hp:ctrl 아래 요소로 옮기는 컨트롤
SHAPES = {"$rec": "rect", "$ell": "ellipse", "$arc": "arc", "$pol": "polygon", "$cur": "curve", "$lin": "line",
          "$col": "connectLine"}  # SHAPE_COMPONENT 종류 ID → 그리기 요소(글상자 리스트가 있을 수 있다)
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
IMPLAUSIBLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]")  # 글맵시 글자로 읽은 것이 틀렸다는 표지(U+FFFD·제어 문자, 탭·줄바꿈은 뺀다)


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


def hwp_string(body: bytes, at: int, what: str, location: str) -> str:
    """몸 at 자리의 WORD 길이 + WCHAR 글자(대리쌍 묶음, 짝 없는 대리 문자 → U+FFFD). 모자라면 ParseError."""
    if len(body) < at + 2:
        raise ParseError(f"{what} record too short", location)
    (n,) = struct.unpack_from("<H", body, at)
    if len(body) < at + 2 + 2 * n:
        raise ParseError(f"{what} record too short", location)
    return utf16(wchars(body[at + 2:at + 2 + 2 * n]))[0]


def record_text(record: Record, location: str) -> str | None:
    """EQEDIT(속성 UINT32 뒤 스크립트)·글맵시(꼭짓점 8 INT32 뒤 글자) 레코드의 글자. 옮겨 쓰기와 R(r)이 함께 쓴다(디코드는
    자리 하나뿐이다: 순서·소속은 장부가, 자리는 독립 샘플 게이트가 본다). 글맵시 배치는 실파일로 확인하지 못했다: 길이가
    몸을 넘거나 글자에 U+FFFD·제어 문자(탭·줄바꿈 \t \n \r 말고 < 0x20)가 섞이면 ParseError 대신 None(읽지 못한 레코드)."""
    if record.tag == EQEDIT:
        return hwp_string(record.body, 4, "EQEDIT", f"{location}#{record.index}")
    body = record.body
    if len(body) < 34 or len(body) < 34 + 2 * struct.unpack_from("<H", body, 32)[0]:
        return None
    text = hwp_string(body, 32, "SHAPE_COMPONENT_TEXTART", f"{location}#{record.index}")
    return None if IMPLAUSIBLE.search(text) else text


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
    """옮겨 쓴 구역: 트리, 처리 이력(처리부 앞, 장부 처리 이력 포함), 장부 숫자, 바탕쪽 글자 수."""

    root: ET.Element
    regions: list[Region] = field(default_factory=list)
    audit: Audit = Audit(0, 0, 0)
    masters: list[int] = field(default_factory=list)  # 바탕쪽 리스트마다 공백 아닌 글자 수(구역 안 차례)


class _Writer:
    def __init__(self, index: int, base: int, location: str, nodes: Nodes) -> None:
        self.index = index
        self.base = base
        self.location = location
        self.nodes = nodes
        self.marks: dict[int, int] = {}  # id(요소) → 글자 레코드 순번(hp:t, hp:script, hp:textart. 트리가 붙잡고 있다)
        self.skipped: set[int] = set()  # 기록과 함께 건너뛴 글자 레코드
        self.regions: list[Region] = []
        self.unknown: set[int] = set()  # unknown_control을 남긴 문단(id)
        self.dropped = False  # 지금 컨트롤의 묶음 안에서 모르는 도형이 빠졌나
        self.used: set[int] = set()  # 리스트로 옮긴 PARA_HEADER 순번(어느 리스트에도 들지 않은 문단을 찾는다)
        self.masters: list[int] = []  # 바탕쪽 리스트마다 공백 아닌 글자 수

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
        bad = len(body) % 2  # 홀수 길이 몸의 남는 바이트 하나도 짝 없는 대리 문자처럼 센다
        unknown = ext = mismatch = 0
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
        if cursor < len(order):  # 글자 뒤 경계는 빈 런 하나로 접는다(처리부는 글자 없는 런의 글자 모양을 쓰지 않는다)
            run = self.node(p, RUN, depth + 1, charPrIDRef=chosen[order[cursor]])
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
        """CTRL_HEADER 하나(depth는 런 아래 요소의 깊이). 자리 컨트롤은 빈 hp:ctrl, 덧말·글자 겹침은 글자 수만, 아는
        개체는 그 요소, 몸 레코드가 없거나 모르는 ID는 hp:unknownObject 아래로 그 아래 문단들(레코드 차례)을 옮긴다.
        아는 개체에서 어느 리스트에도 들지 않은 문단은 그 뒤 hp:unknownObject로 옮긴다."""
        ident = control_id(record.body)
        paras = self.paragraphs(record)
        lists = any(r.tag == LIST_HEADER for r in self.descendants(record))
        simple = ident in PLACES or ident.startswith("%")
        if simple and not paras and not lists:
            self.node(run, CTRL, depth)
            if ident in TEXT_CONTROLS:
                chars = ink(control_text(record.body, ident, f"{self.location}#{record.index}"))
                self.skipped.add(record.index)
                if chars:
                    self.region("control-text", CONTROL_TEXT, anchor, "chars", chars)
            return
        builders = {"tbl ": self.table, "gso ": self.gso, "eqed": self.equation, "secd": self.masterpages,
                    **dict.fromkeys(LISTED, self.listed)}
        build = builders.get(ident)
        outer, self.dropped = self.dropped, False  # 묶음에서 빠진 도형 표시는 컨트롤마다 따로 둔다(안쪽이 바깥으로 새지 않게)
        try:
            built = build is not None and build(run, record, anchor, depth)
            dropped = self.dropped
        finally:
            self.dropped = outer
        if ident == "gso ":
            later = [r for r in record.children if r.tag == SHAPE_COMPONENT][1:]
            if later:  # 둘째 이후 SHAPE_COMPONENT는 옮기지 않는다(글자는 아래 남는 문단 길). 첫째를 몰라도 같다
                self.flag_unknown(p, anchor)
                for r in later:
                    self.skipped.update(x.index for x in self.descendants(r) if x.tag == TEXTART)  # 기록과 함께 건너뜀
        if not built:
            self.unknown_object(run, paras, p, anchor, depth)
            return
        if dropped:  # 묶음 안에서 모르는 도형이 빠졌다(만든 요소 없음): 문단마다 한 번 기록
            self.flag_unknown(p, anchor)
        rest = [header for header in paras if header.index not in self.used]
        if rest:
            self.unknown_object(run, rest, p, anchor, depth)

    def listed(self, run: ET.Element, record: Record, anchor: int, depth: int) -> bool:
        """머리말·꼬리말·각주·미주·숨은 설명: hp:ctrl/요소/hp:subList(첫 리스트). 리스트가 없으면 거짓."""
        ident = control_id(record.body)
        found = self.lists(record.children, anchor)
        if not found:
            return False
        attrs = {}
        if ident in ("head", "foot"):
            if len(record.body) < 8:
                raise ParseError(f"CTRL_HEADER {ident} record too short", f"{self.location}#{record.index}")
            attrs["applyPageType"] = PAGES[struct.unpack_from("<I", record.body, 4)[0] & 3]
        element = self.node(self.node(run, CTRL, depth), LISTED[ident], depth + 1, **attrs)
        self.sublist(element, found[0][1], anchor, depth + 2)
        return True

    def masterpages(self, run: ET.Element, record: Record, anchor: int, depth: int) -> bool:
        """구역 정의 아래 리스트(바탕쪽, HWPX의 masterpage 멤버): 옮기지 않는다. 그 문단 아래 글자 레코드는 모두 건너뛴
        것으로 적고, 리스트마다 PARA_TEXT의 공백 아닌 글자 수를 센다. 리스트가 없으면 거짓(빈 자리 컨트롤)."""
        found = self.lists(record.children, anchor)
        if not found:
            return False
        self.node(run, CTRL, depth)
        for _, paras in found:
            chars = 0
            stack = list(paras)
            while stack:
                r = stack.pop()
                stack.extend(r.children)
                if r.tag == PARA_HEADER:
                    self.used.add(r.index)
                elif r.tag in (PARA_TEXT, EQEDIT, TEXTART) or (
                        r.tag == CTRL_HEADER and control_id(r.body) in TEXT_CONTROLS):
                    self.skipped.add(r.index)
                    if r.tag == PARA_TEXT:
                        chars += ink(canonical(r, self.location))
            self.masters.append(chars)
        return True

    def unknown_object(self, run: ET.Element, paras: list[Record], p: ET.Element, anchor: int, depth: int) -> None:
        """hp:ctrl/hp:unknownObject/hp:subList 아래 문단들(처리부가 모르는 요소: 장부가 글자를 살린다)."""
        self.flag_unknown(p, anchor)
        ctrl = self.node(run, CTRL, depth)
        self.sublist(self.node(ctrl, f"{HP}unknownObject", depth + 1), paras, anchor, depth + 2)

    def flag_unknown(self, p: ET.Element, anchor: int) -> None:
        """unknown_control(문단 p마다 한 번)."""
        if id(p) not in self.unknown:
            self.unknown.add(id(p))
            self.region("unknown-control", UNKNOWN_CONTROL, anchor)

    def sublist(self, parent: ET.Element, paras: list[Record], anchor: int, depth: int) -> ET.Element:
        """hp:subList(depth) 아래 문단들."""
        sub = self.node(parent, SUBLIST, depth)
        for header in paras:
            self.used.add(header.index)
            self.paragraph(sub, header, anchor, depth + 1)
        return sub

    def lists(self, siblings: list[Record], anchor: int) -> list[tuple[Record, list[Record]]]:
        """형제 레코드들의 리스트: LIST_HEADER마다 그 뒤 이어지는 같은 레벨 PARA_HEADER들. 선언한 문단 수(INT16)와
        다르면 실제 수를 쓰고 record_inconsistent(list_paragraphs = 차이)."""
        out: list[tuple[Record, list[Record]]] = []
        open_ = False  # 마지막 리스트에 문단이 이어 붙을 수 있나(문단 아닌 형제가 끊는다)
        for r in siblings:
            if r.tag == LIST_HEADER:
                out.append((r, []))
                open_ = True
            elif r.tag == PARA_HEADER and open_:
                out[-1][1].append(r)
            else:
                open_ = False
        for header, paras in out:
            declared = struct.unpack_from("<h", header.body)[0] if len(header.body) >= 2 else 0
            if declared != len(paras):
                self.region("record-inconsistent", RECORD_INCONSISTENT, anchor, "list_paragraphs",
                            abs(declared - len(paras)))
        return out

    def caption(self, parent: ET.Element, item: tuple[Record, list[Record]], anchor: int, depth: int) -> None:
        """캡션 리스트 → hp:caption@side(depth) + hp:subList. 캡션 속성은 LIST_HEADER 오프셋 8 UINT32."""
        header, paras = item
        if len(header.body) < 12:
            raise ParseError("LIST_HEADER caption record too short", f"{self.location}#{header.index}")
        side = SIDES[struct.unpack_from("<I", header.body, 8)[0] & 3]
        self.sublist(self.node(parent, CAPTION, depth, side=side), paras, anchor, depth + 1)

    def gso(self, run: ET.Element, record: Record, anchor: int, depth: int) -> bool:
        """그리기 개체: 첫 SHAPE_COMPONENT(ID가 두 번 적힌다) → 개체 요소, 그 앞 리스트는 캡션. 없거나 모르는 종류면 거짓."""
        kids = record.children
        k = next((i for i, r in enumerate(kids) if r.tag == SHAPE_COMPONENT), None)
        if k is None:
            return False
        element = self.shape(run, kids[k], anchor, depth, 8)
        if element is None:
            return False
        captions = self.lists(kids[:k], anchor)
        if captions:
            self.caption(element, captions[0], anchor, depth + 1)
        return True

    def shape(self, parent: ET.Element, record: Record, anchor: int, depth: int, base: int) -> ET.Element | None:
        """SHAPE_COMPONENT 하나 → 개체 요소(depth). base는 종류 ID 뒤 필드의 시작(개체 바로 아래 8, 묶음 안 4).
        모르는 종류면 None(만든 요소 없음: 그 아래 문단은 부르는 쪽이 모르는 컨트롤 길로 옮긴다)."""
        ident = control_id(record.body)
        kids = record.children
        tags = {r.tag for r in kids}
        location = f"{self.location}#{record.index}"
        if VIDEO_DATA in tags or CHART_DATA in tags:
            return self.node(parent, f"{HP}video" if VIDEO_DATA in tags else f"{HP}chart", depth)
        if ident == "$pic":
            if len(record.body) < base + 28:
                raise ParseError("SHAPE_COMPONENT record too short", location)
            first = struct.unpack_from("<II", record.body, base + 12)  # 처음 폭·높이
            now = struct.unpack_from("<II", record.body, base + 20)  # 현재 폭·높이
            pic = self.node(parent, f"{HP}pic", depth)
            # 현재 폭·높이가 처음 것과 같으면 HWPX는 그 값을 0으로 적는다(폭·높이 따로, 짝 그림 196개 모두)
            cur = [0 if a == b else a for a, b in zip(now, first)]
            self.node(pic, f"{HP}orgSz", depth + 1, width=str(first[0]), height=str(first[1]))  # 처리부 dpi가 쓴다
            self.node(pic, f"{HP}curSz", depth + 1, width=str(cur[0]), height=str(cur[1]))
            image = next((r for r in kids if r.tag == PICTURE), None)
            if image is not None:
                if len(image.body) < 73:
                    raise ParseError("SHAPE_COMPONENT_PICTURE record too short", f"{self.location}#{image.index}")
                self.node(pic, f"{HC}img", depth + 1, binaryItemIDRef=str(struct.unpack_from("<H", image.body, 71)[0]))
            return pic
        if ident in SHAPES:
            element = self.node(parent, f"{HP}{SHAPES[ident]}", depth)
            texts = self.lists(kids, anchor)
            if texts:
                self.sublist(self.node(element, f"{HP}drawText", depth + 1), texts[0][1], anchor, depth + 2)
            return element
        if ident == "$con":
            element = self.node(parent, f"{HP}container", depth)
            for child in kids:
                if child.tag == SHAPE_COMPONENT:
                    self.dropped |= self.shape(element, child, anchor, depth + 1, 4) is None
            return element
        if ident == "$ole":
            return self.node(parent, f"{HP}ole", depth)
        if ident == "$tat":
            art = next((r for r in kids if r.tag == TEXTART), None)
            if art is None:
                return None
            text = record_text(art, self.location)
            if text is None:  # 읽지 못한 글맵시: 개체 없이 모르는 컨트롤 길, 레코드는 기록과 함께 건너뜀
                self.skipped.add(art.index)
                self.region("record-inconsistent", RECORD_INCONSISTENT, anchor, "textart", 1)
                return None
            element = self.node(parent, TEXTART_TAG, depth, text=text)
            self.marks[id(element)] = art.index
            return element
        return None

    def equation(self, run: ET.Element, record: Record, anchor: int, depth: int) -> bool:
        """수식: hp:equation/hp:script(EQEDIT 스크립트), 그 앞 리스트는 캡션. EQEDIT가 없으면 거짓."""
        kids = record.children
        k = next((i for i, r in enumerate(kids) if r.tag == EQEDIT), None)
        if k is None:
            return False
        element = self.node(run, f"{HP}equation", depth)
        captions = self.lists(kids[:k], anchor)
        if captions:
            self.caption(element, captions[0], anchor, depth + 1)
        script = self.node(element, SCRIPT, depth + 1)
        script.text = record_text(kids[k], self.location)
        self.marks[id(script)] = kids[k].index
        return True

    def table(self, run: ET.Element, record: Record, anchor: int, depth: int) -> bool:
        """표: TABLE 앞 리스트는 캡션(첫째), 뒤 리스트는 칸(레코드 차례, 행 주소가 바뀔 때 새 hp:tr), 칸보다 짧은 뒤
        리스트는 남는 문단 길. TABLE이 없으면 거짓(모르는 컨트롤 길)."""
        kids = record.children
        k = next((i for i, r in enumerate(kids) if r.tag == TABLE), None)
        if k is None:
            return False
        body = kids[k].body
        if len(body) < 8:
            raise ParseError("TABLE record too short", f"{self.location}#{kids[k].index}")
        rows, cols = struct.unpack_from("<HH", body, 4)
        tbl = self.node(run, TBL, depth, rowCnt=str(rows), colCnt=str(cols))
        captions = self.lists(kids[:k], anchor)
        if captions:
            self.caption(tbl, captions[0], anchor, depth + 1)
        tr: ET.Element | None = None
        row = -1
        for header, paras in self.lists(kids[k + 1:], anchor):
            cell = header.body
            if len(cell) < 34:  # 칸보다 짧은 리스트(캡션 모양)는 칸이 아니라 남는 문단 길로(문단은 used에 들지 않는다)
                self.region("record-inconsistent", RECORD_INCONSISTENT, anchor, "list_shape", 1)
                continue
            col, at, colspan, rowspan = struct.unpack_from("<HHHH", cell, 8)
            if tr is None or at != row:
                tr, row = self.node(tbl, TR, depth + 1), at
            tc = self.node(tr, TC, depth + 2, header=str(struct.unpack_from("<H", cell, 6)[0] >> 2 & 1),
                           borderFillIDRef=str(struct.unpack_from("<H", cell, 32)[0]))
            self.sublist(tc, paras, anchor, depth + 3)
            self.node(tc, f"{HP}cellAddr", depth + 3, rowAddr=str(at), colAddr=str(col))
            self.node(tc, f"{HP}cellSpan", depth + 3, rowSpan=str(rowspan), colSpan=str(colspan))
        return True

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


def canonical(record: Record, location: str) -> str | None:
    """R(r): 옮겨 쓰기와 따로 디코드한 정규 문자열. PARA_TEXT는 보통 글자(대리쌍 묶음, 짝 없는 대리 문자 → U+FFFD)와
    표지 글자(탭 \\t, 줄바꿈 \\n, 묶음 빈칸 \\x1e, 고정폭 빈칸 \\x1f, 하이픈 -), 그 밖 제어 문자·컨트롤은 뺀다.
    runs와 다른 길: 제어 문자 자리를 WCHAR마다 찾고 그 사이 바이트를 errors="replace"로 디코드한다(짝 없는 대리
    WCHAR 하나 → U+FFFD 하나). location은 스트림 위치(덧말·글자 겹침 몸이 짧으면 ParseError에 #순번을 붙인다). 읽지
    못한 글맵시는 None(건너뛴 기록이 없으면 장부가 고아로 센다)."""
    if record.tag == CTRL_HEADER:
        return control_text(record.body, control_id(record.body), f"{location}#{record.index}")
    if record.tag in (EQEDIT, TEXTART):
        return record_text(record, location)
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
    """T(r): r로 표시된 요소들을 문서 순서로 훑어 처리부가 읽는 자리만 이은 것. hp:t는 text, 자식마다 표지 글자 또는 그
    안 글자, 자식 tail. hp:script는 text, hp:textart는 text 속성."""
    out: dict[int, list[str]] = {}
    for element in root.iter():
        k = marks.get(id(element))
        if k is None:
            continue
        if element.tag != T:
            out.setdefault(k, []).append(element.get("text", "") if element.tag == TEXTART_TAG else element.text or "")
            continue
        parts = [element.text or ""]
        for child in element:
            parts.append(MARKS[child.tag] if child.tag in MARKS else "".join(child.itertext()))
            parts.append(child.tail or "")
        out.setdefault(k, []).append("".join(parts))
    return {k: "".join(v) for k, v in out.items()}


def stray(root: ET.Element) -> int:
    """처리부가 읽지 않는 자리(hp:t 자신의 tail, hp:t 자식의 text, hp:script의 tail, 그 밖 요소의 text·tail)의 공백
    아닌 글자 수."""
    count = 0
    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    while stack:
        element, in_t = stack.pop()
        if element.tag == T and not in_t:
            count += ink(element.tail or "")
        elif in_t:
            count += ink(element.text or "")
        elif element.tag == SCRIPT:
            count += ink(element.tail or "")
        else:
            count += ink(element.text or "") + ink(element.tail or "")
        stack.extend((child, element.tag == T and not in_t) for child in element)
    return count


def audit(records: list[Record], root: ET.Element, marks: dict[int, int], skipped: set[int], location: str) -> Audit:
    """모든 글자 레코드(PARA_TEXT, 덧말·글자 겹침 CTRL_HEADER, EQEDIT, 글맵시)를 레벨·부모와 상관없이 훑어 판정한다.
    location은 구역 스트림 위치("{파일}:{스트림}", 오류에 #순번을 붙인다)."""
    found = tree_texts(root, marks)
    mismatched = orphans = 0
    stack = list(reversed(records))
    while stack:
        record = stack.pop()
        stack.extend(reversed(record.children))
        if record.tag not in (PARA_TEXT, EQEDIT, TEXTART) and not (
                record.tag == CTRL_HEADER and control_id(record.body) in TEXT_CONTROLS):
            continue
        text = found.get(record.index)
        if record.index in skipped:
            mismatched += text is not None
            continue
        expected = canonical(record, location)
        if expected is not None and (text == expected or (text is None and expected == "")):
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
    result = Section(root, writer.regions, masters=writer.masters)
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
