"""HWPX 컨테이너(zip + XML)를 믿을 수 없는 입력으로 읽는다. 멤버는 정확한 이름으로만 열고 디스크에 풀지 않는다. 읽는
바이트는 상한까지만 읽고(zip 머리의 선언 크기를 믿지 않는다), XML은 트리를 만들기 전에 expat로 DOCTYPE·깊이를 거른다.
실패는 ParseError(원인, "{파일 이름}:{멤버 이름}")."""

import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from xml.parsers import expat

from ...errors import ParseError

MIME = "application/hwp+zip"
HS = "{http://www.hancom.co.kr/hwpml/2011/section}"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HC = "{http://www.hancom.co.kr/hwpml/2011/core}"
HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
OPF = "{http://www.idpf.org/2007/opf/}"
MiB = 1024 * 1024
MAX_MEMBERS = 4096  # zip 멤버 수
MAX_XML_BYTES = 64 * MiB  # XML 멤버 하나(풀린 크기)
MAX_XML_TOTAL = 256 * MiB  # 읽은 XML 합
MAX_BINARY_BYTES = 64 * MiB  # 그림 멤버 하나
MAX_BINARY_TOTAL = 512 * MiB  # 읽은 그림 멤버 합
MAX_RATIO = 200  # 선언 압축률 상한(풀린 크기가 RATIO_FLOOR를 넘는 멤버만)
RATIO_FLOOR = MiB
MAX_XML_DEPTH = 256  # 요소 중첩 깊이(expat 사전 검사가 센다)
MIMETYPE_BYTES = 256  # mimetype 멤버는 이만큼만 풀어 읽는다
CONTENT = "Contents/content.hpf"
HEADER = "Contents/header.xml"
SECTION = re.compile(r"Contents/section\d+\.xml")
MASTERPAGE = re.compile(r"Contents/masterpage\d+\.xml")


class TooLarge(Exception):
    """멤버가 크기·압축률 상한을 넘는다(그림이면 그 그림만 이미지 없이 낸다)."""


class OverBudget(TooLarge):
    """읽은 그림 멤버 합이 MAX_BINARY_TOTAL을 넘는다(남은 그림은 이미지 없이)."""


@dataclass(frozen=True, slots=True)
class Item:
    """content.hpf manifest 항목."""

    href: str
    media_type: str
    embedded: bool


class _Stop(Exception):
    """expat 사전 검사를 멈춘다(사유는 reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _scan(data: bytes, location: str) -> None:
    """트리를 만들기 전에 DOCTYPE·엔티티 선언을 거부하고 깊이를 센다. 엔티티 확장이 일어나기 전에 멈춘다."""
    parser = expat.ParserCreate()
    depth = 0

    def start(name: str, attrs: object) -> None:
        nonlocal depth
        depth += 1
        if depth > MAX_XML_DEPTH:
            raise _Stop(f"XML nesting deeper than {MAX_XML_DEPTH}")

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


class Package:
    """열린 HWPX 꾸러미. 생성자가 zip·mimetype·content.hpf(manifest·spine)를 확인한다. sections는 spine 순서의
    구역 멤버 이름, masterpages는 zip의 마스터 쪽 멤버 이름이다."""

    def __init__(self, data: bytes, name: str) -> None:
        self.name = name
        self.xml_read = 0
        self.binary_read = 0
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError):
            raise ParseError("not an HWPX (zip) package (HWP 5.0 binary documents are not supported yet)",
                             name) from None
        infos = self._zip.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ParseError(f"too many zip members (over {MAX_MEMBERS})", name)
        self._infos: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.filename in self._infos:
                raise ParseError("duplicate zip member name", f"{name}:{info.filename}")
            if info.flag_bits & 1:
                raise ParseError("encrypted zip member", f"{name}:{info.filename}")
            self._infos[info.filename] = info
        self._mimetype()
        self.items, spine = self._content()
        self.sections = self._spine(spine)
        self.masterpages = tuple(n for n in self._infos if MASTERPAGE.fullmatch(n))

    def has(self, member: str) -> bool:
        return member in self._infos

    def _read(self, member: str, size: int) -> bytes:
        """정확한 이름의 멤버를 size 바이트까지만 읽는다(부르는 쪽이 상한 + 1을 주고 길이로 넘음을 안다). 선언 압축률이
        MAX_RATIO를 넘으면 열지 않고 TooLarge, zip 오류(CRC 등)는 ParseError."""
        info = self._infos[member]
        if info.file_size > RATIO_FLOOR and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise TooLarge(f"zip member compression ratio over {MAX_RATIO}")
        try:
            with self._zip.open(info) as stream:
                return stream.read(size)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError, OSError) as exc:
            raise ParseError(f"cannot read zip member: {exc}", f"{self.name}:{member}") from None

    def _mimetype(self) -> None:
        if "mimetype" not in self._infos:
            raise ParseError("missing mimetype member", self.name)
        try:
            raw = self._read("mimetype", MIMETYPE_BYTES + 1)
        except TooLarge as exc:
            raise ParseError(str(exc), f"{self.name}:mimetype") from None
        if len(raw) > MIMETYPE_BYTES:
            raise ParseError(f"mimetype member longer than {MIMETYPE_BYTES} bytes", f"{self.name}:mimetype")
        if raw.strip(b" \t\r\n") != MIME.encode("ascii"):
            raise ParseError(f"mimetype is not {MIME}", f"{self.name}:mimetype")

    def xml(self, member: str, root: str | None) -> ET.Element:
        """XML 멤버를 상한 안에서 읽고 expat 사전 검사 뒤 트리로. 뿌리 요소 이름(root, {URI}local)이 다르면 ParseError
        (None이면 보지 않는다: 마스터 쪽). 읽기는 min(MAX_XML_BYTES, 합 상한의 남은 몫) + 1바이트까지만 한다."""
        location = f"{self.name}:{member}"
        if member not in self._infos:
            raise ParseError("missing zip member", location)
        left = MAX_XML_TOTAL - self.xml_read
        try:
            data = self._read(member, min(MAX_XML_BYTES, left) + 1)
        except TooLarge as exc:
            raise ParseError(str(exc), location) from None
        self.xml_read += len(data)
        if len(data) > MAX_XML_BYTES:
            raise ParseError(f"zip member larger than {MAX_XML_BYTES} bytes", location)
        if len(data) > left:
            raise ParseError(f"XML members larger than {MAX_XML_TOTAL} bytes in total", location)
        _scan(data, location)
        try:
            tree = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ParseError(f"invalid XML: {exc}", f"{location}:{exc.position[0]}") from None
        if root is not None and tree.tag != root:
            raise ParseError(f"unexpected root element {tree.tag}", location)
        return tree

    def binary(self, member: str) -> bytes:
        """그림 멤버 바이트. 하나가 MAX_BINARY_BYTES·압축률을 넘으면 TooLarge, 합이 MAX_BINARY_TOTAL을 넘으면 OverBudget.
        읽기는 min(MAX_BINARY_BYTES, 합 상한의 남은 몫) + 1바이트까지만 하고, 합 상한을 다 쓴 뒤에는 멤버를 열지 않는다
        (실제로 읽은 합 ≤ MAX_BINARY_TOTAL + 1)."""
        left = MAX_BINARY_TOTAL - self.binary_read
        if left <= 0:
            raise OverBudget(f"picture members larger than {MAX_BINARY_TOTAL} bytes in total")
        data = self._read(member, min(MAX_BINARY_BYTES, left) + 1)
        self.binary_read += len(data)
        if len(data) > MAX_BINARY_BYTES:
            raise TooLarge(f"zip member larger than {MAX_BINARY_BYTES} bytes")
        if len(data) > left:
            raise OverBudget(f"picture members larger than {MAX_BINARY_TOTAL} bytes in total")
        return data

    def _content(self) -> tuple[dict[str, Item], list[str]]:
        root = self.xml(CONTENT, f"{OPF}package")
        location = f"{self.name}:{CONTENT}"
        items: dict[str, Item] = {}
        for item in root.iterfind(f"{OPF}manifest/{OPF}item"):
            key = item.get("id", "")
            if key in items:
                raise ParseError(f"duplicate manifest id {key!r}", location)
            items[key] = Item(item.get("href", ""), item.get("media-type", ""), item.get("isEmbeded", "1") != "0")
        spine = [ref.get("idref", "") for ref in root.iterfind(f"{OPF}spine/{OPF}itemref")]
        return items, spine

    def _spine(self, spine: list[str]) -> tuple[str, ...]:
        """spine 순서의 구역 멤버. 어긋나면 ParseError: 건너뛰거나 두 번 읽으면 장부가 그것을 놓친다."""
        location = f"{self.name}:{CONTENT}"
        if len(set(spine)) != len(spine):
            raise ParseError("duplicate spine itemref", location)
        sections: list[str] = []
        hrefs: set[str] = set()
        for ref in spine:
            if ref not in self.items:
                raise ParseError(f"spine itemref {ref!r} is not in the manifest", location)
            href = self.items[ref].href
            if href != HEADER and not SECTION.fullmatch(href):
                raise ParseError(f"unsupported spine item {href!r}", location)
            if href in hrefs:
                raise ParseError(f"spine refers to {href!r} twice", location)
            hrefs.add(href)
            if href not in self._infos:
                raise ParseError("spine member is missing", f"{self.name}:{href}")
            if href != HEADER:
                sections.append(href)
        orphans = sorted(n for n in self._infos if SECTION.fullmatch(n) and n not in hrefs)
        if orphans:
            raise ParseError("section member is not in the spine", f"{self.name}:{orphans[0]}")
        if not sections:
            raise ParseError("no sections", location)
        return tuple(sections)
