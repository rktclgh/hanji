"""OOXML(DOCX) 합성 문서 쓰기 도구(지어낸 한국어·영어 글자만).

zip은 ZIP_STORED(zlib 판 차이 회피), 고정 시각, 고정 멤버 순서, create_system=3이다. 테스트는 이 파일을 importlib로 읽어
쓰기 도구(p·r·t·document·styles·docx 등)를 쓴다.
"""

import zipfile
from collections.abc import Sequence
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {
    "w": W, "r": R,
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "x": "urn:hanji:test:future",  # S에 없는 이름공간(모르는 Requires·무시 표시)
}
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
WHEN = (1980, 1, 1, 0, 0, 0)
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
REL = {kind: f"{R}/{kind}" for kind in ("officeDocument", "styles", "numbering", "comments", "header", "footer",
                                        "footnotes", "endnotes", "hyperlink")}


def attrs(**values: object) -> str:
    """키워드 → XML 속성(이름의 __는 :로, 값 None은 뺀다)."""
    return "".join(f" {k.replace('__', ':')}={quoteattr(str(v))}" for k, v in values.items() if v is not None)


def xmlns(*prefixes: str) -> str:
    return "".join(f' xmlns:{p}="{NS[p]}"' for p in prefixes)


def t(text: str) -> str:
    return f'<w:t xml:space="preserve">{escape(text)}</w:t>'


def rpr(sz: int | None = None, b: str | None = None, vanish: str | None = None, style: str | None = None) -> str:
    """런 속성: sz는 반 포인트, b·vanish는 w:val 값("" = 값 없이 켬)."""
    parts = [f"<w:rStyle{attrs(w__val=style)}/>" if style else ""]
    for tag, value in (("b", b), ("vanish", vanish)):
        if value is not None:
            parts.append(f"<w:{tag}{attrs(w__val=value or None)}/>")
    if sz is not None:
        parts.append(f"<w:sz{attrs(w__val=sz)}/>")
    joined = "".join(parts)
    return f"<w:rPr>{joined}</w:rPr>" if joined else ""


def r(*items: str, sz: int | None = None, b: str | None = None, vanish: str | None = None,
      style: str | None = None) -> str:
    """런. 꺾쇠로 시작하지 않는 항목은 w:t가 된다."""
    inner = "".join(item if item.startswith("<") else t(item) for item in items)
    return f"<w:r>{rpr(sz, b, vanish, style)}{inner}</w:r>"


def ppr(style: str | None = None, outline: int | str | None = None, num: str | None = None, extra: str = "") -> str:
    parts = [f"<w:pStyle{attrs(w__val=style)}/>" if style else "",
             f'<w:numPr><w:ilvl w:val="0"/><w:numId{attrs(w__val=num)}/></w:numPr>' if num is not None else "",
             f"<w:outlineLvl{attrs(w__val=outline)}/>" if outline is not None else "", extra]
    joined = "".join(parts)
    return f"<w:pPr>{joined}</w:pPr>" if joined else ""


def p(*items: str, style: str | None = None, outline: int | str | None = None, num: str | None = None,
      extra: str = "") -> str:
    """문단. 꺾쇠로 시작하지 않는 항목은 런 하나(글자 하나)가 된다."""
    inner = "".join(item if item.startswith("<") else r(item) for item in items)
    return f"<w:p>{ppr(style, outline, num, extra)}{inner}</w:p>"


def sect() -> str:
    return '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'


def fld(kind: str) -> str:
    """복합 필드 표시 런(begin·separate·end)."""
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def instr(text: str) -> str:
    return f'<w:r><w:instrText xml:space="preserve">{escape(text)}</w:instrText></w:r>'


def document(*items: str, ignorable: str = "w14") -> str:
    """w:document(본문 항목들 + 마지막 구역 속성). 모든 테스트 접두어를 뿌리에서 선언한다."""
    return (f'{DECL}<w:document{xmlns(*NS)} mc:Ignorable="{ignorable}"><w:body>{"".join(items)}{sect()}'
            "</w:body></w:document>")


def style(key: str, name: str | None = None, kind: str = "paragraph", based: str | None = None,
          default: bool = False, ppr_xml: str = "", rpr_xml: str = "") -> str:
    head = f"<w:name{attrs(w__val=name)}/>" if name is not None else ""
    based_xml = f"<w:basedOn{attrs(w__val=based)}/>" if based else ""
    return (f'<w:style w:type="{kind}" w:styleId="{key}"{attrs(w__default="1" if default else None)}>{head}'
            f"{based_xml}{ppr_xml}{rpr_xml}</w:style>")


def styles(*items: str, defaults: str = "") -> str:
    """w:styles. defaults는 docDefaults의 런 속성(rpr() 결과)."""
    return (f'{DECL}<w:styles{xmlns("w")}><w:docDefaults><w:rPrDefault>{defaults}</w:rPrDefault></w:docDefaults>'
            f'{"".join(items)}</w:styles>')


def numbering(*num_ids: str) -> str:
    nums = "".join(f'<w:num w:numId="{n}"><w:abstractNumId w:val="0"/></w:num>' for n in num_ids)
    return f'{DECL}<w:numbering{xmlns("w")}><w:abstractNum w:abstractNumId="0"/>{nums}</w:numbering>'


def comments(*paras: str) -> str:
    inner = "".join(f'<w:comment w:id="{k}">{p}</w:comment>' for k, p in enumerate(paras))
    return f"{DECL}<w:comments{xmlns('w')}>{inner}</w:comments>"


def rels(*items: tuple[str, ...]) -> str:
    """(Id, Type, Target[, TargetMode]) 목록 → 관계 XML."""
    body = "".join(f"<Relationship{attrs(Id=i[0], Type=i[1], Target=i[2], TargetMode=i[3] if len(i) > 3 else None)}/>"
                   for i in items)
    return f'{DECL}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}' \
           "</Relationships>"


def types(overrides: Sequence[tuple[str, str]], defaults: Sequence[tuple[str, str]] = ()) -> str:
    base = [("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml"),
            *defaults]
    body = "".join(f"<Default{attrs(Extension=e, ContentType=c)}/>" for e, c in base)
    body += "".join(f"<Override{attrs(PartName=n, ContentType=c)}/>" for n, c in overrides)
    return f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{body}</Types>'


def zip_bytes(members: Sequence[tuple[str, bytes | str]], deflated: bool = False) -> bytes:
    """고정 시각·순서·create_system의 zip(기본 무압축, deflated는 압축 폭탄 시험용)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            info = zipfile.ZipInfo(name, WHEN)
            info.compress_type = zipfile.ZIP_DEFLATED if deflated else zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            z.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


def docx(document_xml: str, styles_xml: str | None = None, numbering_xml: str | None = None,
         comments_xml: str | None = None, main_type: str = DOCX_MAIN, extra: Sequence[tuple[str, bytes | str]] = (),
         doc_rels: Sequence[tuple[str, ...]] = (), root_rels: Sequence[tuple[str, ...]] | None = None,
         deflated: bool = False) -> bytes:
    """DOCX 바이트. 있는 파트만 관계와 Override를 단다. root_rels가 있으면 뿌리 관계를 그것으로 바꾼다."""
    parts: list[tuple[str, str, str, str]] = []  # (멤버, 내용, 관계 형식, 콘텐츠 형식)
    for member, xml, kind, ctype in (
            ("word/styles.xml", styles_xml, "styles", "wordprocessingml.styles+xml"),
            ("word/numbering.xml", numbering_xml, "numbering", "wordprocessingml.numbering+xml"),
            ("word/comments.xml", comments_xml, "comments", "wordprocessingml.comments+xml")):
        if xml is not None:
            parts.append((member, xml, kind, f"application/vnd.openxmlformats-officedocument.{ctype}"))
    overrides = [("/word/document.xml", main_type)] + [("/" + m, c) for m, _, _, c in parts]
    links = [(f"rId{k + 1}", REL[kind], member.removeprefix("word/")) for k, (member, _, kind, _) in enumerate(parts)]
    roots = root_rels if root_rels is not None else [("rId1", REL["officeDocument"], "word/document.xml")]
    members: list[tuple[str, bytes | str]] = [
        ("[Content_Types].xml", types(overrides)), ("_rels/.rels", rels(*roots)), ("word/document.xml", document_xml),
        ("word/_rels/document.xml.rels", rels(*links, *doc_rels))]
    members += [(member, xml) for member, xml, _, _ in parts]
    return zip_bytes([*members, *extra], deflated)
