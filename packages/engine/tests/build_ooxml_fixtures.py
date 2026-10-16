"""OOXML(DOCX·PPTX) 합성 문서 쓰기 도구와 골든 예제 생성기(지어낸 한국어·영어 글자만).

    uv run python packages/engine/tests/build_ooxml_fixtures.py          # fixtures/ooxml/ 재생성
    uv run python packages/engine/tests/build_ooxml_fixtures.py --check  # 최신인지 확인(바이트 비교)

zip은 ZIP_STORED(zlib 판 차이 회피), 고정 시각, 고정 멤버 순서, create_system=3이다. 테스트는 이 파일을 importlib로 읽어
쓰기 도구(DOCX는 p·r·t·document·styles·docx 등, PPTX는 sp·para·grp·slide·pptx 등)를 쓴다. 그림 골든은 HWPX와 같은
화소 비교 대체를 쓴다: 트리는 그림 자산 키에서 나온 값을 가려 비교하고(portable), 그림은 디코드한 화소 정보(pixels.json)를
바이트로 비교한다(OS마다 PNG 압축 바이트가 달라도 같다).
"""

import argparse
import hashlib
import json
import sys
import zipfile
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from PIL import Image

from hanji.core import build_tree
from hanji.formats.ooxml.docx.parser import DocxParser
from hanji_contracts import DocumentTree, ProcessingHistory, SourceInfo

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
# 개체 쓰기 도구가 그 요소 자리에서 선언하는 접두어(문서 뿌리 선언을 바꾸지 않아 OX-A 골든 입력 바이트가 그대로다)
LOCAL = {
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "o": "urn:schemas-microsoft-com:office:office",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "dsp": "http://schemas.microsoft.com/office/drawing/2008/diagram",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
EMU = 914400  # 1인치
PIC_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
WHEN = (1980, 1, 1, 0, 0, 0)
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
REL = {kind: f"{R}/{kind}" for kind in ("officeDocument", "styles", "numbering", "comments", "header", "footer",
                                        "footnotes", "endnotes", "hyperlink")}


def attrs(**values: object) -> str:
    """키워드 → XML 속성(이름의 __는 :로, 값 None은 뺀다)."""
    return "".join(f" {k.replace('__', ':')}={quoteattr(str(v))}" for k, v in values.items() if v is not None)


def xmlns(*prefixes: str) -> str:
    return "".join(f' xmlns:{p}="{NS[p] if p in NS else LOCAL[p]}"' for p in prefixes)


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
      extra: str = "", process: str | None = None) -> str:
    """문단. 꺾쇠로 시작하지 않는 항목은 런 하나(글자 하나)가 된다. process는 mc:ProcessContent 값."""
    inner = "".join(item if item.startswith("<") else r(item) for item in items)
    return f"<w:p{attrs(mc__ProcessContent=process)}>{ppr(style, outline, num, extra)}{inner}</w:p>"


def tc(*items: str, span: int | str | None = None, merge: str | None = None) -> str:
    """표 칸. 꺾쇠로 시작하지 않는 항목은 문단 하나가 된다(항목이 없으면 빈 문단). merge는 vMerge@w:val("" = 값 없음)."""
    props = (f"<w:gridSpan{attrs(w__val=span)}/>" if span is not None else "") + (
        f"<w:vMerge{attrs(w__val=merge or None)}/>" if merge is not None else "")
    inner = "".join(item if item.startswith("<") else p(item) for item in items) or "<w:p/>"
    return f"<w:tc>{f'<w:tcPr>{props}</w:tcPr>' if props else ''}{inner}</w:tc>"


def tr(*cells: str, header: bool = False, before: int | None = None, after: int | None = None) -> str:
    props = (f"<w:gridBefore{attrs(w__val=before)}/>" if before is not None else "") + (
        f"<w:gridAfter{attrs(w__val=after)}/>" if after is not None else "") + ("<w:tblHeader/>" if header else "")
    return f"<w:tr>{f'<w:trPr>{props}</w:trPr>' if props else ''}{''.join(cells)}</w:tr>"


def tbl(*rows: str, cols: int | None = None) -> str:
    """표. cols가 있으면 선언 격자 열 수(없으면 빈 격자: 열 수는 행에서 나온다)."""
    grid = '<w:gridCol w:w="2000"/>' * (cols or 0)
    return f'<w:tbl><w:tblPr/><w:tblGrid>{grid}</w:tblGrid>{"".join(rows)}</w:tbl>'


def sect() -> str:
    return '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'


def fld(kind: str) -> str:
    """복합 필드 표시 런(begin·separate·end)."""
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def instr(text: str) -> str:
    return f'<w:r><w:instrText xml:space="preserve">{escape(text)}</w:instrText></w:r>'


def document(*items: str, ignorable: str = "w14", final: str | None = None) -> str:
    """w:document(본문 항목들 + 마지막 구역 속성, final이 있으면 그것). 모든 테스트 접두어를 뿌리에서 선언한다."""
    return (f'{DECL}<w:document{xmlns(*NS)} mc:Ignorable="{ignorable}"><w:body>{"".join(items)}{final or sect()}'
            "</w:body></w:document>")


def margin(root: str, *items: str) -> str:
    """머리말(root "hdr")·꼬리말("ftr") 파트. 꺾쇠로 시작하지 않는 항목은 문단 하나."""
    inner = "".join(item if item.startswith("<") else p(item) for item in items)
    return f"{DECL}<w:{root}{xmlns(*NS)}>{inner}</w:{root}>"


def notes(kind: str, *entries: tuple[str, str | None, str]) -> str:
    """각주(kind "footnotes")·미주("endnotes") 파트. 항목은 (w:id, w:type 또는 None, 안 XML)."""
    tag = kind[:-1]
    inner = "".join(f"<w:{tag}{attrs(w__type=kind_, w__id=key)}>{body}</w:{tag}>" for key, kind_, body in entries)
    return f"{DECL}<w:{kind}{xmlns(*NS)}>{inner}</w:{kind}>"


def note_ref(kind: str, key: str, vanish: bool = False) -> str:
    """각주(kind "footnote")·미주("endnote") 참조 런."""
    return f'<w:r>{rpr(vanish="" if vanish else None)}<w:{kind}Reference w:id="{key}"/></w:r>'


def sect_refs(*refs: tuple[str, str, str]) -> str:
    """머리말·꼬리말 참조를 단 구역 속성. 항목은 ("header"·"footer", w:type, 관계 id)."""
    inner = "".join(f'<w:{kind}Reference w:type="{kind_}" r:id="{rid}"/>' for kind, kind_, rid in refs)
    return f"<w:sectPr>{inner}</w:sectPr>"


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


def pic(embed: str | None = None, link: str | None = None, cx: int | None = None, svg: str | None = None) -> str:
    """pic:pic(a:blip@r:embed·r:link). cx가 있으면 그림 자신의 표시 너비(a:xfrm), svg는 asvg:svgBlip 관계 id."""
    ext = (f'<a:extLst><a:ext uri="{{96DAC541-7B7A-43D3-8B79-37D633B846F1}}"><asvg:svgBlip{xmlns("asvg")}'
           f'{attrs(r__embed=svg)}/></a:ext></a:extLst>') if svg else ""
    xfrm = f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cx}"/></a:xfrm>' if cx else ""
    return (f'<pic:pic{xmlns("pic")}><pic:nvPicPr><pic:cNvPr id="1" name="p"/><pic:cNvPicPr/></pic:nvPicPr>'
            f"<pic:blipFill><a:blip{attrs(r__embed=embed, r__link=link)}>{ext}</a:blip></pic:blipFill>"
            f"<pic:spPr>{xfrm}</pic:spPr></pic:pic>")


def drawing(*graphic: str, cx: int = EMU, anchor: bool = False, hidden: bool = False, uri: str = PIC_URI) -> str:
    """w:drawing 런: wp:inline(anchor면 wp:anchor)/a:graphic/a:graphicData 아래 graphic들."""
    frame = "anchor" if anchor else "inline"
    return (f'<w:r><w:drawing><wp:{frame}><wp:extent cx="{cx}" cy="{cx}"/>'
            f'<wp:docPr id="1" name="d"{attrs(hidden="1" if hidden else None)}/><a:graphic>'
            f'<a:graphicData uri="{uri}">{"".join(graphic)}</a:graphicData></a:graphic></wp:{frame}></w:drawing></w:r>')


def wsp(*items: str) -> str:
    """DrawingML 글상자 도형(wps:wsp/wps:txbx/w:txbxContent). 꺾쇠로 시작하지 않는 항목은 문단 하나."""
    inner = "".join(item if item.startswith("<") else p(item) for item in items)
    return f"<wps:wsp><wps:spPr/><wps:txbx><w:txbxContent>{inner}</w:txbxContent></wps:txbx><wps:bodyPr/></wps:wsp>"


def wgp(*children: str) -> str:
    return f'<wpg:wgp{xmlns("wpg")}><wpg:cNvGrpSpPr/><wpg:grpSpPr/>{"".join(children)}</wpg:wgp>'


def vshape(image: str | None = None, *items: str) -> str:
    """VML 도형: image는 v:imagedata@r:id, items가 있으면 v:textbox 글상자(꺾쇠로 시작하지 않으면 문단)."""
    data = f"<v:imagedata{attrs(r__id=image)}/>" if image else ""
    inner = "".join(item if item.startswith("<") else p(item) for item in items)
    box = f"<v:textbox><w:txbxContent>{inner}</w:txbxContent></v:textbox>" if items else ""
    return f'<v:shape style="width:20pt;height:20pt">{data}{box}</v:shape>'


def vpict(*shapes: str, ole: bool = False) -> str:
    """w:pict(ole면 w:object와 o:OLEObject) 런."""
    if ole:
        return f'<w:r><w:object>{"".join(shapes)}<o:OLEObject{xmlns("o")} ProgID="Excel.Sheet.12"/></w:object></w:r>'
    return f'<w:r><w:pict>{"".join(shapes)}</w:pict></w:r>'


def a_p(*texts: str) -> str:
    """DrawingML 문단(a:p): 글자마다 런, 빈 문자열은 a:br."""
    return "<a:p>" + "".join(f"<a:r><a:t>{escape(x)}</a:t></a:r>" if x else "<a:br/>" for x in texts) + "</a:p>"


def chart_ref(rid: str) -> str:
    return f'<c:chart{xmlns("c")}{attrs(r__id=rid)}/>'


def chart_part(*titles: str, cache: Sequence[str] = ()) -> str:
    """차트 파트: 풍부한 글자 제목들(a:p 하나씩)과 캐시 문자열 값들(c:strCache), 제목·차트마다 빈 a:p만 든 c:txPr."""
    empty = '<c:txPr><a:bodyPr/><a:p><a:pPr/><a:endParaRPr lang="ko-KR"/></a:p></c:txPr>'
    rich = "".join(f"<c:title><c:tx><c:rich><a:bodyPr/>{a_p(x)}</c:rich></c:tx>{empty}</c:title>" for x in titles)
    points = "".join(f'<c:pt idx="{k}"><c:v>{escape(v)}</c:v></c:pt>' for k, v in enumerate(cache))
    return (f'{DECL}<c:chartSpace{xmlns("c", "a", "r")}><c:chart>{rich}<c:plotArea><c:barChart><c:ser><c:cat><c:strRef>'
            f"<c:f>Sheet1!$A$2</c:f><c:strCache>{points}</c:strCache></c:strRef></c:cat></c:ser></c:barChart>"
            f"</c:plotArea></c:chart>{empty}</c:chartSpace>")


def smartart_ref(dm: str) -> str:
    return f'<dgm:relIds{xmlns("dgm")}{attrs(r__dm=dm, r__lo="rIdLo", r__qs="rIdQs", r__cs="rIdCs")}/>'


def smartart_data(*texts: str, drawn: str | None = None) -> str:
    """SmartArt 데이터 파트: 점마다 dgm:t 글자, drawn은 그려진 파트 관계 id(데이터 파트 확장, 호스트 관계로 푼다)."""
    points = "".join(f'<dgm:pt modelId="{k + 1}"><dgm:t><a:bodyPr/>{a_p(x)}</dgm:t></dgm:pt>'
                     for k, x in enumerate(texts))
    ext = (f'<dgm:extLst><a:ext uri="http://schemas.microsoft.com/office/drawing/2008/diagram"><dsp:dataModelExt'
           f'{xmlns("dsp")} relId="{drawn}" minVer="http://schemas.openxmlformats.org/drawingml/2006/diagram"/></a:ext>'
           "</dgm:extLst>") if drawn else ""
    return f'{DECL}<dgm:dataModel{xmlns("dgm", "a")}><dgm:ptLst>{points}</dgm:ptLst>{ext}</dgm:dataModel>'


def smartart_drawn(*shapes: Sequence[str]) -> str:
    """SmartArt 그려진 파트: 도형마다 dsp:txBody의 a:p들(항목마다 하나)."""
    body = "".join(f'<dsp:sp modelId="{k}"><dsp:txBody><a:bodyPr/>{"".join(a_p(x) for x in shape)}'
                   "</dsp:txBody></dsp:sp>" for k, shape in enumerate(shapes))
    return f'{DECL}<dsp:drawing{xmlns("dsp", "a")}><dsp:spTree>{body}</dsp:spTree></dsp:drawing>'


# ---- PPTX 쓰기 도구 ----
PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
PML = "application/vnd.openxmlformats-officedocument.presentationml"
SLIDE_NS = xmlns("p", "a", "r", "mc", "m") + ' mc:Ignorable="x"' + xmlns("x")
PREL = {kind: f"{R}/{kind}" for kind in ("slide", "slideLayout", "slideMaster", "notesSlide", "comments", "image",
                                         "chart", "diagramData")}


def a_r(text: str, sz: int | None = None, b: str | None = None, fld: str | None = None) -> str:
    """DrawingML 런(sz는 1/100pt, b는 "1"·"0"). fld가 있으면 그 종류의 필드(a:fld)."""
    pr = f'<a:rPr lang="ko-KR"{attrs(sz=sz, b=b)}/>'
    if fld is not None:
        key = "{00000000-0000-0000-0000-000000000001}"
        return f'<a:fld id="{key}" type="{fld}">{pr}<a:t>{escape(text)}</a:t></a:fld>'
    return f"<a:r>{pr}<a:t>{escape(text)}</a:t></a:r>"


def para(*items: str, lvl: int | None = None, bullet: str | None = None, def_sz: int | None = None,
         def_b: str | None = None) -> str:
    """a:p. 꺾쇠로 시작하지 않는 항목은 런 하나, 빈 문자열은 a:br. bullet은 "char"·"auto"·"none"(a:pPr 글머리),
    def_sz·def_b는 문단 기본 런 속성(a:pPr/a:defRPr)."""
    bu = {"char": '<a:buChar char="•"/>', "auto": '<a:buAutoNum type="arabicPeriod"/>', "none": "<a:buNone/>",
          None: ""}[bullet]
    default = f"<a:defRPr{attrs(sz=def_sz, b=def_b)}/>" if def_sz is not None or def_b is not None else ""
    ppr = f"<a:pPr{attrs(lvl=lvl)}>{bu}{default}</a:pPr>" if bu or default or lvl is not None else ""
    inner = "".join(x if x.startswith("<") else a_r(x) if x else "<a:br/>" for x in items)
    return f"<a:p>{ppr}{inner}</a:p>"


def xfrm(y: int | None, x: int = 0, cx: int = EMU, cy: int = EMU // 2, tag: str = "a:xfrm") -> str:
    return "" if y is None else f'<{tag}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></{tag}>'


def nv(kind: str, name: str, hidden: bool, ph: str | None = None, extra: str = "") -> str:
    """p:nv*Pr(kind "Sp"·"GrpSp"·"Pic"·"GraphicFrame"·"CxnSp"). ph는 자리 표시자 종류("" = type 없음)."""
    holder = "" if ph is None else f"<p:ph{attrs(type=ph or None)}/>"
    return (f'<p:nv{kind}Pr><p:cNvPr id="2" name="{name}"{attrs(hidden="1" if hidden else None)}/><p:cNv{kind}Pr/>'
            f"<p:nvPr>{holder}{extra}</p:nvPr></p:nv{kind}Pr>")


def sp(*paras: str, ph: str | None = None, hidden: bool = False, y: int | None = None, lst: str = "",
       tag: str = "sp") -> str:
    """도형(tag "sp"·"cxnSp") 글 몸통. 꺾쇠로 시작하지 않는 항목은 문단 하나. y는 위쪽 좌표(a:off@y, EMU),
    lst는 도형 목록 스타일(a:lstStyle 안 XML)."""
    body = "".join(x if x.startswith("<") else para(x) for x in paras)
    kind = "Sp" if tag == "sp" else "CxnSp"
    return (f"<p:{tag}>{nv(kind, '도형', hidden, ph)}<p:spPr>{xfrm(y)}</p:spPr>"
            f"<p:txBody><a:bodyPr/><a:lstStyle>{lst}</a:lstStyle>{body}</p:txBody></p:{tag}>")


def grp(*children: str, hidden: bool = False, off: int = 0, ext: int = EMU, ch_off: int = 0,
        ch_ext: int | None = EMU) -> str:
    """그룹(세로 변환만 쓴다: off·ext·chOff·chExt의 y·cy). ch_ext가 None이면 a:chExt를 뺀다."""
    child = f'<a:chExt cx="{EMU}" cy="{ch_ext}"/>' if ch_ext is not None else ""
    frame = (f'<a:xfrm><a:off x="0" y="{off}"/><a:ext cx="{EMU}" cy="{ext}"/><a:chOff x="0" y="{ch_off}"/>{child}'
             "</a:xfrm>")
    return f"<p:grpSp>{nv('GrpSp', '그룹', hidden)}<p:grpSpPr>{frame}</p:grpSpPr>{''.join(children)}</p:grpSp>"


def slide(*shapes: str, show: str | None = None) -> str:
    """p:sld(도형 트리). show가 "0"이면 숨긴 슬라이드."""
    return (f"{DECL}<p:sld{SLIDE_NS}{attrs(show=show)}><p:cSld><p:spTree>{nv('GrpSp', '', False)}<p:grpSpPr/>"
            f"{''.join(shapes)}</p:spTree></p:cSld></p:sld>")


def levels(sz: int | None = None, b: str | None = None, lvl: int = 1) -> str:
    """목록 스타일 수준 하나(a:lvlNpPr/a:defRPr)."""
    return f"<a:lvl{lvl}pPr><a:defRPr{attrs(sz=sz, b=b)}/></a:lvl{lvl}pPr>"


def pptx(*slides: str, slide_rels: Sequence[Sequence[tuple[str, ...]]] = (),
         extra: Sequence[tuple[str, bytes | str]] = (), master_shapes: str = "", layout_shapes: str = "",
         other_style: str = "", default_style: str = "", main_type: str = PPTX_MAIN,
         pres_rels: Sequence[tuple[str, ...]] = ()) -> bytes:
    """PPTX 바이트: 프레젠테이션, 슬라이드들(slide_rels는 슬라이드마다 더할 관계), 레이아웃·마스터 하나씩(그 도형과 마스터
    p:otherStyle 수준들), 기본 글 스타일(p:defaultTextStyle 수준들)."""
    ids = "".join(f'<p:sldId id="{256 + k}" r:id="rIdS{k + 1}"/>' for k in range(len(slides)))
    pres = (f'{DECL}<p:presentation{xmlns("p", "a", "r")}><p:sldMasterIdLst><p:sldMasterId id="2147483648" '
            f'r:id="rIdM"/></p:sldMasterIdLst><p:sldIdLst>{ids}</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/>'
            f"<p:defaultTextStyle>{default_style}</p:defaultTextStyle></p:presentation>")
    tree = f"<p:cSld><p:spTree>{nv('GrpSp', '', False)}<p:grpSpPr/>{{}}</p:spTree></p:cSld>"
    master = (f'{DECL}<p:sldMaster{xmlns("p", "a", "r")}>{tree.format(master_shapes)}<p:txStyles><p:titleStyle/>'
              f"<p:bodyStyle/><p:otherStyle>{other_style}</p:otherStyle></p:txStyles></p:sldMaster>")
    layout = f'{DECL}<p:sldLayout{xmlns("p", "a", "r")}>{tree.format(layout_shapes)}</p:sldLayout>'
    members: list[tuple[str, bytes | str]] = [
        ("[Content_Types].xml", types([("/ppt/presentation.xml", main_type),
                                       *((f"/ppt/slides/slide{k + 1}.xml", f"{PML}.slide+xml")
                                         for k in range(len(slides)))])),
        ("_rels/.rels", rels(("rId1", REL["officeDocument"], "ppt/presentation.xml"))),
        ("ppt/presentation.xml", pres),
        ("ppt/_rels/presentation.xml.rels", rels(("rIdM", PREL["slideMaster"], "slideMasters/slideMaster1.xml"),
                                                 *((f"rIdS{k + 1}", PREL["slide"], f"slides/slide{k + 1}.xml")
                                                   for k in range(len(slides))), *pres_rels)),
        ("ppt/slideMasters/slideMaster1.xml", master),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", rels(("rIdL", PREL["slideLayout"],
                                                               "../slideLayouts/slideLayout1.xml"))),
        ("ppt/slideLayouts/slideLayout1.xml", layout),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", rels(("rIdM", PREL["slideMaster"],
                                                               "../slideMasters/slideMaster1.xml")))]
    for k, xml in enumerate(slides):
        more = slide_rels[k] if k < len(slide_rels) else ()
        members += [(f"ppt/slides/slide{k + 1}.xml", xml),
                    (f"ppt/slides/_rels/slide{k + 1}.xml.rels",
                     rels(("rIdL", PREL["slideLayout"], "../slideLayouts/slideLayout1.xml"), *more))]
    return zip_bytes([*members, *extra])


# 그림 원본: HWPX 골든과 같은 고정 바이트(6×6 회색 PNG, 4×4 반투명 RGBA PNG, 8×8 CMYK JPEG). 가짜 EMF는 머리만 있다
GRAY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000006000000060800000000c5a7b094000000134944415478da638c626060606060622048"
    "01000f900066ffa59b760000000049454e44ae426082")
RGBA_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000004000000040806000000a9f19e7e000000154944415478da633c2127d7c080049818d0"
    "006101006506018c5f1704320000000049454e44ae426082")
CMYK_JPEG = bytes.fromhex(
    "ffd8ffee000e41646f626500640000000000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d"
    "1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc000140800080008044311004d11005911004b11"
    "00ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b510000201030302040305050404000001"
    "7d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536"
    "3738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2"
    "a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9"
    "faffda000e0443004d0059004b00003f00f7fa4af9febdfebfffd9")
FAKE_EMF = bytes.fromhex("01000000") + bytes(36) + b" EMF" + bytes(40)


def media(**files: tuple[str, bytes]) -> dict[str, list[tuple[str, ...]] | list[tuple[str, bytes | str]]]:
    """관계 id → (word/ 아래 멤버, 바이트)들 → docx()의 doc_rels·extra 인자."""
    return {"doc_rels": [(rid, f"{R}/image", member) for rid, (member, _) in files.items()],
            "extra": [(f"word/{member}", data) for member, data in files.values()]}


# ---- 골든 예제 ----

def _alt(choice: str, fallback: str, requires: str = "wps") -> str:
    return (f'<mc:AlternateContent><mc:Choice Requires="{requires}">{choice}</mc:Choice>'
            f"<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent>")


def _sym(char: str) -> str:
    return f'<w:r><w:sym w:font="Wingdings" w:char="{char}"/></w:r>'


OUTLINE = '<w:pPr><w:outlineLvl w:val="{}"/></w:pPr>'
BASIC_STYLES = styles(
    style("Normal", "Normal", default=True, rpr_xml=rpr(sz=20)),
    style("Heading1", "heading 1", based="Normal", ppr_xml=OUTLINE.format(0), rpr_xml=rpr(sz=32, b="")),
    style("Heading2", "heading 2", based="Heading1", ppr_xml=OUTLINE.format(1)),
    style("MyHeading", "my heading", based="Heading2"),  # 개요 수준을 사슬에서 이어받는다
    style("CycA", "heading 3", based="CycB"), style("CycB", "cycle b", based="CycA"),  # 순환은 끊고 이름으로
    style("ListPara", "list paragraph", ppr_xml='<w:pPr><w:numPr><w:numId w:val="2"/></w:numPr></w:pPr>'),
    style("Hidden", "hidden text", kind="character", rpr_xml=rpr(vanish="")))
BASIC = document(
    p("2026년 지어낸 사업 계획", style="Heading1"),
    p("본 문서는 합성 예제로 만든 글이다."),
    p(),
    p("1. 추진 배경", style="MyHeading"),
    p(r("구분", "<w:tab/>", "내용", "<w:br/>", "둘째 줄 A", "<w:noBreakHyphen/>", "B 기호 "), _sym("F0E0")),
    p("첫째 번호 항목", num="1"),
    p("스타일 목록 항목", style="ListPara"),
    p("아주 깊은 개요", outline=7),
    p(r("총액 1,234.5억원, "), '<w:hyperlink r:id="rId9">' + r("증가율 12.3%") + "</w:hyperlink>",
      "<w:sdt><w:sdtPr/><w:sdtContent>" + r(", 면적 84㎡") + "</w:sdtContent></w:sdt>",
      "<w:smartTag>" + r(", 변동 △0.5") + "</w:smartTag>", r(", GDP 대비 2.1%p(OECD 평균)")),
    p("순환 스타일 제목", style="CycA", extra=sect()),
    p(r("모르는 ", "<x:future><w:t>자식 글</w:t></x:future>", " 끝")),
    p(r("보이는 글 "), r("숨긴 글", vanish=""), r("스타일 숨김", style="Hidden"),
      f"<w:r><w:rPr><w:specVanish/></w:rPr>{t('표시 기호 글')}</w:r>"),
    p("<w:ins>" + r("넣은 글 ") + "</w:ins>", "<w:del><w:r><w:delText>지운 글</w:delText></w:r></w:del>",
      "<w:moveFrom>" + r("옮겨 간 글") + "</w:moveFrom>", "<w:moveTo>" + r("옮겨 온 글") + "</w:moveTo>"),
    p(r("수식 "), "<m:oMath><m:r><m:t>x</m:t></m:r><m:r><m:t>=</m:t></m:r><m:f><m:num><m:r><m:t>1</m:t></m:r>"
                  "</m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f></m:oMath>"),
    p("2. 다음 절", style="Heading1"),
    p("끝 문단이다."))
HEAD_STYLES = styles(style("Normal", "Normal", default=True, rpr_xml=rpr(sz=20)))
BODY = "지어낸 본문 문단은 크기 규칙의 본문 크기를 정할 만큼 길게 쓴다"
HEADINGS = document(
    p(r("지어낸 큰 제목", sz=36)), p(BODY), p(r("중간 제목", sz=28)), p(BODY + " 둘째"),
    p(r("굵은 소제목", sz=22, b="")), p(r("큰 글 첫 줄", "<w:br/>", "둘째 줄", "<w:br/>", "셋째 줄", sz=28)),
    p(r("섞인 ", sz=28), r("런 제목", sz=28), r("작게", sz=20)), p(BODY + " 셋째"))
TABLE_STYLES = styles(style("Heading1", "heading 1", ppr_xml=OUTLINE.format(0)))
TABLE = document(
    p("표 예제 문단"),
    tbl(tr(tc("구분"), tc("2025년"), tc("2026년"), header=True),
        tr(tc("지역", merge="restart"), tc("100억원"), tc("120억원")),
        tr(tc(merge=""), tc("△5.0%"), tc("+3.2%")),
        tr(tc("이어진 칸 글", merge="continue"), tc("84㎡"), tc("90㎡")), cols=3),
    tbl(tr(tc("선언 격자보다 넓은 칸", span=3)), tr(tc("가"), tc("나"), tc("다")),
        tr(tc("짝 없는 이음", merge="continue"), tc("라"), tc("마")),
        tr(tc("사슬 시작", merge="restart"), tc("바"), tc("사")),
        tr(tc("사슬 끝"), tc("아"), tc("자")),
        tr(tc("끝난 뒤 이음", merge="continue"), tc("차"), tc("카")), cols=2),
    tbl(tr(tc("빈자리 앞 시작", merge="restart"), tc("하나")), tr(tc("둘"), before=1),
        tr(tc("빈자리 뒤 이음", merge="continue"), tc("셋")), tr(tc("넷"), after=1), cols=2),
    tbl(tr('<w:tc><w:tcPr><w:hMerge w:val="restart"/></w:tcPr>' + p("옛 가로 병합") + "</w:tc>", tc("옆 칸")),
        tr(tc("아래 칸"), tc("끝 칸"))),
    tbl(tr(tc(p("상자 안 제목", style="Heading1"), p("상자 안 문단"),
              tbl(tr(tc("안 가"), tc("안 나")), tr(tc("안 다"), tc("안 라"))))), cols=1),
    tbl(tr(tc(p("바깥 칸"), tbl(tr(tc("안 1"), tc("안 2")), tr(tc("안 3"), tc("안 4")))), tc(tbl(tr(tc("작은 상자"))))),
        tr(tc("아래 1"), tc("아래 2"))),
    tbl(tr(tc("띠 1"), tc("띠 2"), tc("띠 3"))),
    tbl(tr(tc("아주 넓은 칸", span=10**9)), tr(tc("큰 값 아래"))),
    p("표 뒤 문단"))
FIELDS = document(
    p(fld("begin"), instr(' TOC \\o "1-3" \\h '), fld("separate"), r("1. 첫째 절", "<w:tab/>", "1")),
    p(r("2. 둘째 절", "<w:tab/>"), fld("begin"), instr(" PAGEREF _Toc1 \\h "), fld("separate"), r("3"), fld("end")),
    p(r("3. 셋째 절", "<w:tab/>", "5"), fld("end")),
    p(r("앞 "), fld("begin"), instr(" IF "), fld("begin"), instr(" REF 책갈피 "), fld("separate"), r("안쪽 결과"),
      fld("end"), instr(' = 1 "참" "거짓" '), fld("separate"), r("참"), fld("end"), r(" 뒤")),
    p(instr("필드 밖 명령 글자")),
    p(r("쪽 수 "), '<w:fldSimple w:instr=" NUMPAGES ">' + r("12") + "</w:fldSimple>"),
    p(_alt(r("고른 쪽 글"), r("버린 쪽 글"))),
    p(_alt(r("모르는 쪽 글"), r("대체 쪽 글"), requires="x")),
    p(_alt("", r("빈 Choice라 버린 글"))),
    p(r("무시 표시 "), "<x:future>" + r("무시한 글") + "</x:future>", "<x:box>" + r("벗긴 글") + "</x:box>",
      process="x:box"),
    p(r("끝나지 않은 "), fld("begin"), instr(" HYPERLINK 끝 ")),
    ignorable="w14 x")
FIGURE_MEDIA = media(rId11=("media/image1.png", RGBA_PNG), rId12=("media/image2.jpeg", CMYK_JPEG),
                     rId13=("media/image3.emf", FAKE_EMF), rId15=("media/image5.png", GRAY_PNG),
                     rId14=("media/image4.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'))
FIGURE_RELS = [("rId16", f"{R}/image", "http://example.invalid/linked.png", "External"),
               ("rId17", f"{R}/image", "media/missing.png")]
FIGURES = document(
    p("그림 예제 문단이다."),
    p(r("그림 1. 지어낸 막대 그림 "), drawing(pic("rId11"), cx=EMU // 24)),
    p(drawing(pic("rId12"), anchor=True, cx=EMU // 12)),
    p(drawing(pic("rId13"))),
    p(drawing(pic(link="rId16"))),
    p(drawing(pic("rId17"))),
    p(drawing(pic("rId99"))),
    p(drawing(pic("rId15", svg="rId14"), cx=EMU // 16)),
    p(vpict(vshape("rId15"))),
    p(drawing(pic("rId11"), cx=EMU // 24)),
    tbl(tr(tc(p(r("칸 속 그림"), drawing(pic("rId15")))), tc("옆 칸")), tr(tc("아래 칸"), tc("끝 칸"))),
    p("그림 뒤 문단이다."))


def _alt_box(new: str, old: str) -> str:
    """새 형식(wps) 글상자와 옛 형식(VML) 글상자의 mc:AlternateContent 짝 런."""
    return ('<w:r><mc:AlternateContent><mc:Choice Requires="wps">' + drawing(wsp(new))[5:-6]
            + "</mc:Choice><mc:Fallback>" + vpict(vshape(None, old))[5:-6]
            + "</mc:Fallback></mc:AlternateContent></w:r>")


NOTE_PARTS = [
    ("rIdH1", "header", "header1.xml", margin("hdr", "지어낸 머리말",
                                              p(r("머리말 둘째 줄"), _alt_box("머리말 글상자", "머리말 옛 글상자")))),
    ("rIdH2", "header", "header2.xml", margin("hdr", "첫 쪽 머리말")),
    ("rIdH3", "header", "header3.xml", margin("hdr", "쓰지 않는 머리말")),
    ("rIdF1", "footer", "footer1.xml", margin("ftr", p(r("쪽 "), fld("begin"), instr(" PAGE "), fld("separate"), r("1"),
                                                      fld("end")), p(drawing(pic("rId1"))))),
    ("rIdFN", "footnotes", "footnotes.xml", notes(
        "footnotes", ("-1", "separator", p("<w:r><w:separator/></w:r>")),
        ("0", "continuationSeparator", p("<w:r><w:continuationSeparator/></w:r>")),
        ("1", "continuationNotice", p("다음 쪽에 계속")),
        ("2", None, p('<w:r><w:footnoteRef/></w:r>', r(" 첫째 각주 글"))), ("3", None, p("칸 속 각주")),
        ("4", None, p("참조 없는 각주")))),
    ("rIdEN", "endnotes", "endnotes.xml", notes(
        "endnotes", ("-1", "separator", p("<w:r><w:separator/></w:r>")),
        ("1", None, p('<w:r><w:endnoteRef/></w:r>', r(" 첫째 미주 글"))))),
]
NOTE_SECT = sect_refs(("header", "default", "rIdH1"), ("header", "first", "rIdH2"), ("footer", "default", "rIdF1"))
NOTES = document(
    p("머리말을 둔 첫 문단이다."),
    p(r("각주가 달린 문단"), note_ref("footnote", "2"), r(" 이어지는 글")),
    tbl(tr(tc(p(r("각주 든 칸"), note_ref("footnote", "3"))), tc("둘째 칸")),
        tr(tc("셋째 칸"), tc(p(drawing(wsp("칸 속 글상자")))))),
    p(drawing(wsp("본문 글상자 첫째", "본문 글상자 둘째"))),
    p(_alt_box("짝 글상자 새 형식", "짝 글상자 옛 형식")),
    p(drawing(wgp(wsp("묶음 속 글상자"), wgp(wsp("안 묶음 글상자"))))),
    p(r("미주가 달린 문단"), note_ref("endnote", "1")),
    p(r("같은 각주를 다시 참조"), note_ref("footnote", "2"), extra=NOTE_SECT),
    p("둘째 구역 문단이다."),
    final=sect_refs(("header", "default", "rIdH1"), ("footer", "default", "rIdF1")))
SAMPLES: dict[str, bytes] = {
    "basic.docx": docx(BASIC, BASIC_STYLES, numbering("1", "2")),
    "headings.docx": docx(HEADINGS, HEAD_STYLES),
    "table.docx": docx(TABLE, TABLE_STYLES),
    "fields.docx": docx(FIELDS),
    "figures.docx": docx(FIGURES, doc_rels=[*FIGURE_MEDIA["doc_rels"], *FIGURE_RELS], extra=FIGURE_MEDIA["extra"]),
    "notes.docx": docx(NOTES, doc_rels=[(rid, REL[kind], member) for rid, kind, member, _ in NOTE_PARTS],
                       extra=[*((f"word/{member}", xml) for _, _, member, xml in NOTE_PARTS),
                              ("word/_rels/footer1.xml.rels", rels(("rId1", f"{R}/image", "media/image1.png"))),
                              ("word/media/image1.png", GRAY_PNG)]),
}
ROOT = Path(__file__).resolve().parent / "fixtures" / "ooxml"


def document_id(name: str) -> str:
    return "fx-ooxml-" + Path(name).stem


def golden(name: str, data: bytes) -> tuple[DocumentTree, ProcessingHistory, list[dict[str, object]]]:
    """엔진 ingest와 같은 SourceInfo(쪽 없음 → page_count None). 셋째는 그림 자산의 화소 정보(처음 나온 차례)."""
    parsed = DocxParser().parse(data, name)
    source = SourceInfo(name=name, mime=parsed.mime, content_hash="sha256:" + hashlib.sha256(data).hexdigest())
    tree = build_tree(parsed, document_id(name), 1, source)
    order = list(dict.fromkeys(b.figure.asset for b in tree.blocks if b.figure is not None))
    return (tree, ProcessingHistory(document_id=tree.document_id, version=1, regions=parsed.regions),
            [pixels(parsed.assets[key]) for key in order])


def pixels(png: bytes) -> dict[str, object]:
    """PNG를 디코드한 화소 정보(압축 바이트와 상관없다): 모드, 크기, 원시 화소의 sha256."""
    with Image.open(BytesIO(png)) as image:
        return {"mode": image.mode, "size": list(image.size), "sha256": hashlib.sha256(image.tobytes()).hexdigest()}


def portable(tree: dict) -> dict:
    """그림 자산 키를 처음 나온 차례의 자리표(asset-1, …)로 바꾸고 그 키에서 나온 값(그림 블록의 figure.asset·
    content_hash·block_id와 그 값을 가리키는 곳)을 가린 트리(HWPX 골든과 같은 규칙)."""
    masks: dict[str, str] = {}
    for block in tree["blocks"]:
        figure = block.get("figure")
        if figure is None:
            continue
        mark = masks.setdefault(figure["asset"], f"asset-{sum(m.startswith('asset-') for m in masks.values()) + 1}")
        masks[block["content_hash"]] = f"hash-of-{mark}"
        masks[block["block_id"]] = f"block-{block['order']}"

    def walk(value: object) -> object:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return masks.get(value, value) if isinstance(value, str) else value

    return walk(tree)  # type: ignore[return-value]


def same(rel: str, built: bytes, path: Path) -> bool:
    """골든 비교: 트리는 portable로(그림 자산 키에서 나온 값만 가린다), 입력·처리 이력·화소 정보는 바이트 그대로."""
    if not path.exists():
        return False
    old = path.read_bytes()
    if rel.startswith("expected/") and not rel.endswith((".history.json", ".pixels.json")):
        return portable(json.loads(old)) == portable(json.loads(built))
    return old == built


def _json(model: DocumentTree | ProcessingHistory) -> bytes:
    return (json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_all() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name, data in SAMPLES.items():
        tree, history, shown = golden(name, data)
        out[f"inputs/{name}"] = data
        out[f"expected/{Path(name).stem}.json"] = _json(tree)
        out[f"expected/{Path(name).stem}.history.json"] = _json(history)
        out[f"expected/{Path(name).stem}.pixels.json"] = (json.dumps(shown, indent=2) + "\n").encode("utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hanji OOXML fixtures")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for rel, data in build_all().items():
        path = ROOT / rel
        if args.check:
            if not same(rel, data, path):
                stale.append(rel)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(path)
    for rel in stale:
        print(f"stale fixture: {rel}", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
