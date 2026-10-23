"""HWP 5.0 합성 문서 쓰기 도구와 쌍둥이 골든 예제 생성기(지어낸 한국어 글자만, HWP 스펙 §11.1).

    uv run python packages/engine/tests/build_hwp_fixtures.py          # fixtures/hwp/inputs/ 재생성
    uv run python packages/engine/tests/build_hwp_fixtures.py --check  # 입력이 최신이고 쌍둥이가 맞는지 확인

자체 OLE 복합 파일(MS-CFB) 쓰기 도구: 512B 섹터(major 3) 또는 4096B(major 4), FAT·mini FAT·디렉터리, 고정 차례(경로
이름 차례, 형제는 CFB 비교 규칙 차례로 right에 잇는다), 시각 0. 레코드 쓰기 도구: 레코드 머리(0xFFF 확장 크기),
FileHeader, raw deflate, DocInfo(build_hwpx_fixtures.header()와 같은 인자), 문단(PARA_HEADER·PARA_TEXT·
PARA_CHAR_SHAPE·PARA_LINE_SEG)과 컨트롤, 문서 하나(document). 테스트는 이 파일을 importlib로 읽어 쓰기 도구를 쓰고,
tamper로 직렬화 직전 배치를 고쳐 잘못된 파일을 만든다.

쌍둥이 골든: HWP 입력은 build_hwpx_fixtures.py의 같은 예제(구역 XML·header.xml·그림 manifest)를 레코드로 옮겨 만들고,
기대값은 HWPX 골든(fixtures/hwpx/expected)을 그대로 쓴다. 비교는 형식 정체성(source.name·mime·content_hash, document_id)만
가리고 block_id는 블록 순번으로 바꾼다(참조도 같이). 그림은 HWPX 골든처럼 자산 키를 가리고 화소 정보를 따로 견준다. HWP에
같은 것이 없는 HWPX 처리 이력은 DROPPED에 적은 것만 뺀다.
"""

import argparse
import hashlib
import importlib.util
import json
import struct
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hanji.core import build_tree
from hanji.formats.hwp import HwpParser
from hanji.formats.hwp.cfb import Compound
from hanji_contracts import DocumentTree, ProcessingHistory, SourceInfo

SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
DIFSECT, FATSECT, ENDOFCHAIN, FREESECT = 0xFFFFFFFC, 0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF
MINI = 64
CUTOFF = 4096


@dataclass
class Layout:
    """직렬화 직전 배치. header는 머리 필드 이름 → 값(difat는 109개 목록), fat·minifat은 정수 목록, entries는 디렉터리
    항목(name·kind·left·right·child·start·size, 이름 길이를 바꾸려면 length), sectors는 파일의 섹터마다 바이트(번호 =
    목록 순번), places는 배치 이름(fat·dir·minifat·mini·큰 스트림 경로) → (첫 섹터, 섹터 수). 직렬화가 fat·entries·
    minifat을 제자리 섹터에 다시 쓴다(tamper가 고친 값이 그대로 들어간다)."""

    sector: int
    header: dict[str, object] = field(default_factory=dict)
    fat: list[int] = field(default_factory=list)
    minifat: list[int] = field(default_factory=list)
    entries: list[dict[str, object]] = field(default_factory=list)
    sectors: list[bytearray] = field(default_factory=list)
    places: dict[str, tuple[int, int]] = field(default_factory=dict)


def _cfb_key(name: str) -> tuple[int, str]:
    return len(name), name.upper()


def _tree(paths: list[str]) -> list[dict[str, object]]:
    """경로들 → 디렉터리 항목(0은 뿌리). 저장소는 경로 앞부분에서 나온다. 형제는 CFB 비교 차례로 right에 잇는다."""
    nodes: dict[str, dict] = {"": {"name": "Root Entry", "kind": 5, "children": []}}
    order = [""]
    for path in sorted(paths):
        parts = path.split("/")
        for k in range(1, len(parts) + 1):
            here = "/".join(parts[:k])
            if here not in nodes:
                nodes[here] = {"name": parts[k - 1], "kind": 2 if k == len(parts) else 1, "children": []}
                nodes["/".join(parts[:k - 1])]["children"].append(here)
                order.append(here)
    index = {p: k for k, p in enumerate(order)}
    entries: list[dict[str, object]] = [
        {"path": p, "name": nodes[p]["name"], "kind": nodes[p]["kind"], "left": NOSTREAM, "right": NOSTREAM,
         "child": NOSTREAM, "start": ENDOFCHAIN, "size": 0} for p in order]
    for p in order:
        kids = sorted(nodes[p]["children"], key=lambda c: _cfb_key(nodes[c]["name"]))
        if kids:
            entries[index[p]]["child"] = index[kids[0]]
            for a, b in zip(kids, kids[1:]):
                entries[index[a]]["right"] = index[b]
    return entries


def _entry(entry: Mapping[str, object]) -> bytes:
    name = str(entry["name"]).encode("utf-16-le") + b"\0\0"
    length = int(entry.get("length", len(name)))  # type: ignore[call-overload]
    return (name.ljust(64, b"\0")[:64] + struct.pack("<HBB", length, entry["kind"], 1)
            + struct.pack("<III", entry["left"], entry["right"], entry["child"]) + bytes(36)
            + struct.pack("<IQ", entry["start"], entry["size"]))


def compound(streams: Mapping[str, bytes], major: int = 3, tamper: Callable[[Layout], None] | None = None) -> bytes:
    """스트림 경로 → 바이트로 복합 파일을 만든다. 섹터 차례: FAT, 디렉터리, mini FAT, mini 스트림, 큰 스트림(경로
    차례). 4096B 미만 스트림은 mini 스트림에(64B 단위). FAT 섹터는 109개까지(DIFAT 섹터 없음)."""
    sector = 512 if major == 3 else 4096
    words = sector // 4
    entries = _tree(list(streams))
    by_path = {str(e["path"]): e for e in entries}
    mini_data = bytearray()
    minifat: list[int] = []
    for p in sorted(streams):
        data = streams[p]
        by_path[p]["size"] = len(data)
        if not data or len(data) >= CUTOFF:
            continue
        first, count = len(mini_data) // MINI, (len(data) + MINI - 1) // MINI
        by_path[p]["start"] = first
        minifat += [first + k + 1 for k in range(count - 1)] + [ENDOFCHAIN]
        mini_data += data.ljust(count * MINI, b"\0")
    pieces: list[tuple[str, bytes]] = [("dir", b"".join(_entry(e) for e in entries))]
    if minifat:
        pieces.append(("minifat", struct.pack(f"<{len(minifat)}I", *minifat)))
    if mini_data:
        pieces.append(("mini", bytes(mini_data)))
    pieces += [(p, streams[p]) for p in sorted(streams) if len(streams[p]) >= CUTOFF]
    used = sum((len(data) + sector - 1) // sector for _, data in pieces)
    nfat = 1
    while nfat * words < nfat + used:
        nfat += 1
    layout = Layout(sector=sector, fat=[FATSECT] * nfat, minifat=minifat, entries=entries,
                    sectors=[bytearray(sector) for _ in range(nfat)], places={"fat": (0, nfat)})
    for name, data in pieces:
        count = (len(data) + sector - 1) // sector
        first = len(layout.fat)
        layout.places[name] = (first, count)
        layout.fat += [first + k + 1 for k in range(count - 1)] + [ENDOFCHAIN]
        layout.sectors += [bytearray(data[k * sector:(k + 1) * sector].ljust(sector, b"\0")) for k in range(count)]
    for p in streams:
        if p in layout.places:
            by_path[p]["start"] = layout.places[p][0]
    if mini_data:
        entries[0]["start"], entries[0]["size"] = layout.places["mini"][0], len(mini_data)
    layout.header = {"major": major, "order": 0xFFFE, "shift": 9 if major == 3 else 12, "mini_shift": 6,
                     "ndir": 0 if major == 3 else layout.places["dir"][1], "nfat": nfat, "dir": layout.places["dir"][0],
                     "cutoff": CUTOFF, "minifat": layout.places.get("minifat", (ENDOFCHAIN, 0))[0],
                     "nminifat": layout.places.get("minifat", (0, 0))[1], "difat_first": ENDOFCHAIN, "ndifat": 0,
                     "difat": list(range(nfat)) + [FREESECT] * (109 - nfat)}
    if tamper is not None:
        tamper(layout)
    return serialize(layout)


def _put(layout: Layout, name: str, raw: bytes, pad: bytes) -> None:
    """배치 이름 자리 섹터들에 raw를 다시 쓴다(남는 자리는 pad)."""
    first, count = layout.places[name]
    raw = raw.ljust(count * layout.sector, pad)
    for k in range(count):
        layout.sectors[first + k] = bytearray(raw[k * layout.sector:(k + 1) * layout.sector])


def serialize(layout: Layout) -> bytes:
    h = layout.header
    _put(layout, "fat", struct.pack(f"<{len(layout.fat)}I", *layout.fat), b"\xff")
    _put(layout, "dir", b"".join(_entry(e) for e in layout.entries), b"\0")
    if "minifat" in layout.places:
        _put(layout, "minifat", struct.pack(f"<{len(layout.minifat)}I", *layout.minifat), b"\xff")
    head = (SIGNATURE + bytes(16) + struct.pack("<HHHHH", 0x3E, h["major"], h["order"], h["shift"], h["mini_shift"])
            + bytes(6) + struct.pack("<IIII", h["ndir"], h["nfat"], h["dir"], 0)
            + struct.pack("<IIIII", h["cutoff"], h["minifat"], h["nminifat"], h["difat_first"], h["ndifat"])
            + struct.pack("<109I", *h["difat"]))  # type: ignore[misc]
    return head.ljust(layout.sector, b"\0") + b"".join(bytes(s) for s in layout.sectors)


# ---- 레코드 쓰기 도구 ----

def record(tag: int, level: int, body: bytes = b"") -> bytes:
    """레코드 머리(TagID 10비트·Level 10비트·Size 12비트, 4095B 이상은 Size 0xFFF + DWORD) + 몸."""
    if len(body) >= 0xFFF:
        return struct.pack("<II", tag | level << 10 | 0xFFF << 20, len(body)) + body
    return struct.pack("<I", tag | level << 10 | len(body) << 20) + body


def file_header(version: tuple[int, int, int, int] = (5, 1, 0, 1), flags: int = 0) -> bytes:
    """FileHeader 256B: 서명(NUL 채움), 버전 0xMMnnPPrr, 속성, 속성 2, EncryptVersion 4."""
    value = version[0] << 24 | version[1] << 16 | version[2] << 8 | version[3]
    return (b"HWP Document File".ljust(32, b"\0") + struct.pack("<IIII", value, flags, 0, 4)).ljust(256, b"\0")


def deflate(data: bytes) -> bytes:
    """머리 없는 raw deflate(한컴 문서의 압축 스트림과 같은 꼴, wbits −15)."""
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    return packer.compress(data) + packer.flush()


# ---- DocInfo 쓰기 도구 ----
DOCUMENT_PROPERTIES, ID_MAPPINGS, BIN_DATA, BORDER_FILL, CHAR_SHAPE, PARA_SHAPE = 0x10, 0x11, 0x12, 0x14, 0x15, 0x19
HEADS = ("NONE", "OUTLINE", "NUMBER", "BULLET")
LINK, EMBEDDING, STORAGE = 0, 1, 2


def bin_data(sid: int = 1, ext: str = "png", kind: int = EMBEDDING, mode: int = 0) -> bytes:
    """BIN_DATA 몸: 속성(bit 0–3 종류, bit 4–5 압축 0 문서 따름·1 압축·2 비압축). 연결은 빈 절대·상대 경로, 포함은 저장
    id + 확장자, OLE 저장소는 저장 id."""
    attr = kind | mode << 4
    if kind == LINK:
        return struct.pack("<HHH", attr, 0, 0)
    if kind == EMBEDDING:
        return struct.pack("<HHH", attr, sid, len(ext)) + ext.encode("utf-16-le")
    return struct.pack("<HH", attr, sid)


def colorref(color: str) -> int:
    """"#RRGGBB" → COLORREF 0x00BBGGRR, "none" → 0xFFFFFFFF(HWPX faceColor="none"과 같은 값)."""
    if color == "none":
        return 0xFFFFFFFF
    r, g, b = (int(color[k:k + 2], 16) for k in (1, 3, 5))
    return r | g << 8 | b << 16


def para_shape(kind: str = "NONE", level: int = 0) -> bytes:
    """PARA_SHAPE 54B: 속성 1의 bit 23–24 문단 머리 종류, bit 25–27 수준. 나머지는 0."""
    return struct.pack("<I", HEADS.index(kind) << 23 | level << 25) + bytes(50)


def char_shape(height: int = 1000, bold: bool = False, rel: int = 100) -> bytes:
    """CHAR_SHAPE 74B: 언어별 글꼴 id·장평·자간·상대 크기(rel)·위치, 기준 크기, 속성(bit 1 굵게), 나머지 0."""
    return (bytes(14) + bytes([100] * 7) + bytes(7) + bytes([rel] * 7) + bytes(7)
            + struct.pack("<iI", height, 2 if bold else 0) + bytes(24))


def border_fill(color: str = "none", kind: int = 1) -> bytes:
    """BORDER_FILL: 테두리 32B(0) + 채우기 종류 + (단색이면 배경색·무늬색·무늬 종류) + 추가 채우기 길이 0."""
    fill = struct.pack("<I", kind) + (struct.pack("<IIi", colorref(color), 0, -1) if kind & 1 else b"")
    return bytes(32) + fill + struct.pack("<I", 0)


def docinfo(headings: Mapping[int, tuple[str, int]] = (), chars: Mapping[int, tuple[int, bool]] = (),
            fills: Mapping[int, str] = (), sections: int = 1, extra: Sequence[bytes] = (),
            bins: Sequence[bytes] = ()) -> bytes:
    """DocInfo 레코드(풀린 바이트). 인자는 build_hwpx_fixtures.header()와 같다(문단 모양 0 NONE·글자 모양 0 10pt·
    테두리 채움 1 none은 늘 있다). HWP id는 순번이라 id가 0(테두리 채움은 1)부터 이어져야 한다. extra는 끝에 붙일
    레코드(레벨 1). bins는 BIN_DATA 몸들(bin_data, ID_MAPPINGS 바로 뒤 레벨 1, 순번 1부터)."""
    headings = {0: ("NONE", 0), **dict(headings)}
    chars = {0: (1000, False), **dict(chars)}
    fills = {1: "none", **dict(fills)}
    assert list(headings) == list(range(len(headings))) and list(chars) == list(range(len(chars)))
    assert list(fills) == list(range(1, len(fills) + 1))
    counts = [0] * 18
    counts[0], counts[8], counts[9], counts[13] = len(bins), len(fills), len(chars), len(headings)
    out = [record(DOCUMENT_PROPERTIES, 0, struct.pack("<H", sections) + bytes(24)),
           record(ID_MAPPINGS, 0, struct.pack("<18i", *counts))]
    out += [record(BIN_DATA, 1, body) for body in bins]
    out += [record(BORDER_FILL, 1, border_fill(color)) for color in fills.values()]
    out += [record(CHAR_SHAPE, 1, char_shape(height, bold)) for height, bold in chars.values()]
    out += [record(PARA_SHAPE, 1, para_shape(kind, level)) for kind, level in headings.values()]
    return b"".join(out) + b"".join(extra)


# ---- 문단 쓰기 도구 ----
PARA_HEADER, PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG, CTRL_HEADER, LIST_HEADER = 0x42, 0x43, 0x44, 0x45, 0x47, 0x48


def inline(code: int) -> list[int]:
    """인라인 컨트롤 8 WCHAR(코드, 부가 정보 6, 코드)."""
    return [code, 0, 0, 0, 0, 0, 0, code]


def extended(code: int, ident: str) -> list[int]:
    """확장 컨트롤 8 WCHAR: 코드, 컨트롤 ID(UINT32 LE, 네 글자는 큰 엔디언), 0 넷, 코드."""
    value = int.from_bytes(ident.encode("latin-1"), "big")
    return [code, value & 0xFFFF, value >> 16, 0, 0, 0, 0, code]


TAB, TITLE_MARK, FIELD_END = inline(9), inline(8), inline(4)
LINE_BREAK, NB_SPACE, FW_SPACE, HYPHEN, PARA_END = [10], [30], [31], [24], [13]


def units(*items: str | list[int]) -> list[int]:
    """글자(UTF-16 단위로 편다)와 제어 문자 목록 → WCHAR들."""
    out: list[int] = []
    for item in items:
        if isinstance(item, str):
            raw = item.encode("utf-16-le", "surrogatepass")
            out += struct.unpack(f"<{len(raw) // 2}H", raw)
        else:
            out += item
    return out


def control(ident: str, level: int, *children: bytes, body: bytes = b"") -> bytes:
    """CTRL_HEADER(몸 = 컨트롤 ID UINT32 + body) + 이미 직렬화한 자식 레코드들(레벨 level + 1)."""
    return record(CTRL_HEADER, level, int.from_bytes(ident.encode("latin-1"), "big").to_bytes(4, "little") + body) + \
        b"".join(children)


def list_header(level: int, paragraphs: int) -> bytes:
    """LIST_HEADER: 문단 수(INT16) + 속성(UINT32). 문단들은 이 레코드 뒤 같은 레벨에 온다."""
    return record(LIST_HEADER, level, struct.pack("<hI", paragraphs, 0))


# ---- 개체 쓰기 도구(리스트·표) ----
TABLE = 0x4D
SIDES = ("LEFT", "RIGHT", "TOP", "BOTTOM")
OBJECT = bytes(42)  # 개체 공통 속성 자리(CTRL_HEADER 몸의 ID 뒤, 파서는 읽지 않는다)


def listed(level: int, *paras: bytes, tail: bytes = b"", ext: int = 0, count: int | None = None) -> bytes:
    """리스트: LIST_HEADER(level, 문단 수 INT16 + 속성 UINT32 + 확장 속성 UINT16 + tail) + 같은 레벨 문단들(paras는
    paragraph(level=level)로 직렬화한 문단 하나씩, count가 있으면 그 수를 선언한다)."""
    head = struct.pack("<hIH", len(paras) if count is None else count, 0, ext)
    return record(LIST_HEADER, level, head + tail) + b"".join(paras)


def caption(level: int, *paras: bytes, side: str = "BOTTOM") -> bytes:
    """캡션 리스트(30B LIST_HEADER: 오프셋 8 캡션 속성 bit 0–1 방향, 폭, 간격, 최대 길이, 0 8바이트)."""
    return listed(level, *paras, tail=struct.pack("<IIhI", SIDES.index(side), 0, 0, 0) + bytes(8))


def cell(level: int, row: int, col: int, *paras: bytes, span: tuple[int, int] = (1, 1), header: bool = False,
         fill: int = 1) -> bytes:
    """표 칸 리스트(47B LIST_HEADER: 오프셋 6의 bit 2 제목 칸, 8부터 열·행 주소, 열·행 병합(span = (행, 열)), 폭·높이,
    여백 넷, 32 테두리 채움 id, 0 13바이트)."""
    tail = struct.pack("<HHHHII", col, row, span[1], span[0], 0, 0) + bytes(8) + struct.pack("<H", fill) + bytes(13)
    return listed(level, *paras, tail=tail, ext=4 if header else 0)


def table(level: int, rows: int, cols: int, *cells: bytes, caption: bytes = b"") -> bytes:
    """표 컨트롤(level): [캡션 리스트] + TABLE(속성, 행·열 수, 칸 간격, 안쪽 여백, 행마다 칸 수, 테두리 채움, 영역 0) +
    칸 리스트들(레벨 level + 1로 만든 것)."""
    body = struct.pack("<IHHH", 0, rows, cols, 0) + bytes(8) + struct.pack(f"<{rows}H", *[cols] * rows)
    return control("tbl ", level, caption, record(TABLE, level + 1, body + struct.pack("<HH", 1, 0)), *cells,
                   body=OBJECT)


# ---- 개체 쓰기 도구(그리기 개체·수식) ----
SHAPE_COMPONENT, SC_LINE, SC_RECTANGLE, SC_OLE, SC_PICTURE = 0x4C, 0x4E, 0x4F, 0x54, 0x55
EQEDIT, SC_TEXTART, CHART_DATA = 0x58, 0x5A, 0x5F


def hwp_string(text: str) -> bytes:
    """WORD 길이 + WCHAR 글자."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return struct.pack("<H", len(raw) // 2) + raw


def component(level: int, ident: str, *children: bytes, top: bool = True, size: tuple[int, int] = (7200, 3600),
              first: tuple[int, int] = (0, 0)) -> bytes:
    """SHAPE_COMPONENT(level): 종류 ID(개체 바로 아래면 두 번, 묶음 안이면 한 번) + 묶음 안 X·Y, 묶인 횟수, 버전, 처음
    폭·높이(first), 현재 폭·높이(size), 속성·회전 자리 0 + 이미 직렬화한 자식(레벨 level + 1)."""
    ident4 = int.from_bytes(ident.encode("latin-1"), "big").to_bytes(4, "little")
    body = ident4 * (2 if top else 1) + struct.pack("<iiHHIIII", 0, 0, 0, 1, *first, *size) + bytes(14)
    return record(SHAPE_COMPONENT, level, body) + b"".join(children)


def gso(level: int, shape: bytes, caption: bytes = b"") -> bytes:
    """그리기 개체 컨트롤(level): [캡션 리스트] + SHAPE_COMPONENT(component(level + 1, …))."""
    return control("gso ", level, caption, shape, body=OBJECT)


def picture(level: int, ref: int) -> bytes:
    """SHAPE_COMPONENT_PICTURE 91B: 테두리·자르기·여백 68B 0, 밝기·명암·효과, BinData 순번(오프셋 71), 나머지 0."""
    return record(SC_PICTURE, level, bytes(71) + struct.pack("<H", ref) + bytes(18))


def pic(level: int, ref: int, width: int = 7200, height: int = 3600, top: bool = True,
        first: tuple[int, int] = (0, 0)) -> bytes:
    """그림 SHAPE_COMPONENT(level). 처음 크기는 기본 0이라 현재 크기가 늘 다르다(같은 쪽은 HWPX처럼 curSz 0)."""
    return component(level, "$pic", picture(level + 1, ref), top=top, size=(width, height), first=first)


def rect(level: int, *paras: bytes, top: bool = True) -> bytes:
    """글상자(사각형 SHAPE_COMPONENT(level)): 리스트(paras는 레벨 level + 1 문단) + SHAPE_COMPONENT_RECTANGLE."""
    box = listed(level + 1, *paras) if paras else b""
    return component(level, "$rec", box, record(SC_RECTANGLE, level + 1, bytes(33)), top=top)


def line(level: int, top: bool = True) -> bytes:
    return component(level, "$lin", record(SC_LINE, level + 1, bytes(20)), top=top)


def container(level: int, *shapes: bytes) -> bytes:
    """묶음 SHAPE_COMPONENT(level): 자식 SHAPE_COMPONENT들(레벨 level + 1, top=False로 만든 것)."""
    return component(level, "$con", *shapes)


def ole(level: int, chart: bool = False) -> bytes:
    """OLE SHAPE_COMPONENT(level)(chart면 CHART_DATA 자식도)."""
    data = record(CHART_DATA, level + 1, bytes(2)) if chart else b""
    return component(level, "$ole", record(SC_OLE, level + 1, bytes(24)), data)


def textart(level: int, text: str) -> bytes:
    """글맵시 SHAPE_COMPONENT(level): SHAPE_COMPONENT_TEXTART(꼭짓점 8 INT32 + 글자 + 글꼴 이름·모양 + 나머지 0)."""
    body = bytes(32) + hwp_string(text) + hwp_string("") + hwp_string("") + bytes(40)
    return component(level, "$tat", record(SC_TEXTART, level + 1, body))


def equation(level: int, script: str, caption: bytes = b"") -> bytes:
    """수식 컨트롤(level): [캡션 리스트] + EQEDIT(속성, 스크립트, 글자 크기, 색, 기준선, 버전·글꼴 이름)."""
    body = struct.pack("<I", 0) + hwp_string(script) + struct.pack("<IIh", 1000, 0, 0) + hwp_string("") * 2
    return control("eqed", level, caption, record(EQEDIT, level + 1, body), body=OBJECT)


# ---- 개체 쓰기 도구(머리말·꼬리말·주석·숨은 설명·바탕쪽) ----
PAGES = ("BOTH", "EVEN", "ODD")


def margin(level: int, kind: str, *paras: bytes, apply: str = "BOTH") -> bytes:
    """머리말(head)·꼬리말(foot) 컨트롤(level, 몸 12B: 속성 bit 0–1 적용 쪽) + 리스트(34B, paras는 레벨 level + 1)."""
    return control(kind, level, listed(level + 1, *paras, tail=bytes(26)), body=struct.pack("<I", PAGES.index(apply))
                   + bytes(4))


def note(level: int, kind: str, *paras: bytes) -> bytes:
    """각주(fn  )·미주(en  ) 컨트롤(level, 몸 20B) + 리스트(16B)."""
    return control(kind, level, listed(level + 1, *paras, tail=bytes(8)), body=bytes(16))


def hidden(level: int, *paras: bytes) -> bytes:
    """숨은 설명(tcmt) 컨트롤(level) + 리스트."""
    return control("tcmt", level, listed(level + 1, *paras, tail=bytes(8)))


def section_def(level: int, *masters: Sequence[bytes]) -> bytes:
    """구역 정의(secd) 컨트롤(level) + 바탕쪽 리스트들(masters: 리스트마다 레벨 level + 1 문단들, 34B LIST_HEADER)."""
    return control("secd", level, *(listed(level + 1, *paras, tail=bytes(26)) for paras in masters), body=bytes(34))


def paragraph(*items: str | list[int], level: int = 0, pr: int = 0, style: int = 0,
              shapes: Sequence[tuple[int, int]] = ((0, 0),), lines: int | None = 1, controls: Sequence[bytes] = (),
              nchars: int | None = None, text: Sequence[int] | None = None) -> bytes:
    """문단 하나: PARA_HEADER(level) + PARA_TEXT(items + 문단 끝 13, 글자가 없으면 레코드 없음, text가 있으면 그
    WCHAR 그대로) + PARA_CHAR_SHAPE(shapes = (시작 위치, 글자 모양 id)) + PARA_LINE_SEG(lines개, None이면 레코드 없음)
    + controls(레벨 level + 1로 직렬화한 컨트롤들, 확장 컨트롤 문자 차례대로)."""
    ws = list(text) if text is not None else (units(*items) + PARA_END if items else [])
    count = len(ws) if ws else 1
    head = struct.pack("<IIHBBHHHIH", count if nchars is None else nchars, 0, pr, style, 0, len(shapes), 0, lines or 0,
                       0, 0)
    out = [record(PARA_HEADER, level, head)]
    if ws:
        out.append(record(PARA_TEXT, level + 1, struct.pack(f"<{len(ws)}H", *ws)))
    out.append(record(PARA_CHAR_SHAPE, level + 1, b"".join(struct.pack("<II", pos, k) for pos, k in shapes)))
    if lines is not None:
        out.append(record(PARA_LINE_SEG, level + 1, bytes(36 * lines)))
    return b"".join(out) + b"".join(controls)


def document(*sections: bytes, head: bytes | None = None, compressed: bool = False, flags: int = 0,
             version: tuple[int, int, int, int] = (5, 1, 0, 1), extra: Mapping[str, bytes] = (),
             major: int = 3) -> bytes:
    """HWP 문서 바이트: FileHeader, DocInfo(head, 없으면 docinfo()), BodyText/Section{k}(풀린 레코드 바이트), extra
    스트림. compressed면 DocInfo·구역을 raw deflate로 싸고 FileHeader bit 0을 세운다."""
    pack = deflate if compressed else bytes
    streams = {"FileHeader": file_header(version, flags | int(compressed)),
               "DocInfo": pack(docinfo(sections=len(sections)) if head is None else head)}
    streams.update({f"BodyText/Section{k}": pack(data) for k, data in enumerate(sections)})
    streams.update(dict(extra))
    return compound(streams, major=major)


# ---- HWPX 예제 → HWP 레코드(쌍둥이 입력) ----
HERE = Path(__file__).resolve().parent


def _hwpx_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HX = _hwpx_builder()
OWPML = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HEAD_NS = "{http://www.hancom.co.kr/hwpml/2011/head}"
CORE_NS = "{http://www.hancom.co.kr/hwpml/2011/core}"
MARKS = {"tab": TAB, "lineBreak": LINE_BREAK, "nbSpace": NB_SPACE, "fwSpace": FW_SPACE, "titleMark": TITLE_MARK}


def from_section(xml: str) -> bytes:
    """HWPX 구역 XML → 구역 레코드(최상위 hp:p마다 레벨 0 문단)."""
    return b"".join(from_para(p, 0) for p in ET.fromstring(xml))


def from_para(p: ET.Element, level: int) -> bytes:
    """HWPX hp:p → 문단 레코드(level). 런마다 글자 모양 경계(시작 위치, charPrIDRef), hp:t 안 아는 인라인 요소는 제어 문자,
    모르는 요소는 그 글자만 남긴다(HWP에는 그런 구조가 없다: 형광펜은 영역 태그다). 런 안 개체·컨트롤은 그 자리의 확장
    컨트롤 문자와 문단 아래 컨트롤 레코드(레벨 level + 1)로(from_object)."""
    items: list[str | list[int]] = []
    shapes = []
    controls: list[bytes] = []
    for run in p.iterfind(f"{OWPML}run"):
        shapes.append((len(units(*items)), int(run.get("charPrIDRef", "0"))))
        for child in run:
            if child.tag != f"{OWPML}t":
                for code, ident, data in from_object(child, level + 1):
                    items.append(extended(code, ident))
                    controls.append(data)
                continue
            items.append(child.text or "")
            for inner in child:
                name = inner.tag.removeprefix(OWPML)
                items.append(MARKS[name] if name in MARKS else "".join(inner.itertext()))
                items.append(inner.tail or "")
    segs = p.find(f"{OWPML}linesegarray")
    lines = len(segs) if segs is not None else None
    text = [item for item in items if item != ""]
    return paragraph(*text, level=level, pr=int(p.get("paraPrIDRef", "0")), style=int(p.get("styleIDRef", "0")),
                     shapes=shapes or ((0, 0),), lines=lines, controls=controls)


def _paras(element: ET.Element | None, level: int) -> list[bytes]:
    """요소의 hp:subList 문단들 → 레벨 level 문단 레코드들(없으면 빈 목록)."""
    sub = element.find(f"{OWPML}subList") if element is not None else None
    return [from_para(p, level) for p in sub.iterfind(f"{OWPML}p")] if sub is not None else []


def _caption(element: ET.Element, level: int) -> bytes:
    found = element.find(f"{OWPML}caption")
    return caption(level, *_paras(found, level), side=found.get("side", "BOTTOM")) if found is not None else b""


def _component(element: ET.Element, level: int, top: bool) -> bytes:
    """그리기 개체 요소 → SHAPE_COMPONENT(level). 그림 id "imageK"는 BIN_DATA 순번 K다(from_items)."""
    name = element.tag.removeprefix(OWPML)
    if name == "pic":
        size, org = element.find(f"{OWPML}curSz"), element.find(f"{OWPML}orgSz")
        ref = element.find(f"{CORE_NS}img").get("binaryItemIDRef")
        first = (int(org.get("width")), int(org.get("height"))) if org is not None else (0, 0)
        now = [int(size.get(k)) or v for k, v in zip(("width", "height"), first)]  # curSz 0은 처음 값과 같다는 뜻
        return pic(level, int(ref.removeprefix("image")), *now, top=top, first=first)
    if name == "rect":
        return rect(level, *_paras(element.find(f"{OWPML}drawText"), level + 1), top=top)
    if name == "line":
        return line(level, top=top)
    if name == "container":  # 묶음의 캡션은 gso 캡션 리스트로 간다(from_object)
        return container(level, *(_component(child, level + 1, False) for child in element
                                   if child.tag != f"{OWPML}caption"))
    if name == "textart":
        return textart(level, element.get("text", ""))
    if name == "ole":
        return ole(level)
    raise ValueError(f"not a twin drawing object: {element.tag}")


def ruby(main: str, sub: str) -> bytes:
    """덧말(tdut) 몸(ID 뒤): 본말·덧말 글자, 위치·크기 비율·옵션·스타일·정렬 UINT32 다섯(사양 표 150)."""
    return hwp_string(main) + hwp_string(sub) + struct.pack("<5I", 0, 0, 0, 0, 0)


def overlap(text: str, border: int = 0) -> bytes:
    """글자 겹침(tcps) 몸(ID 뒤): 겹칠 글자, 테두리 종류(1 원)·내부 글자 크기·펼침·글자 모양 수(10) + 글자 모양 id 10개
    (사양 표 151, 실문서 꼬리 44B와 같은 꼴)."""
    return hwp_string(text) + bytes([border, 253, 0, 10]) + bytes(40)


def circled_glyph(text: str) -> str:
    """HWP가 원 모양 글자 겹침에 저장하는 겹친 글자: 유니코드 분해가 '<circle> 그 글자'인 BMP 글자(처리부 표와 따로
    유니코드 데이터에서 찾는다). 없으면 그대로."""
    want = "<circle> " + " ".join(f"{ord(ch):04X}" for ch in text)
    return next((chr(c) for c in range(0x2460, 0x10000) if unicodedata.decomposition(chr(c)) == want), text)


def from_object(element: ET.Element, level: int) -> list[tuple[int, str, bytes]]:
    """런 안 개체·컨트롤 요소 → (확장 컨트롤 코드, 컨트롤 ID, 컨트롤 레코드(level))들. hp:ctrl 하나에 든 머리말·꼬리말
    둘은 HWP 컨트롤 둘이다(처리부 출력이 같다). 덧말·글자 겹침은 확장 컨트롤 23이다."""
    name = element.tag.removeprefix(OWPML)
    if name == "dutmal":
        texts = (element.findtext(f"{OWPML}mainText") or "", element.findtext(f"{OWPML}subText") or "")
        return [(23, "tdut", control("tdut", level, body=ruby(*texts)))]
    if name == "compose":  # 원 모양이면 HWP처럼 겹친 원문자를 테두리 1(원)과 함께 쓴다
        text, circle = element.get("composeText", ""), element.get("circleType") == "SHAPE_CIRCLE"
        body = overlap(circled_glyph(text), 1) if circle else overlap(text)
        return [(23, "tcps", control("tcps", level, body=body))]
    if name == "tbl":
        rows = []
        for tc in element.iterfind(f"{OWPML}tr/{OWPML}tc"):
            addr, span = tc.find(f"{OWPML}cellAddr"), tc.find(f"{OWPML}cellSpan")
            rows.append(cell(level + 1, int(addr.get("rowAddr")), int(addr.get("colAddr")), *_paras(tc, level + 1),
                             span=(int(span.get("rowSpan")), int(span.get("colSpan"))), header=tc.get("header") == "1",
                             fill=int(tc.get("borderFillIDRef"))))
        return [(11, "tbl ", table(level, int(element.get("rowCnt")), int(element.get("colCnt")), *rows,
                                   caption=_caption(element, level + 1)))]
    if name == "equation":
        return [(11, "eqed", equation(level, element.findtext(f"{OWPML}script") or "",
                                      caption=_caption(element, level + 1)))]
    if name == "ctrl":
        out = []
        for child in element:
            kind = child.tag.removeprefix(OWPML)
            if kind in ("header", "footer"):
                out.append((16, kind[:4], margin(level, kind[:4], *_paras(child, level + 1),
                                                  apply=child.get("applyPageType", "BOTH"))))
            elif kind in ("footNote", "endNote"):
                ident = "fn  " if kind == "footNote" else "en  "
                out.append((17, ident, note(level, ident, *_paras(child, level + 1))))
            elif kind == "hiddenComment":
                out.append((15, "tcmt", hidden(level, *_paras(child, level + 1))))
            else:
                raise ValueError(f"not a twin control: {child.tag}")
        return out
    return [(11, "gso ", gso(level, _component(element, level + 1, True), caption=_caption(element, level + 1)))]


def from_items(items: Sequence[tuple[str, str, str, bool]], binaries: Mapping[str, bytes]) -> tuple[list[bytes],
                                                                                                     dict[str, bytes]]:
    """HWPX 그림 manifest 항목(id "imageK"는 K번째) → BIN_DATA 몸들과 BinData 스트림. 내장이 아니면 연결."""
    bins, streams = [], {}
    for k, (key, href, _, embedded) in enumerate(items, 1):
        assert key == f"image{k}"
        if not embedded:
            bins.append(bin_data(kind=LINK))
            continue
        ext = href.rsplit(".", 1)[1]
        bins.append(bin_data(k, ext))
        if href in binaries:
            streams[f"BinData/BIN{k:04X}.{ext}"] = binaries[href]
    return bins, streams


def from_header(xml: str, bins: Sequence[bytes] = ()) -> bytes:
    """HWPX header.xml(build_hwpx_fixtures.header()) → DocInfo 레코드(같은 id: 문단·글자 모양 0부터, 테두리 채움 1부터)와
    BIN_DATA 몸들."""
    root = ET.fromstring(xml)
    headings = {int(e.get("id")): (e.find(f"{HEAD_NS}heading").get("type"), int(e.find(f"{HEAD_NS}heading").get(
        "level"))) for e in root.iter(f"{HEAD_NS}paraPr")}
    chars = {int(e.get("id")): (int(e.get("height")), e.find(f"{HEAD_NS}bold") is not None)
             for e in root.iter(f"{HEAD_NS}charPr")}
    fills = {int(e.get("id")): e.find(f".//{CORE_NS}winBrush").get("faceColor")
             for e in root.iter(f"{HEAD_NS}borderFill")}
    return docinfo(headings=headings, chars=chars, fills=fills, bins=bins)


# ---- 쌍둥이 골든 예제 ----
BASIC = [from_section(s) for s in HX.BASIC]
FIGURE_BINS = from_items(HX.FIGURE_ITEMS, HX.FIGURE_BINARIES)
ONE_BIN = from_items(HX.FIGURE_ITEMS[:1], {"BinData/image1.png": HX.GRAY_PNG})
SAMPLES: dict[str, bytes] = {
    "basic.hwp": document(*BASIC, head=from_header(HX.BASIC_HEAD)),
    "headings.hwp": document(*(from_section(s) for s in HX.HEADINGS), head=from_header(HX.HEAD_SIZES)),
    "empty.hwp": document(from_section(HX.section(HX.para())), head=from_header(HX.header())),
    "basic-deflate.hwp": document(*BASIC, head=from_header(HX.BASIC_HEAD), compressed=True),
    "table.hwp": document(from_section(HX.TABLE), head=from_header(HX.TABLE_HEAD, ONE_BIN[0]), extra=ONE_BIN[1]),
    "figures.hwp": document(from_section(HX.FIGURES), head=from_header(HX.header(), FIGURE_BINS[0]),
                            extra=FIGURE_BINS[1]),
    "notes.hwp": document(*(from_section(s) for s in HX.NOTES), head=from_header(HX.header(), ONE_BIN[0]),
                          extra=ONE_BIN[1]),
    "objects.hwp": document(from_section(HX.OBJECTS), head=from_header(HX.header())),
}
TWINS = {"basic.hwp": "basic", "headings.hwp": "headings", "empty.hwp": "empty", "basic-deflate.hwp": "basic",
         "table.hwp": "table", "figures.hwp": "figures", "notes.hwp": "notes", "objects.hwp": "objects"}
# HWP에 같은 구조가 없어 HWP 출력에 없는 HWPX 처리 이력(region_id): hp:t 안 모르는 요소(형광펜)
DROPPED = {"basic": ("s1-p10-unknown-inline-0",)}
ROOT = HERE / "fixtures" / "hwp"
HWPX_EXPECTED = HERE / "fixtures" / "hwpx" / "expected"
COMPRESSED_STREAMS = ("DocInfo", "BodyText/")


def golden(name: str, data: bytes) -> tuple[DocumentTree, ProcessingHistory]:
    """엔진 ingest와 같은 SourceInfo로 만든 트리와 처리 이력."""
    parsed = HwpParser().parse(data, name)
    source = SourceInfo(name=name, mime=parsed.mime, content_hash="sha256:" + hashlib.sha256(data).hexdigest())
    tree = build_tree(parsed, "fx-hwp-" + Path(name).stem, 1, source)
    return tree, ProcessingHistory(document_id=tree.document_id, version=1, regions=parsed.regions)


def twin_key(tree: dict, history: dict) -> dict:
    """비교 키: 형식 정체성(source.name·mime·content_hash, document_id)만 가리고, block_id는 블록 순번(#k)으로 바꾼다
    (그 id를 가리키는 값도 같은 순번으로). 그림 자산 키에서 나온 값은 HWPX 골든처럼 portable로 가린다(OS마다 PNG 압축
    바이트가 다르다: 화소는 twin_ok가 따로 본다). 그 밖 모든 필드는 그대로 비교한다."""
    tree = HX.portable(tree)
    ids = {block["block_id"]: f"#{block['order']}" for block in tree["blocks"]}

    def walk(value: object) -> object:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return ids.get(value, value) if isinstance(value, str) else value

    tree = walk({k: v for k, v in tree.items() if k != "document_id"})  # type: ignore[assignment]
    tree["source"] = {k: v for k, v in tree["source"].items() if k not in ("name", "mime", "content_hash")}
    return {"tree": tree, "regions": history["regions"]}


def expected_key(stem: str) -> dict:
    """HWPX 골든 기대값의 비교 키(DROPPED에 적은 처리 이력은 뺀다)."""
    tree = json.loads((HWPX_EXPECTED / f"{stem}.json").read_bytes())
    history = json.loads((HWPX_EXPECTED / f"{stem}.history.json").read_bytes())
    history["regions"] = [r for r in history["regions"] if r["region_id"] not in DROPPED.get(stem, ())]
    return twin_key(tree, history)


def twin_ok(name: str, data: bytes) -> bool:
    """비교 키와 그림 화소 정보(처음 나온 차례)가 HWPX 골든과 같다."""
    tree, history = golden(name, data)
    assets = HwpParser().parse(data, name).assets
    order = list(dict.fromkeys(b.figure.asset for b in tree.blocks if b.figure is not None))
    shown = [HX.pixels(assets[key]) for key in order]
    expected = json.loads((HWPX_EXPECTED / f"{TWINS[name]}.pixels.json").read_bytes())
    return twin_key(tree.model_dump(mode="json"), history.model_dump(mode="json")) == expected_key(TWINS[name]) and \
        shown == expected


def streams_of(data: bytes) -> dict[str, bytes]:
    """복합 파일의 스트림 경로 → 바이트(압축 스트림은 푼 것: zlib 판마다 압축 바이트가 다를 수 있다)."""
    cfb = Compound(data, "x")
    header = cfb.read("FileHeader", 256)
    compressed = bool(struct.unpack_from("<I", header, 36)[0] & 1)
    out = {}
    for path in cfb.streams:
        raw = cfb.read(path, 1 << 30)
        out[path] = zlib.decompress(raw, -15) if compressed and path.startswith(COMPRESSED_STREAMS) else raw
    return out


def same(name: str, built: bytes, path: Path) -> bool:
    """입력 비교: 압축판은 스트림을 풀어 비교하고, 그 밖은 바이트 그대로."""
    if not path.exists():
        return False
    old = path.read_bytes()
    if "deflate" in name:
        return streams_of(old) == streams_of(built)
    return old == built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hanji HWP twin fixtures")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    bad = []
    for name, data in SAMPLES.items():
        path = ROOT / "inputs" / name
        if args.check:
            if not same(name, data, path):
                bad.append(f"stale fixture: inputs/{name}")
            elif not twin_ok(name, path.read_bytes()):
                bad.append(f"twin mismatch: inputs/{name} vs hwpx/expected/{TWINS[name]}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(path)
    for line in bad:
        print(line, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
