"""HWPX 합성 문서 쓰기 도구(지어낸 한국어 글자만).

zip은 ZIP_STORED(zlib 판 차이 회피), 고정 시각, 고정 멤버 순서, mimetype이 첫 멤버다. 테스트는 이 파일을
importlib로 읽어 쓰기 도구(para·run·t·section·header·package)를 쓴다.
"""

import zipfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

HS = "http://www.hancom.co.kr/hwpml/2011/section"
HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HC = "http://www.hancom.co.kr/hwpml/2011/core"
HH = "http://www.hancom.co.kr/hwpml/2011/head"
OPF = "http://www.idpf.org/2007/opf/"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
WHEN = (1980, 1, 1, 0, 0, 0)
MIME = b"application/hwp+zip"


def attrs(**values: object) -> str:
    """키워드 → XML 속성(이름의 _는 -로)."""
    return "".join(f" {k.replace('_', '-')}={quoteattr(str(v))}" for k, v in values.items())


def t(text: str = "", *inline: str) -> str:
    """hp:t. inline은 글자 뒤에 이어 붙일 요소·글자 조각(tab() 등과 escape된 글자)."""
    return f"<hp:t>{escape(text)}{''.join(inline)}</hp:t>"


def tab() -> str:
    return "<hp:tab/>"


def line_break() -> str:
    return "<hp:lineBreak/>"


def nb_space() -> str:
    return "<hp:nbSpace/>"


def fw_space() -> str:
    return "<hp:fwSpace/>"


def title_mark() -> str:
    return '<hp:titleMark ignore="0"/>'


def run(*items: str, char: int = 0) -> str:
    return f'<hp:run charPrIDRef="{char}">{"".join(items)}</hp:run>'


def para(*runs: str, pr: int = 0, lines: int | None = None) -> str:
    """hp:p. lines가 있으면 그만큼의 hp:lineseg(저장된 표시 줄)."""
    segs = ""
    if lines is not None:
        segs = "<hp:linesegarray>" + '<hp:lineseg textpos="0" vertpos="0"/>' * lines + "</hp:linesegarray>"
    return f'<hp:p id="0" paraPrIDRef="{pr}" styleIDRef="0">{"".join(runs)}{segs}</hp:p>'


def section(*paras: str) -> str:
    return f'{DECL}<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}" xmlns:hc="{HC}">{"".join(paras)}</hs:sec>'


def header(headings: Mapping[int, tuple[str, int]] = (), chars: Mapping[int, tuple[int, bool]] = (),
           fills: Mapping[int, str] = ()) -> str:
    """header.xml. headings: paraPr id → (heading@type, @level), chars: charPr id → (height 1/100pt, 굵게),
    fills: borderFill id → winBrush@faceColor. 문단 모양 0(NONE)과 글자 모양 0(10pt)은 늘 있다."""
    headings = {0: ("NONE", 0), **dict(headings)}
    chars = {0: (1000, False), **dict(chars)}
    fills = {1: "none", **dict(fills)}
    para_prs = "".join(f'<hh:paraPr id="{k}"><hh:heading{attrs(type=kind, idRef=0, level=level)}/></hh:paraPr>'
                       for k, (kind, level) in headings.items())
    char_prs = "".join(f'<hh:charPr id="{k}" height="{height}">{"<hh:bold/>" if bold else ""}'
                       f'<hh:relSz hangul="100" latin="100"/></hh:charPr>' for k, (height, bold) in chars.items())
    border_fills = "".join(f'<hh:borderFill id="{k}"><hc:fillBrush><hc:winBrush faceColor="{color}"/></hc:fillBrush>'
                           f"</hh:borderFill>" for k, color in fills.items())
    return (f'{DECL}<hh:head xmlns:hh="{HH}" xmlns:hc="{HC}"><hh:refList><hh:borderFills>{border_fills}'
            f"</hh:borderFills><hh:charProperties>{char_prs}</hh:charProperties><hh:paraProperties>{para_prs}"
            f"</hh:paraProperties></hh:refList></hh:head>")


def content(sections: int, items: Sequence[tuple[str, str, str, bool]] = (), spine: Sequence[str] | None = None) -> str:
    """content.hpf. items: (id, href, media-type, 내장 여부) 그림 항목. spine이 없으면 header 다음 구역 차례."""
    entries = [("header", "Contents/header.xml")]
    entries += [(f"section{k}", f"Contents/section{k}.xml") for k in range(sections)]
    manifest = "".join(f"<opf:item{attrs(id=key, href=href, media_type='application/xml')}/>" for key, href in entries)
    manifest += "".join(f"<opf:item{attrs(id=key, href=href, media_type=media, isEmbeded=int(embedded))}/>"
                        for key, href, media, embedded in items)
    refs = ["header", *(f"section{k}" for k in range(sections))] if spine is None else spine
    return (f'{DECL}<opf:package xmlns:opf="{OPF}"><opf:manifest>{manifest}</opf:manifest><opf:spine>'
            + "".join(f'<opf:itemref idref="{ref}"/>' for ref in refs) + "</opf:spine></opf:package>")


def package(*sections: str, head: str | None = None, items: Sequence[tuple[str, str, str, bool]] = (),
            binaries: Mapping[str, bytes] = (), extra: Mapping[str, bytes] = (), spine: Sequence[str] | None = None,
            mimetype: bytes = MIME) -> bytes:
    """HWPX 바이트. 멤버 순서: mimetype, Contents/content.hpf, header, 구역들, binaries(BinData/…), extra."""
    members = [("mimetype", mimetype), ("Contents/content.hpf", content(len(sections), items, spine).encode("utf-8")),
               ("Contents/header.xml", (header() if head is None else head).encode("utf-8"))]
    members += [(f"Contents/section{k}.xml", s.encode("utf-8")) for k, s in enumerate(sections)]
    members += list(dict(binaries).items()) + list(dict(extra).items())
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in members:
            z.writestr(zipfile.ZipInfo(name, WHEN), data)
    return buf.getvalue()
