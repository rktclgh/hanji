"""FileHeader(HWP 스펙 §5.1), 레코드 스트림 압축 풀기(§5.2), 레코드 읽기(§5.3)와 상한(§5.4).

압축 문서의 DocInfo·BodyText는 사양 문구와 달리 머리 없는 raw deflate다(wbits −15). 푼 바이트는 상한 + 1까지만 만든다.
레코드 머리 4B는 TagID(하위 10비트)·Level(다음 10비트)·Size(상위 12비트, 0xFFF면 뒤 DWORD)다. 부모는 바로 앞의
(Level − 1) 레코드이고 레벨이 한 번에 2 이상 오르면 ParseError다(명시적 스택, 재귀 없음). 상한은 모듈 상수이고 읽을
때마다 읽는다(테스트가 바꿔 끼우는 자리). 레코드 수·푼 바이트 합은 문서 전체 예산이다."""

import struct
import zlib
from dataclasses import dataclass, field

from ...errors import ParseError
from .cfb import Compound

MiB = 1024 * 1024
MAX_STREAM_BYTES = 64 * MiB  # DocInfo·Section 하나(풀린 크기). 실측 최대 2.54MB
MAX_STREAM_TOTAL = 256 * MiB  # 읽은 DocInfo·Section 합. 실측 최대 2.67MB
MAX_RECORDS = 1_000_000  # 문서 전체 레코드 수(DocInfo 포함). 레코드를 만들기 전에 센다
MAX_LEVEL = 64  # 레코드 레벨. 실측 최대 8
HEADER_BYTES = 256  # FileHeader는 이만큼만 읽는다
SIGNATURE = b"HWP Document File"
HWP3_SIGNATURE = b"HWP Document File V3.00"  # 옛 HWP 3.x(OLE 복합 파일이 아니다)
COMPRESSED, PASSWORD, DISTRIBUTION, DRM, CERT_ENCRYPT, CERT_DRM = 1, 1 << 1, 1 << 2, 1 << 4, 1 << 8, 1 << 10


@dataclass(frozen=True, slots=True)
class FileHeader:
    version: tuple[int, int, int, int]  # (MM, nn, PP, rr)
    flags: int

    @property
    def compressed(self) -> bool:
        return bool(self.flags & COMPRESSED)

    @property
    def distribution(self) -> bool:
        return bool(self.flags & DISTRIBUTION)


def file_header(cfb: Compound) -> FileHeader:
    """FileHeader 스트림(필수, 256B까지): 서명, 버전 MM = 5만, 암호·DRM은 ParseError."""
    location = f"{cfb.name}:FileHeader"
    if not cfb.has("FileHeader"):
        raise ParseError("missing FileHeader stream", cfb.name)
    raw = cfb.read("FileHeader", HEADER_BYTES)
    if len(raw) < 40 or raw[:32].rstrip(b"\0") != SIGNATURE:
        raise ParseError("not an HWP 5.0 document (FileHeader signature)", location)
    value, flags = struct.unpack_from("<II", raw, 32)
    version = (value >> 24, value >> 16 & 0xFF, value >> 8 & 0xFF, value & 0xFF)
    if version[0] != 5:
        raise ParseError(f"unsupported HWP version {'.'.join(map(str, version))}", location)
    if flags & PASSWORD:
        raise ParseError("password-protected HWP document", location)
    if flags & (DRM | CERT_ENCRYPT | CERT_DRM):
        raise ParseError("DRM-protected HWP document", location)
    return FileHeader(version, flags)


@dataclass(slots=True)
class Record:
    """레코드 하나: 태그, 레벨, 몸, 스트림 안 순번(0부터), 자식(바로 뒤 레벨 + 1 레코드들)."""

    tag: int
    level: int
    body: bytes
    index: int
    children: list["Record"] = field(default_factory=list)


class Reader:
    """한 문서의 레코드 스트림 읽기. compressed는 FileHeader bit0. 푼 바이트 합(read)·레코드 수(records)는 문서
    전체 예산이다."""

    def __init__(self, cfb: Compound, compressed: bool) -> None:
        self.cfb = cfb
        self.compressed = compressed
        self.read = 0
        self.records = 0

    def data(self, path: str) -> bytes:
        """스트림 바이트(압축이면 푼 것). 하나가 MAX_STREAM_BYTES, 합이 MAX_STREAM_TOTAL을 넘으면 ParseError."""
        location = f"{self.cfb.name}:{path}"
        left = MAX_STREAM_TOTAL - self.read
        limit = min(MAX_STREAM_BYTES, left)
        raw = self.cfb.read(path, MAX_STREAM_BYTES)
        if len(raw) > MAX_STREAM_BYTES:
            raise ParseError(f"HWP stream larger than {MAX_STREAM_BYTES} bytes", location)
        if self.compressed:
            inflater = zlib.decompressobj(-15)
            try:
                out = inflater.decompress(raw, limit + 1)
            except zlib.error as exc:
                raise ParseError(f"invalid compressed stream: {exc}", location) from None
            if len(out) <= limit and not inflater.eof:
                raise ParseError("truncated compressed stream", location)
        else:
            out = raw[:limit + 1]
        self.read += len(out)
        if len(out) > MAX_STREAM_BYTES:
            raise ParseError(f"HWP stream larger than {MAX_STREAM_BYTES} bytes", location)
        if len(out) > left:
            raise ParseError(f"HWP streams larger than {MAX_STREAM_TOTAL} bytes in total", location)
        return out

    def tree(self, path: str) -> list[Record]:
        """스트림의 레코드 트리(레벨 0 레코드들). 잘린 레코드·레벨 점프·상한 초과는 ParseError(위치에 #순번)."""
        data = self.data(path)
        location = f"{self.cfb.name}:{path}"
        top: list[Record] = []
        stack: list[Record] = []
        at, index, previous = 0, 0, -1
        while at < len(data):
            if len(data) - at < 4:
                raise ParseError("truncated record header", f"{location}#{index}")
            (head,) = struct.unpack_from("<I", data, at)
            tag, level, size = head & 0x3FF, head >> 10 & 0x3FF, head >> 20
            at += 4
            if size == 0xFFF:
                if len(data) - at < 4:
                    raise ParseError("truncated record header", f"{location}#{index}")
                (size,) = struct.unpack_from("<I", data, at)
                at += 4
            if size > len(data) - at:
                raise ParseError("truncated record", f"{location}#{index}")
            if level > MAX_LEVEL:
                raise ParseError(f"record level deeper than {MAX_LEVEL}", f"{location}#{index}")
            if level > previous + 1:
                raise ParseError("record level jump", f"{location}#{index}")
            self.records += 1
            if self.records > MAX_RECORDS:
                raise ParseError(f"more than {MAX_RECORDS} records in the document", f"{location}#{index}")
            record = Record(tag, level, data[at:at + size], index)
            del stack[level:]
            (stack[-1].children if stack else top).append(record)
            stack.append(record)
            at += size
            index += 1
            previous = level
        return top
