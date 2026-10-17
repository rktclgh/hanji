"""HWP 5.0 합성 문서 쓰기 도구(지어낸 한국어 글자만).

자체 OLE 복합 파일(MS-CFB) 쓰기 도구: 512B 섹터(major 3) 또는 4096B(major 4), FAT·mini FAT·디렉터리, 고정 차례(경로
이름 차례, 형제는 CFB 비교 규칙 차례로 right에 잇는다), 시각 0. 레코드 쓰기 도구: 레코드 머리(0xFFF 확장 크기),
FileHeader, raw deflate. 테스트는 이 파일을 importlib로 읽어 쓰기 도구를 쓰고, tamper로 직렬화 직전 배치를 고쳐
잘못된 파일을 만든다.
"""

import struct
import zlib
from collections.abc import Callable, Mapping
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
