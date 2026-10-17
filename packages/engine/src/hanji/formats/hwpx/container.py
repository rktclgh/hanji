"""HWPX 컨테이너(zip + XML)를 믿을 수 없는 입력으로 읽는다. 멤버는 정확한 이름으로만 열고 디스크에 풀지 않는다. 읽는
바이트는 상한까지만 읽고(zip 머리의 선언 크기를 믿지 않는다), XML은 트리를 만들기 전에 expat로 DOCTYPE·깊이를 거른다.
실패는 ParseError(원인, "{파일 이름}:{멤버 이름}"). zip·XML 안전 읽기는 공용 zipxml.Archive이고, 상한은 이 모듈의
MAX_* 값을 읽을 때마다 넘긴다(테스트가 바꿔 끼우는 자리)."""

import re
from dataclasses import dataclass

from ...errors import ParseError
from ..zipxml import Archive, Limits, OverBudget, TooLarge  # noqa: F401  (OverBudget·TooLarge는 다시 내보낸다)

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


@dataclass(frozen=True, slots=True)
class Item:
    """content.hpf manifest 항목."""

    href: str
    media_type: str
    embedded: bool


def _limits() -> Limits:
    """이 모듈의 상한 값(호출 때 읽는다: 몽키패치가 그대로 작동한다)."""
    return Limits(members=MAX_MEMBERS, xml_bytes=MAX_XML_BYTES, xml_total=MAX_XML_TOTAL, binary_bytes=MAX_BINARY_BYTES,
                  binary_total=MAX_BINARY_TOTAL, ratio=MAX_RATIO, ratio_floor=RATIO_FLOOR, xml_depth=MAX_XML_DEPTH)


class Package(Archive):
    """열린 HWPX 꾸러미. 생성자가 zip·mimetype·content.hpf(manifest·spine)를 확인한다. sections는 spine 순서의
    구역 멤버 이름, masterpages는 zip의 마스터 쪽 멤버 이름이다."""

    def __init__(self, data: bytes, name: str) -> None:
        super().__init__(data, name, "not an HWPX (zip) package", _limits)
        self._mimetype()
        self.items, spine = self._content()
        self.sections = self._spine(spine)
        self.masterpages = tuple(n for n in self._infos if MASTERPAGE.fullmatch(n))

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
