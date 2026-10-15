"""OOXML 꾸러미(스펙 §4): [Content_Types].xml, 관계(_rels), 주 파트 형식, Strict·옛 바이너리 거부, 같은 내용 두
형식(mc:AlternateContent)과 무시 표시(mc:Ignorable) 판정 도우미. zip·XML 안전 읽기는 공용 zipxml.Archive이고 상한은 이
모듈의 MAX_* 값을 읽을 때마다 넘긴다(테스트가 바꿔 끼우는 자리). 이름공간은 URI로 비교한다(Transitional 2006 판)."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote

from ...errors import ParseError
from ..zipxml import Archive, Limits, Prefixes

MiB = 1024 * 1024
MAX_MEMBERS = 4096  # 상한 값은 HWPX와 같다(스펙 §4.1)
MAX_XML_BYTES = 64 * MiB
MAX_XML_TOTAL = 256 * MiB
MAX_BINARY_BYTES = 64 * MiB
MAX_BINARY_TOTAL = 512 * MiB
MAX_RATIO = 200
RATIO_FLOOR = MiB
MAX_XML_DEPTH = 256

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_URI = "http://schemas.openxmlformats.org/markup-compatibility/2006"
MC = "{" + MC_URI + "}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
STRICT = "http://purl.oclc.org/ooxml/"
OFFICE_DOCUMENT = f"{R}/officeDocument"
CONTENT_TYPES = "[Content_Types].xml"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 옛 .doc·.ppt(복합 문서) 머리
# 읽는 이름공간 집합 S(스펙 §5.4): 이 파서가 내용을 읽는 이름공간. 실문서 26건의 mc:Choice@Requires(wps·wpg·wpc·v·c14·
# p14)가 모두 든다. 여기에 없는 이름공간을 요구하는 Choice는 고르지 않고, mc:Ignorable이면 그 요소를 무시한다
READ = frozenset({
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    R,
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "http://schemas.openxmlformats.org/presentationml/2006/main",
    "urn:schemas-microsoft-com:vml",
    "urn:schemas-microsoft-com:office:office",
    "urn:schemas-microsoft-com:office:word",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "http://schemas.microsoft.com/office/word/2010/wordml",
    "http://schemas.microsoft.com/office/word/2012/wordml",
    "http://schemas.microsoft.com/office/drawing/2010/main",
    "http://schemas.microsoft.com/office/drawing/2007/8/2/chart",
    "http://schemas.microsoft.com/office/drawing/2008/diagram",
    "http://schemas.microsoft.com/office/powerpoint/2010/main",
})
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def _limits() -> Limits:
    """이 모듈의 상한 값(호출 때 읽는다: 몽키패치가 그대로 작동한다)."""
    return Limits(members=MAX_MEMBERS, xml_bytes=MAX_XML_BYTES, xml_total=MAX_XML_TOTAL, binary_bytes=MAX_BINARY_BYTES,
                  binary_total=MAX_BINARY_TOTAL, ratio=MAX_RATIO, ratio_floor=RATIO_FLOOR, xml_depth=MAX_XML_DEPTH)


@dataclass(frozen=True, slots=True)
class Rel:
    """관계 하나. 외부 관계(TargetMode="External")는 따라가지 않아 target이 None이다."""

    id: str
    type: str
    target: str | None


def rels_member(part: str) -> str:
    """파트 x/y.xml의 관계 멤버 x/_rels/y.xml.rels(꾸러미 뿌리 "" → _rels/.rels)."""
    folder, _, base = part.rpartition("/")
    return f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"


def resolve(part: str, target: str, location: str) -> str:
    """내부 관계 대상 → zip 멤버 이름(스펙 §4.2-4). scheme·authority·역슬래시·NUL·조각은 거부하고, 퍼센트 인코딩을 한
    번 풀고, 파트 폴더(/로 시작하면 뿌리)에서 조각을 하나씩 쌓는다. ..가 빈 스택을 꺼내려 하면 그 자리에서 거부한다."""
    if _SCHEME.match(target) or target.startswith("//") or any(c in target for c in "\\\0#"):
        raise ParseError("invalid relationship target", location)
    path = unquote(target)
    if any(c in path for c in "\\\0"):
        raise ParseError("invalid relationship target", location)
    stack = [] if path.startswith("/") else part.split("/")[:-1]
    for piece in path.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not stack:
                raise ParseError("relationship target outside the package", location)
            stack.pop()
        else:
            stack.append(piece)
    return "/".join(stack)


def _uri(prefix: str, prefixes: Prefixes) -> str | None:
    """접두어 → 그 파트에서 선언된 유일한 URI(없거나 둘 이상이면 None: 풀 수 없다)."""
    uris = prefixes.get(prefix, frozenset())
    return next(iter(uris)) if len(uris) == 1 else None


def choose(alternate: ET.Element, prefixes: Prefixes) -> ET.Element | None:
    """mc:AlternateContent에서 읽을 쪽(스펙 §5.4): Requires 접두어가 모두 S의 URI로 풀리는 첫 mc:Choice, 없으면
    mc:Fallback, 그것도 없으면 None. 고른 쪽이 비어도 바꾸지 않는다."""
    fallback = None
    for child in alternate:
        if child.tag == f"{MC}Choice":
            needs = child.get("Requires", "").split()
            if needs and all(_uri(p, prefixes) in READ for p in needs):
                return child
        elif child.tag == f"{MC}Fallback" and fallback is None:
            fallback = child
    return fallback


def ignorable(element: ET.Element, prefixes: Prefixes) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """요소의 mc:Ignorable 가운데 S에 들지 않는 URI들과 mc:ProcessContent의 (URI, 이름 또는 *)들."""
    ignored = frozenset(uri for p in element.get(f"{MC}Ignorable", "").split()
                        if (uri := _uri(p, prefixes)) is not None and uri not in READ)
    process = set()
    for name in element.get(f"{MC}ProcessContent", "").split():
        prefix, _, local = name.partition(":")
        if (uri := _uri(prefix, prefixes)) is not None and local:
            process.add((uri, local))
    return ignored, frozenset(process)


class Package(Archive):
    """열린 OOXML 꾸러미. 생성자가 옛 바이너리·zip·콘텐츠 형식·뿌리 관계(officeDocument)·주 파트 형식을 확인한다.
    main은 주 파트의 zip 멤버 이름이다."""

    def __init__(self, data: bytes, name: str, label: str, main_type: str) -> None:
        if data[:8] == OLE:
            raise ParseError("legacy Word/PowerPoint binary documents are not supported", name)
        super().__init__(data, name, f"not a {label} (zip) package", _limits)
        self._rels: dict[str, dict[str, Rel]] = {}
        self._defaults, self._overrides = self._types()
        self.main = self._main(main_type)

    def _types(self) -> tuple[dict[str, str], dict[str, str]]:
        root = self.xml(CONTENT_TYPES, f"{CT}Types")
        defaults = {d.get("Extension", "").lower(): d.get("ContentType", "") for d in root.iterfind(f"{CT}Default")}
        overrides = {unquote(o.get("PartName", "")).lstrip("/"): o.get("ContentType", "")
                     for o in root.iterfind(f"{CT}Override")}
        return defaults, overrides

    def content_type(self, part: str) -> str | None:
        """Override가 먼저, 없으면 확장자 Default."""
        if part in self._overrides:
            return self._overrides[part]
        return self._defaults.get(part.rpartition(".")[2].lower()) if "." in part else None

    def _main(self, main_type: str) -> str:
        rels = self.rels("")
        if any(rel.type.startswith(STRICT) for rel in rels.values()):
            raise ParseError("Strict OOXML is not supported", self.name)
        main = next((rel for rel in rels.values() if rel.type == OFFICE_DOCUMENT), None)
        if main is None or main.target is None:
            raise ParseError("no officeDocument relationship", f"{self.name}:{rels_member('')}")
        if not self.has(main.target):
            raise ParseError("missing zip member", f"{self.name}:{main.target}")
        if self.content_type(main.target) != main_type:  # 매크로 포함·서식 파일·다른 형식(스펙 §4.2-2)
            raise ParseError("unsupported OOXML document type", f"{self.name}:{main.target}")
        return main.target

    def rels(self, part: str) -> dict[str, Rel]:
        """파트의 관계(Id → Rel, 문서 순서). 관계 멤버가 없으면 빈 사전. Id 중복은 ParseError."""
        if part in self._rels:
            return self._rels[part]
        member = rels_member(part)
        found: dict[str, Rel] = {}
        if self.has(member):
            location = f"{self.name}:{member}"
            for rel in self.xml(member, f"{PR}Relationships").iterfind(f"{PR}Relationship"):
                key = rel.get("Id", "")
                if key in found:
                    raise ParseError(f"duplicate relationship id {key!r}", location)
                external = rel.get("TargetMode") == "External"
                target = None if external else resolve(part, rel.get("Target", ""), location)
                found[key] = Rel(key, rel.get("Type", ""), target)
        self._rels[part] = found
        return found

    def related(self, part: str, kind: str) -> list[str]:
        """파트에서 관계 형식 {R}/{kind}로 가리키는 내부 멤버 가운데 zip에 있는 것(문서 순서)."""
        return [rel.target for rel in self.rels(part).values()
                if rel.type == f"{R}/{kind}" and rel.target is not None and self.has(rel.target)]

    def part(self, member: str, root: str) -> tuple[ET.Element, Prefixes]:
        """XML 파트와 접두어 선언. Strict 이름공간 뿌리는 ParseError("Strict OOXML is not supported")."""
        tree, prefixes = self.tree(member, None)
        if tree.tag.startswith("{" + STRICT):
            raise ParseError("Strict OOXML is not supported", f"{self.name}:{member}")
        if tree.tag != root:
            raise ParseError(f"unexpected root element {tree.tag}", f"{self.name}:{member}")
        return tree, prefixes
