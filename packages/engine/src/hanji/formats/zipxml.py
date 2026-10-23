"""zip + XML 꾸러미를 믿을 수 없는 입력으로 읽는 공용 부분(HWPX·OOXML). 멤버는 정확한 이름으로만 열고 디스크에 풀지
않는다. 읽는 바이트는 상한까지만 읽고(zip 머리의 선언 크기를 믿지 않는다), XML은 트리를 만들기 전에 expat로 DOCTYPE·
깊이를 거른다. 상한 값은 부르는 쪽이 Limits로 넘긴다(형식마다 모듈 상수를 호출 때 읽어 몽키패치가 그대로 작동한다).
멤버 데이터 몇 바이트가 빠지거나 더해져 EOCD의 중앙 디렉터리 위치가 틀린 zip은 로컬 머리를 확인해 적힌 위치로 읽는다
(손상 멤버만 잃는다). 실패는 ParseError(원인, "{파일 이름}:{멤버 이름}")."""

import bisect
import io
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from xml.parsers import expat

from ..errors import ParseError

Prefixes = dict[str, frozenset[str]]  # 접두어 → 그 파트의 xmlns:접두어 선언에 나온 이름공간 URI들
LOCAL, END, END64 = b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x07"  # 로컬 머리, EOCD, zip64 EOCD 위치 표시


class TooLarge(Exception):
    """멤버가 크기·압축률 상한을 넘는다(그림이면 그 그림만 이미지 없이 낸다)."""


class OverBudget(TooLarge):
    """읽은 그림 멤버 합이 MAX_BINARY_TOTAL을 넘는다(남은 그림은 이미지 없이)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """읽기 상한 묶음(바이트·개수). ratio는 풀린 크기가 ratio_floor를 넘는 멤버의 선언 압축률 상한."""

    members: int
    xml_bytes: int
    xml_total: int
    binary_bytes: int
    binary_total: int
    ratio: int
    ratio_floor: int
    xml_depth: int


class _Stop(Exception):
    """expat 사전 검사를 멈춘다(사유는 reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def scan(data: bytes, location: str, max_depth: int) -> Prefixes:
    """트리를 만들기 전에 DOCTYPE·엔티티 선언을 거부하고 깊이를 센다. 엔티티 확장이 일어나기 전에 멈춘다. 돌려주는 값은
    접두어 → 선언된 URI들(ElementTree는 접두어를 버리므로 mc:Choice@Requires·mc:Ignorable을 풀 때 쓴다)."""
    parser = expat.ParserCreate()
    depth = 0
    seen: dict[str, set[str]] = {}

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > max_depth:
            raise _Stop(f"XML nesting deeper than {max_depth}")
        for key, value in attrs.items():
            if key.startswith("xmlns:"):
                seen.setdefault(key[6:], set()).add(value)

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1

    def doctype(*args: object) -> None:
        raise _Stop("XML DOCTYPE is not allowed")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = doctype
    try:
        parser.Parse(data, True)
    except _Stop as stop:
        raise ParseError(stop.reason, location) from None
    except expat.ExpatError as exc:
        raise ParseError(f"invalid XML: {expat.ErrorString(exc.code)}", f"{location}:{exc.lineno}") from None
    except (ValueError, LookupError) as exc:  # 다중바이트·모르는 인코딩 선언은 expat가 이 예외를 낸다
        raise ParseError(f"unsupported XML encoding: {exc}", location) from None
    return {prefix: frozenset(uris) for prefix, uris in seen.items()}


def _directory(data: bytes) -> tuple[int, int] | None:
    """(zipfile이 모든 멤버 위치에 더한 값, 중앙 디렉터리 실제 위치). 더한 값 = 실제 위치 − EOCD에 적힌 위치: 앞에 다른
    바이트가 붙은 zip이면 맞고, 멤버 데이터가 줄거나 는 zip이면 손상 뒤 멤버에만 맞는다. EOCD가 없거나 zip64면 None."""
    end = data.rfind(END, max(0, len(data) - 65_557))  # EOCD 22B + 주석 최대 65535B
    if end < 20 or end + 22 > len(data) or data[end - 20:end - 16] == END64:
        return None
    size, offset = struct.unpack_from("<II", data, end + 12)
    return end - size - offset, end - size


def _start(data: bytes, offset: int, name: str) -> int | None:
    """offset에 이름이 name인 로컬 머리가 있으면 그 멤버 데이터의 시작 위치, 아니면 None. 이름은 zipfile.open처럼 로컬
    머리 자기 UTF-8 플래그로 디코드한다(중앙 항목 플래그가 아니다: 둘이 다르면 zipfile도 그 멤버를 열지 못한다)."""
    if offset < 0 or offset + 30 > len(data) or data[offset:offset + 4] != LOCAL:
        return None
    (flags,) = struct.unpack_from("<H", data, offset + 6)
    n, m = struct.unpack_from("<HH", data, offset + 26)
    try:
        found = data[offset + 30:offset + 30 + n].decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeDecodeError:
        return None
    return offset + 30 + n + m if found == name else None


def _relocate(data: bytes, infos: list[zipfile.ZipInfo], name: str) -> dict[str, zipfile.ZipInfo | None]:
    """zipfile이 옮긴 위치에 로컬 머리가 없고 적힌 위치(옮기기 전)에 같은 이름의 머리가 있는 멤버 → 그 위치를 가리키는 새
    ZipInfo(공개 속성만 옮긴다: zipfile 내부 값을 고치지 않는다). 머리 이름은 zipfile처럼 orig_filename과 견준다. 그런
    멤버가 있으면 머리가 있는 모든 멤버의 구간(머리 + compress_size)을 시작 순으로 놓는다(zipfile의 겹침 검사는 옮긴
    위치 기준이라 다시 찾은 멤버를 모른다). 다시 찾은 멤버가 다음 머리·중앙 디렉터리를 넘으면 None(손상 멤버라 읽지
    않는다), 옮기지 않은 멤버가 다시 찾은 멤버의 머리를 덮으면 같은 바이트를 두 멤버로 읽을 수 있어 ParseError.
    옮기지 않은 멤버끼리의 겹침은 zipfile의 읽기 오류에 맡긴다."""
    found = _directory(data)
    if found is None or found[0] == 0:
        return {}
    shift, directory = found
    again: dict[str, int] = {}
    for info in infos:
        if _start(data, info.header_offset, info.orig_filename) is None and \
                _start(data, info.header_offset - shift, info.orig_filename) is not None:
            again[info.filename] = info.header_offset - shift
    if not again:  # 앞에 바이트가 붙은 zip: zipfile 그대로 읽는다
        return {}
    spans: list[tuple[int, int, zipfile.ZipInfo]] = []  # (머리 위치, 데이터 시작, 멤버)
    for info in infos:
        offset = again.get(info.filename, info.header_offset)
        begin = _start(data, offset, info.orig_filename)
        if begin is not None:  # 머리가 없는 멤버는 zipfile이 열지 못한다
            spans.append((offset, begin, info))
    spans.sort(key=lambda span: span[0])
    # 경계는 다음 머리와 중앙 디렉터리 시작 가운데 앞쪽(디렉터리 extra 안에 박은 머리도 디렉터리를 넘지 못한다)
    bounds = [min(span[0], directory) for span in spans[1:]] + [directory]
    found_again = sorted(again.values())
    moved: dict[str, zipfile.ZipInfo | None] = {}
    for (offset, begin, info), bound in zip(spans, bounds, strict=True):
        end = begin + info.compress_size
        if info.filename not in again:
            after = bisect.bisect_right(found_again, offset)  # 이 멤버 뒤 첫 다시 찾은 머리
            if after < len(found_again) and found_again[after] < end:
                raise ParseError("overlapped zip members", f"{name}:{info.filename}")
            continue
        if end > bound:
            moved[info.filename] = None
            continue
        fresh = zipfile.ZipInfo(info.orig_filename)
        for key in ("compress_type", "flag_bits", "CRC", "compress_size", "file_size", "extra"):
            setattr(fresh, key, getattr(info, key))
        fresh.header_offset = offset
        moved[info.filename] = fresh
    return moved


def _unicode_paths(extra: bytes) -> list[bytes]:
    """중앙 디렉터리 extra의 Info-ZIP 유니코드 경로(0x7075) 이름 바이트들. zipfile은 판·CRC가 맞으면 이것을 filename으로
    쓰고 첫 NUL에서 자른다(윈도에서는 \\도 /로 바꾼다): 잘리기 전 이름을 보려고 직접 읽는다. 판·CRC는 보지 않는다."""
    names = []
    while len(extra) >= 4:
        kind, size = struct.unpack_from("<HH", extra)
        if kind == 0x7075 and size >= 5:
            names.append(extra[9:4 + size])
        extra = extra[4 + size:]
    return names


class Archive:
    """열린 zip 꾸러미. 생성자가 zip과 멤버 목록(수·이름 중복·역슬래시·NUL·암호화·압축 방식)을 확인한다. 상한은 limits가
    호출 때마다 돌려준다(형식이 자기 모듈 상수로 만든다). xml_read·binary_read는 실제로 읽은 바이트 합이다."""

    def __init__(self, data: bytes, name: str, not_zip: str, limits: Callable[[], Limits]) -> None:
        self.name = name
        self.limits = limits
        self.xml_read = 0
        self.binary_read = 0
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError):
            raise ParseError(not_zip, name) from None
        infos = self._zip.infolist()
        members = self.limits().members
        if len(infos) > members:
            raise ParseError(f"too many zip members (over {members})", name)
        self._infos: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.filename in self._infos:
                raise ParseError("duplicate zip member name", f"{name}:{info.filename}")
            # 머리 이름·0x7075 이름 모두 본다: zipfile은 filename을 첫 NUL에서 자르고 윈도에서는 \를 /로 바꾼다
            paths = [path.decode("utf-8", "replace") for path in _unicode_paths(info.extra)]
            names = [info.filename, info.orig_filename, *paths]
            if any("\\" in n for n in names):  # 윈도와 다른 OS가 \를 다르게 읽으므로 모두 거부한다
                raise ParseError("backslash in zip member name", f"{name}:{info.filename}")
            if any("\0" in n for n in names):  # 로컬 머리는 원래 이름과 견주므로 잘린 이름과 어긋난다
                raise ParseError("NUL in zip member name", f"{name}:{info.filename}")
            if info.flag_bits & 1:
                raise ParseError("encrypted zip member", f"{name}:{info.filename}")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                # BZIP2·LZMA는 읽기 상한 없이 풀리므로 거부한다(한컴·Office는 무압축·deflate만 쓴다)
                raise ParseError("unsupported zip compression method", f"{name}:{info.filename}")
            self._infos[info.filename] = info
        self._moved = _relocate(data, infos, name)  # 적힌 위치로 다시 찾은 멤버(None: 겹쳐서 읽지 않는다)

    def has(self, member: str) -> bool:
        return member in self._infos

    def _read(self, member: str, size: int) -> bytes:
        """정확한 이름의 멤버를 size 바이트까지만 읽는다(부르는 쪽이 상한 + 1을 주고 길이로 넘음을 안다). 선언 압축률이
        상한을 넘으면 열지 않고 TooLarge, zip 오류(CRC 등)는 ParseError."""
        info = self._infos[member]
        limits = self.limits()
        if info.file_size > limits.ratio_floor and info.file_size > limits.ratio * max(info.compress_size, 1):
            raise TooLarge(f"zip member compression ratio over {limits.ratio}")
        if member in self._moved:
            moved = self._moved[member]
            if moved is None:
                raise ParseError("cannot read zip member: overlapped entries", f"{self.name}:{member}")
            info = moved
        try:
            with self._zip.open(info) as stream:
                return stream.read(size)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError, OSError) as exc:
            raise ParseError(f"cannot read zip member: {exc}", f"{self.name}:{member}") from None

    def xml(self, member: str, root: str | None) -> ET.Element:
        """XML 멤버를 상한 안에서 읽고 expat 사전 검사 뒤 트리로. 뿌리 요소 이름(root, {URI}local)이 다르면 ParseError
        (None이면 보지 않는다). 읽기는 min(멤버 상한, 합 상한의 남은 몫) + 1바이트까지만 한다."""
        return self.tree(member, root)[0]

    def tree(self, member: str, root: str | None) -> tuple[ET.Element, Prefixes]:
        """xml()과 같고 사전 검사가 모은 접두어 선언도 돌려준다."""
        location = f"{self.name}:{member}"
        if member not in self._infos:
            raise ParseError("missing zip member", location)
        limits = self.limits()
        left = limits.xml_total - self.xml_read
        try:
            data = self._read(member, min(limits.xml_bytes, left) + 1)
        except TooLarge as exc:
            raise ParseError(str(exc), location) from None
        self.xml_read += len(data)
        if len(data) > limits.xml_bytes:
            raise ParseError(f"zip member larger than {limits.xml_bytes} bytes", location)
        if len(data) > left:
            raise ParseError(f"XML members larger than {limits.xml_total} bytes in total", location)
        prefixes = scan(data, location, limits.xml_depth)
        try:
            tree = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ParseError(f"invalid XML: {exc}", f"{location}:{exc.position[0]}") from None
        except (ValueError, LookupError) as exc:
            raise ParseError(f"unsupported XML encoding: {exc}", location) from None
        if root is not None and tree.tag != root:
            raise ParseError(f"unexpected root element {tree.tag}", location)
        return tree, prefixes

    def binary(self, member: str) -> bytes:
        """그림 멤버 바이트. 하나가 멤버 상한·압축률을 넘으면 TooLarge, 합이 합 상한을 넘으면 OverBudget. 읽기는
        min(멤버 상한, 합 상한의 남은 몫) + 1바이트까지만 하고, 합 상한을 다 쓴 뒤에는 멤버를 열지 않는다(실제로 읽은
        합 ≤ 합 상한 + 1)."""
        if member not in self._infos:
            raise ParseError("missing zip member", f"{self.name}:{member}")
        limits = self.limits()
        left = limits.binary_total - self.binary_read
        if left <= 0:
            raise OverBudget(f"picture members larger than {limits.binary_total} bytes in total")
        data = self._read(member, min(limits.binary_bytes, left) + 1)
        self.binary_read += len(data)
        if len(data) > limits.binary_bytes:
            raise TooLarge(f"zip member larger than {limits.binary_bytes} bytes")
        if len(data) > left:
            raise OverBudget(f"picture members larger than {limits.binary_total} bytes in total")
        return data
