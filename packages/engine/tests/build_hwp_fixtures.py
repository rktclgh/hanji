"""HWP 5.0 합성 문서 쓰기 도구와 쌍둥이 골든 예제 생성기(지어낸 한국어 글자만, HWP 스펙 §11.1).

    uv run python packages/engine/tests/build_hwp_fixtures.py          # fixtures/hwp/inputs/ 재생성
    uv run python packages/engine/tests/build_hwp_fixtures.py --check  # 입력이 최신이고 쌍둥이가 맞는지 확인

자체 OLE 복합 파일(MS-CFB) 쓰기 도구: 512B 섹터(major 3) 또는 4096B(major 4), FAT·mini FAT·디렉터리, 고정 차례(경로
이름 차례, 형제는 CFB 비교 규칙 차례로 right에 잇는다), 시각 0. 레코드 쓰기 도구: 레코드 머리(0xFFF 확장 크기),
FileHeader, raw deflate, DocInfo(build_hwpx_fixtures.header()와 같은 인자), 문단(PARA_HEADER·PARA_TEXT·
PARA_CHAR_SHAPE·PARA_LINE_SEG)과 컨트롤, 문서 하나(document). 테스트는 이 파일을 importlib로 읽어 쓰기 도구를 쓰고,
tamper로 직렬화 직전 배치를 고쳐 잘못된 파일을 만든다.

쌍둥이 골든: HWP 입력은 build_hwpx_fixtures.py의 같은 예제(구역 XML·header.xml)를 레코드로 옮겨 만들고, 기대값은 HWPX
골든(fixtures/hwpx/expected)을 그대로 쓴다. 비교는 형식 정체성(source.name·mime·content_hash, document_id)만 가리고
block_id는 블록 순번으로 바꾼다(참조도 같이). HWP에 같은 것이 없는 HWPX 처리 이력은 DROPPED에 적은 것만 뺀다.
"""

import argparse
import hashlib
import importlib.util
import json
import struct
import sys
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
DOCUMENT_PROPERTIES, ID_MAPPINGS, BORDER_FILL, CHAR_SHAPE, PARA_SHAPE = 0x10, 0x11, 0x14, 0x15, 0x19
HEADS = ("NONE", "OUTLINE", "NUMBER", "BULLET")


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
            fills: Mapping[int, str] = (), sections: int = 1, extra: Sequence[bytes] = ()) -> bytes:
    """DocInfo 레코드(풀린 바이트). 인자는 build_hwpx_fixtures.header()와 같다(문단 모양 0 NONE·글자 모양 0 10pt·
    테두리 채움 1 none은 늘 있다). HWP id는 순번이라 id가 0(테두리 채움은 1)부터 이어져야 한다. extra는 끝에 붙일
    레코드(레벨 1)."""
    headings = {0: ("NONE", 0), **dict(headings)}
    chars = {0: (1000, False), **dict(chars)}
    fills = {1: "none", **dict(fills)}
    assert list(headings) == list(range(len(headings))) and list(chars) == list(range(len(chars)))
    assert list(fills) == list(range(1, len(fills) + 1))
    counts = [0] * 18
    counts[8], counts[9], counts[13] = len(fills), len(chars), len(headings)
    out = [record(DOCUMENT_PROPERTIES, 0, struct.pack("<H", sections) + bytes(24)),
           record(ID_MAPPINGS, 0, struct.pack("<18i", *counts))]
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
    """HWPX 구역 XML(hp:p·hp:run·hp:t와 그 안 인라인 요소·hp:linesegarray만) → 구역 레코드. 런마다 글자 모양 경계
    (시작 위치, charPrIDRef), hp:t 안 모르는 요소는 그 글자만 남긴다(HWP에는 그런 구조가 없다: 형광펜은 영역 태그다)."""
    out = []
    for p in ET.fromstring(xml):
        items: list[str | list[int]] = []
        shapes = []
        for run in p.iterfind(f"{OWPML}run"):
            shapes.append((len(units(*items)), int(run.get("charPrIDRef", "0"))))
            for t in run:
                if t.tag != f"{OWPML}t":
                    raise ValueError(f"not an HP-A twin element: {t.tag}")
                items.append(t.text or "")
                for child in t:
                    name = child.tag.removeprefix(OWPML)
                    items.append(MARKS[name] if name in MARKS else "".join(child.itertext()))
                    items.append(child.tail or "")
        segs = p.find(f"{OWPML}linesegarray")
        lines = len(segs) if segs is not None else None
        text = [item for item in items if item != ""]
        out.append(paragraph(*text, pr=int(p.get("paraPrIDRef", "0")), style=int(p.get("styleIDRef", "0")),
                             shapes=shapes or ((0, 0),), lines=lines))
    return b"".join(out)


def from_header(xml: str) -> bytes:
    """HWPX header.xml(build_hwpx_fixtures.header()) → DocInfo 레코드(같은 id: 문단·글자 모양 0부터, 테두리 채움 1부터)."""
    root = ET.fromstring(xml)
    headings = {int(e.get("id")): (e.find(f"{HEAD_NS}heading").get("type"), int(e.find(f"{HEAD_NS}heading").get(
        "level"))) for e in root.iter(f"{HEAD_NS}paraPr")}
    chars = {int(e.get("id")): (int(e.get("height")), e.find(f"{HEAD_NS}bold") is not None)
             for e in root.iter(f"{HEAD_NS}charPr")}
    fills = {int(e.get("id")): e.find(f".//{CORE_NS}winBrush").get("faceColor")
             for e in root.iter(f"{HEAD_NS}borderFill")}
    return docinfo(headings=headings, chars=chars, fills=fills)


# ---- 쌍둥이 골든 예제 ----
BASIC = [from_section(s) for s in HX.BASIC]
SAMPLES: dict[str, bytes] = {
    "basic.hwp": document(*BASIC, head=from_header(HX.BASIC_HEAD)),
    "headings.hwp": document(*(from_section(s) for s in HX.HEADINGS), head=from_header(HX.HEAD_SIZES)),
    "empty.hwp": document(from_section(HX.section(HX.para())), head=from_header(HX.header())),
    "basic-deflate.hwp": document(*BASIC, head=from_header(HX.BASIC_HEAD), compressed=True),
}
TWINS = {"basic.hwp": "basic", "headings.hwp": "headings", "empty.hwp": "empty", "basic-deflate.hwp": "basic"}
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
    (그 id를 가리키는 값도 같은 순번으로). 그 밖 모든 필드는 그대로 비교한다."""
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
    tree, history = golden(name, data)
    return twin_key(tree.model_dump(mode="json"), history.model_dump(mode="json")) == expected_key(TWINS[name])


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
