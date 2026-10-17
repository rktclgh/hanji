"""HWP 5.0 합성 문서 쓰기 도구(지어낸 한국어 글자만).

자체 OLE 복합 파일(MS-CFB) 쓰기 도구: 512B 섹터(major 3) 또는 4096B(major 4), FAT·mini FAT·디렉터리, 고정 차례(경로
이름 차례, 형제는 CFB 비교 규칙 차례로 right에 잇는다), 시각 0. 레코드 쓰기 도구: 레코드 머리(0xFFF 확장 크기),
FileHeader, raw deflate, DocInfo(build_hwpx_fixtures.header()와 같은 인자), 문단(PARA_HEADER·PARA_TEXT·
PARA_CHAR_SHAPE·PARA_LINE_SEG)과 컨트롤. 테스트는 이 파일을 importlib로 읽어 쓰기 도구를 쓰고, tamper로 직렬화 직전
배치를 고쳐 잘못된 파일을 만든다.
"""

import struct
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

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
