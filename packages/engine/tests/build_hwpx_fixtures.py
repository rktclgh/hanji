"""HWPX 합성 문서 쓰기 도구와 골든 예제 생성기(지어낸 한국어 글자만).

    uv run python packages/engine/tests/build_hwpx_fixtures.py          # fixtures/hwpx/ 재생성
    uv run python packages/engine/tests/build_hwpx_fixtures.py --check  # 최신인지 확인(바이트 비교)

zip은 ZIP_STORED(zlib 판 차이 회피), 고정 시각, 고정 멤버 순서, mimetype이 첫 멤버다. 테스트는 이 파일을
importlib로 읽어 쓰기 도구(para·run·t·section·header·package)를 쓴다.
"""

import argparse
import hashlib
import json
import sys
import zipfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from hanji.core import build_tree
from hanji.formats.hwpx import HwpxParser
from hanji_contracts import DocumentTree, ProcessingHistory, SourceInfo

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
            info = zipfile.ZipInfo(name, WHEN)
            info.create_system = 3  # 만든 OS 표시를 고정(Windows 기본 0이면 헤더 바이트가 달라진다)
            z.writestr(info, data)
    return buf.getvalue()


# ---- 골든 예제 ----

BASIC_HEAD = header(headings={1: ("OUTLINE", 0), 2: ("OUTLINE", 1), 3: ("OUTLINE", 7), 4: ("NUMBER", 0),
                              5: ("BULLET", 0)})
BASIC = (
    section(
        para(run(t("2026년 지어낸 사업 계획")), pr=1),
        para(run(t("본 문서는 "), char=0), run(t("합성 예제"), char=0), run(t("로 만든 글이다."))),
        para(),
        para(run(t("1. 추진 배경")), pr=2),
        para(run(t("구분", tab(), "내용", line_break(), "둘째 줄", nb_space(), "묶음", fw_space(), "고정폭",
                   title_mark()))),
        para(run(t("첫째 번호 항목")), pr=4),
        para(run(t("글머리 항목")), pr=5),
        para(run(t("모르는 문단 모양")), pr=42),
    ),
    section(
        para(run(t("아주 깊은 개요")), pr=3),
        para(run(t("총액 1,234.5억원, 증가율 12.3%, 면적 84㎡, 변동 △0.5, GDP 대비 2.1%p(OECD 평균)"))),
        para(run(t("표시 ", "<hp:markpen>강조한</hp:markpen>", " 글자"))),
        para(run(t("2. 다음 절")), pr=2),
        para(run(t("끝 문단이다."))),
    ),
)
HEAD_SIZES = header(chars={1: (1600, False), 2: (1100, True), 3: (2000, False)})
LONG = "본문 크기로 쓴 지어낸 문단이다. 이 문단의 글자 수가 문서에서 가장 많아 본문 크기가 된다."
HEADINGS = (
    section(
        para(run(t("지어낸 보고서 제목"), char=3), lines=1),
        para(run(t(LONG)), lines=2),
        para(run(t("큰 글자 소제목"), char=1), lines=1),
        para(run(t(LONG)), lines=2),
        para(run(t("큰 글자로 쓴 강조 문단은 세 줄로 이어지면 제목이 아니라 문단이다."), char=1), lines=3),
        para(run(t("굵은 작은 제목"), char=2), lines=1),
        para(run(t("크게"), char=1), run(t("시작했지만 대부분이 본문 크기인 문단이다.")), lines=1),
        para(run(t(LONG)), lines=2),
    ),
)
SAMPLES: dict[str, bytes] = {
    "basic.hwpx": package(*BASIC, head=BASIC_HEAD),
    "headings.hwpx": package(*HEADINGS, head=HEAD_SIZES),
    "empty.hwpx": package(section(para())),
}
ROOT = Path(__file__).resolve().parent / "fixtures" / "hwpx"


def document_id(name: str) -> str:
    return "fx-hwpx-" + Path(name).stem


def golden(name: str, data: bytes) -> tuple[DocumentTree, ProcessingHistory]:
    """엔진 ingest와 같은 SourceInfo(쪽 없음 → page_count None)."""
    parsed = HwpxParser().parse(data, name)
    source = SourceInfo(name=name, mime=parsed.mime, content_hash="sha256:" + hashlib.sha256(data).hexdigest())
    tree = build_tree(parsed, document_id(name), 1, source)
    return tree, ProcessingHistory(document_id=tree.document_id, version=1, regions=parsed.regions)


def _json(model: DocumentTree | ProcessingHistory) -> bytes:
    return (json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_all() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name, data in SAMPLES.items():
        tree, history = golden(name, data)
        out[f"inputs/{name}"] = data
        out[f"expected/{Path(name).stem}.json"] = _json(tree)
        out[f"expected/{Path(name).stem}.history.json"] = _json(history)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hanji HWPX fixtures")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for rel, data in build_all().items():
        path = ROOT / rel
        if args.check:
            if not path.exists() or path.read_bytes() != data:
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
