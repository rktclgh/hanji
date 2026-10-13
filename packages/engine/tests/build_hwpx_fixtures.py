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

from PIL import Image

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


def pic(ref: str, caption: str = "", width: int = 7200) -> str:
    """hp:pic(표시 너비 width HWPUNIT, 7200 = 1인치). shapeComment는 한컴이 자동으로 쓰는 개체 설명 자리다."""
    return (f'<hp:pic id="1"><hp:curSz width="{width}" height="3600"/>{caption}'
            f'<hc:img binaryItemIDRef="{ref}" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/>'
            f"<hp:shapeComment>그림입니다.</hp:shapeComment></hp:pic>")


def caption(*paras: str, side: str = "BOTTOM") -> str:
    return f'<hp:caption side="{side}"><hp:subList>{"".join(paras)}</hp:subList></hp:caption>'


def container(*objects: str) -> str:
    return f'<hp:container id="2">{"".join(objects)}</hp:container>'


def textart(text: str) -> str:
    return f'<hp:textart id="6"{attrs(text=text)}/>'


def ole() -> str:
    return '<hp:ole id="7" binaryItemIDRef="ole1"/>'


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

# 그림 원본: Pillow로 한 번 만든 바이트를 고정한다(4×4 반투명 RGBA PNG, 8×8 CMYK JPEG, 4×2 BMP, 4100×2 회색 PNG,
# 6×6 회색 PNG). 가짜 WMF는 placeable WMF 머리만 있다(Pillow 허용 형식이 아니다)
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
BMP = bytes.fromhex(
    "424d4e0000000000000036000000280000000400000002000000010018000000000018000000c40e0000c40e00000000000000000000"
    "dc7814dc7814dc7814dc7814dc7814dc7814dc7814dc7814")
WIDE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200001004000000020800000000f435e674000000234944415478daedd0010100000802a06ab9"
    "d31b224c6033000000408353000000001d1e658e008461bdcbc80000000049454e44ae426082")
GRAY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000006000000060800000000c5a7b094000000134944415478da638c626060606060622048"
    "01000f900066ffa59b760000000049454e44ae426082")
FAKE_WMF = bytes.fromhex("d7cdc69a") + bytes(40)
FIGURE_ITEMS = [("image1", "BinData/image1.png", "image/png", True),
                ("image2", "BinData/image2.jpg", "image/jpg", True),
                ("image3", "BinData/image3.bmp", "image/bmp", True),
                ("image4", "BinData/image4.png", "image/png", True),
                ("image5", "BinData/image5.wmf", "image/x-wmf", True),
                ("image6", "BinData/image6.png", "image/png", False)]
FIGURE_BINARIES = {"BinData/image1.png": RGBA_PNG, "BinData/image2.jpg": CMYK_JPEG, "BinData/image3.bmp": BMP,
                   "BinData/image4.png": WIDE_PNG, "BinData/image5.wmf": FAKE_WMF}
FIGURES = section(
    para(run(t("그림 예제 문단이다."))),
    para(run(pic("image1"))),
    para(run(pic("image2", width=14400))),
    para(run(pic("image3"))),
    para(run(pic("image4", width=72000))),
    para(run(pic("image1"))),
    para(run(pic("image5"))),
    para(run(pic("image6"))),
    para(run(pic("image9"))),
    para(run(pic("image3", caption(para(run(t("그림 1. 지어낸 막대 그림")))), width=0))),
    para(run(pic("image5", caption(para(run(t("그림 2. 이미지 없는 그림의 캡션"))))))),
    para(run(pic("image2", caption(para(run(t("그림 3. 위에 둔 캡션"))), side="TOP")))),
    para(run(container(pic("image3"), pic("image1")))),
    para(run(t("글맵시 앞"), textart("지어낸 글맵시"))),
)
SAMPLES: dict[str, bytes] = {
    "basic.hwpx": package(*BASIC, head=BASIC_HEAD),
    "headings.hwpx": package(*HEADINGS, head=HEAD_SIZES),
    "empty.hwpx": package(section(para())),
    "figures.hwpx": package(FIGURES, items=FIGURE_ITEMS, binaries=FIGURE_BINARIES),
}
ROOT = Path(__file__).resolve().parent / "fixtures" / "hwpx"


def document_id(name: str) -> str:
    return "fx-hwpx-" + Path(name).stem


def golden(name: str, data: bytes) -> tuple[DocumentTree, ProcessingHistory, list[dict[str, object]]]:
    """엔진 ingest와 같은 SourceInfo(쪽 없음 → page_count None). 셋째는 그림 자산의 화소 정보(처음 나온 차례)."""
    parsed = HwpxParser().parse(data, name)
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
    content_hash·block_id와 그 값을 가리키는 곳)을 가린 트리. OS마다 PNG 압축 바이트가 달라도 같다."""
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
    parser = argparse.ArgumentParser(description="hanji HWPX fixtures")
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
