"""PDF 쪽 모델(page_models)과 조립(parse(models=…)): 쪽 묶음마다 따로 계산한 쪽 모델로 조립한 결과가 parse()와
바이트까지 같다. 가짜 OCR·검출기는 그린 쪽마다 같은 줄·상자를 돌려준다(부르는 순서에 기대지 않는다). 글자는 모두
지어낸 합성 문장이다."""

import io
from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hanji import LocalEngine, MemoryStore
from hanji.errors import ParseError
from hanji.formats.pdf import extract, figures, layout, ocr, scan
from hanji.formats.pdf import parser as pdf_parser
from hanji.formats.pdf.extract import extract_pages, pdfium
from hanji.formats.pdf.layout import LayoutBox
from hanji.formats.pdf.ocr import OcrLine
from hanji.formats.pdf.parser import PageModels, PdfParser, page_models

FONT = "HYGothic-Medium"
OTHER_KOREAN_FONT = "HYSMyeongJo-Medium"  # 이름이 한국어 글꼴인 다른 미임베드 CID 글꼴
OTHER_CID_FONT = "STSong-Light"  # 이름이 한국어 글꼴이 아닌 미임베드 CID 글꼴
for _font in (FONT, OTHER_KOREAN_FONT, OTHER_CID_FONT):
    pdfmetrics.registerFont(UnicodeCIDFont(_font))
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = [*sorted((FIXTURES / "pdf" / "inputs").glob("*.pdf")), FIXTURES / "ocr" / "scanned.pdf",
            FIXTURES / "layout" / "figures.pdf"]
W, H = 595.0, 842.0
RED = Image.new("RGB", (40, 30), (220, 30, 30))
BLUE = Image.new("RGB", (40, 30), (30, 60, 220))
CHART = ("chart", 0.9, (90.0, 140.0, 510.0, 300.0))  # 가짜 검출기 상자(보이는 쪽 pt)
CAPTION = ("figure_title", 0.8, (195.0, 312.0, 300.0, 325.0))
OVER = (70.0, 60.0, 300.0, 78.0)  # 기준선 y=770 11pt 줄을 덮는 OCR 줄 상자(보이는 쪽 pt)
BROKEN_CHARS = 9  # broken_page의 보이는 공백 아닌 글자 수(가짜 분류가 이 수로 unreliable 쪽을 고른다)


def pdf(*pages, size=(W, H), rotation=0, crop=None) -> bytes:
    """pages: Canvas를 받아 한 쪽을 그리는 함수들. rotation은 /Rotate, crop은 CropBox(쪽마다 같다)."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=size, invariant=1, pageCompression=0)
    for draw in pages:
        c.setPageRotation(rotation)
        if crop is not None:
            c.setCropBox(crop)
        draw(c)
        c.showPage()
    c.save()
    return buf.getvalue()


def put(c: Canvas, x: float, y: float, size: float, s: str, font: str = FONT) -> None:
    c.setFont(font, size)
    c.drawString(x, y, s)


def text_page(c: Canvas) -> None:
    put(c, 72, 770, 11, "글자만 있는 쪽이다.")


def photo_page(c: Canvas) -> None:
    put(c, 72, 770, 11, "사진 위 문단이다.")
    c.drawImage(ImageReader(BLUE), 150, 300, width=300, height=200)


def broken_page(c: Canvas) -> None:
    """unreliable로 볼 쪽(가짜 분류가 글자 수 BROKEN_CHARS로 고른다)과 사진."""
    put(c, 72, 770, 11, "깨진 쪽 글자다 하나 둘")
    c.drawImage(ImageReader(RED), 150, 300, width=300, height=200)


def chart_page(c: Canvas) -> None:
    """선 12개(path ≥ LAYOUT_MIN_PATHS)로 그린 '차트'와 안의 이름표, 아래 캡션. 상자는 가짜 검출기가 준다."""
    put(c, 72, 770, 11, "차트 위 문단이다.")
    for i in range(12):
        c.line(100, 580 + 10 * i, 500, 580 + 10 * i)
    put(c, 120, 560, 9, "1분기")
    put(c, 200, 520, 10, "그림 1. 분기별 실적")


def snapshot(parsed) -> tuple[str, dict[str, bytes]]:
    """비교할 바이트: 자산을 뺀 JSON과 자산."""
    return parsed.model_dump_json(exclude={"assets"}), parsed.assets


def chunked(data: bytes, name: str, size: int | None, **settings) -> dict[int, PageModels]:
    """쪽을 size쪽씩(None이면 한 묶음) 묶어 묶음마다 따로 page_models를 부른다(쪽 수는 PDFium으로만 센다)."""
    document = pdfium.PdfDocument(data)
    count = len(document)
    document.close()
    size = size or count
    models: dict[int, PageModels] = {}
    for start in range(0, count, size):
        models.update(page_models(data, name, range(start, min(start + size, count)), **settings))
    return models


def assert_same(data: bytes, name: str, **settings):
    """쪽 묶음 1쪽·2쪽·전체로 조립한 결과가 parse()와 바이트까지 같다. parse() 결과를 돌려준다."""
    whole = PdfParser(**settings).parse(data, name)
    for size in (1, 2, None):
        assembled = PdfParser(**settings).parse(data, name, models=chunked(data, name, size, **settings))
        assert snapshot(assembled) == snapshot(whole), size
    return whole


def fake_ocr(monkeypatch, *lines) -> None:
    """OCR 추가 설치가 있는 것처럼(모델 없이): 그린 쪽마다 같은 줄 (글자, 점수, 보이는 쪽 pt 상자)을 그림 화소(A4 너비
    기준 배율)로 돌려준다."""
    def read_lines(image):
        px = image.size[0] / W
        return [OcrLine(box=((x0 * px, y0 * px), (x1 * px, y0 * px), (x1 * px, y1 * px), (x0 * px, y1 * px)),
                        text=text, score=score) for text, score, (x0, y0, x1, y1) in lines]

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "get_reader", lambda: None)
    monkeypatch.setattr(ocr, "read_lines", read_lines)


def fake_layout(monkeypatch, *found) -> None:
    """레이아웃 설치가 있는 것처럼: detect가 그린 쪽마다 (분류, 점수, 보이는 쪽 pt 상자)를 쪽 렌더 화소로 돌려준다."""
    def detect(image):
        sx, sy = image.width / W, image.height / H
        return [LayoutBox(cls, score, (x0 * sx, y0 * sy, x1 * sx, y1 * sy)) for cls, score, (x0, y0, x1, y1) in found]

    monkeypatch.setattr(layout, "available", lambda: True)
    monkeypatch.setattr(layout, "get_detector", lambda: object())
    monkeypatch.setattr(layout, "detect", detect)


def unreliable_when(monkeypatch, chars: int) -> None:
    """보이는 글자 수가 chars인 쪽만 unreliable(나머지는 진짜 분류). 쪽 순서에 기대지 않는다."""
    real = pdf_parser.classify
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable" if stats.chars == chars else real(stats))


def record_renders(monkeypatch) -> list[tuple[int, tuple[int, int]]]:
    """scan.render 호출을 (쪽 순번, 그림 크기)로 기록한다."""
    calls = []
    real = scan.render

    def render(data, name, index):
        image = real(data, name, index)
        calls.append((index, image.size))
        return image

    monkeypatch.setattr(scan, "render", render)
    return calls


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_chunked_page_models_assemble_byte_for_byte_like_parse(path):
    """골든 PDF 전부와 OCR·그림 예제(설치된 OCR·레이아웃 모델, 기본 설정): 쪽을 1쪽·2쪽·전체로 묶어 묶음마다 따로
    page_models를 부른 뒤 parse(models=…)로 조립한 결과가 parse()와 바이트까지 같다."""
    assert_same(path.read_bytes(), path.name)


def host(monkeypatch, draws_hangul) -> None:
    """컴퓨터 흉내: draws_hangul(BaseFont 이름)이 참인 미임베드 글꼴만 '가'를 그린다."""
    monkeypatch.setattr(extract, "_draws_hangul", lambda pdf, font: draws_hangul(extract._base_font_name(font)))


def lost_hangul_page(c: Canvas) -> None:
    """'가'를 못 그리는 글꼴의 한글 줄과 이름이 한국어가 아닌 글꼴의 빠진 공백 객체."""
    put(c, 72, 770, 11, "가나")
    put(c, 72, 750, 11, " ", OTHER_CID_FONT)


def test_hangul_check_partial_would_fail_but_the_whole_document_passes(monkeypatch):
    """1쪽만 놓고 검사하면 실패할 문서(1쪽 한글 글꼴이 '가'를 못 그리고 빠진 객체가 있다)도 2쪽 한글 글꼴이 '가'를
    그리면 전체 추출은 지금처럼 통과한다. 쪽 일부 추출은 검사하지 않으므로 1쪽 묶음도 통과하고 조립 결과는 parse()와
    같다."""
    host(monkeypatch, lambda name: name.startswith(OTHER_KOREAN_FONT))
    with pytest.raises(ParseError, match="no Hangul glyphs"):
        extract_pages(pdf(lost_hangul_page), "h.pdf")  # 1쪽만인 문서: 실패
    data = pdf(lost_hangul_page, lambda c: put(c, 72, 770, 11, "다라", OTHER_KOREAN_FONT))
    assert extract_pages(data, "h.pdf", [0])[0].page == 1
    assert_same(data, "h.pdf", ocr=False, layout=False)


def test_hangul_check_partial_passes_but_the_whole_document_fails_in_assembly(monkeypatch):
    """1쪽(라틴 글자 + 빠진 공백 객체)만 보면 통과하지만 2쪽에 '가'를 못 그리는 글꼴의 한글이 있으면 전체 추출은 지금처럼
    1쪽 위치로 실패한다. 쪽 일부 page_models는 두 쪽 모두 통과하고 조립(parse(models=…))이 parse()와 같은 오류로 실패한다."""
    host(monkeypatch, lambda name: False)
    data = pdf(lambda c: (put(c, 72, 770, 11, "AB", "Helvetica"), put(c, 72, 750, 11, " ", OTHER_CID_FONT)),
               lambda c: put(c, 72, 770, 11, "가나"))
    models = chunked(data, "h.pdf", 1, ocr=False, layout=False)
    assert sorted(models) == [0, 1]
    with pytest.raises(ParseError, match="no Hangul glyphs") as whole:
        PdfParser(ocr=False, layout=False).parse(data, "h.pdf")
    with pytest.raises(ParseError, match="no Hangul glyphs") as assembled:
        PdfParser(ocr=False, layout=False).parse(data, "h.pdf", models=models)
    assert str(assembled.value) == str(whole.value) and whole.value.location == "h.pdf:1"


@pytest.mark.parametrize("score", [0.9, 0.3], ids=["ocr", "no-ocr-text"])
def test_unreliable_page_read_by_ocr_or_kept_is_the_same_when_chunked(monkeypatch, score):
    """unreliable 쪽(가짜 분류)을 OCR로 읽는 경로(점수 0.9)와 받아들인 OCR 글자가 없어 깨진 글자층으로 돌아오는 경로
    (점수 0.3: 판정 기록 ocr_text, 사진은 조립이 다시 그려 자른다)가 쪽 묶음으로 나눠도 parse()와 같다."""
    unreliable_when(monkeypatch, BROKEN_CHARS)
    fake_ocr(monkeypatch, ("다시 읽은 쪽 글자다.", score, OVER))
    data = pdf(photo_page, broken_page, photo_page)
    models = chunked(data, "u.pdf", 1, layout=False)
    assert models[1].mode == ("ocr" if score > 0.5 else "layer")
    assert (models[1].gate is None) == (score > 0.5) and models[1].size is not None
    assert [models[k].lines for k in (0, 2)] == [None, None]
    whole = assert_same(data, "u.pdf", layout=False)
    assert [r.fallback_reason for r in whole.regions] == [
        "unreliable_text_layer_ocr" if score > 0.5 else "unreliable_text_layer_kept"]


@pytest.mark.parametrize("shape", [{"rotation": 90}, {"crop": (50, 60, 550, 760)}, {"size": (3000.0, 4000.0)}],
                         ids=["rotated", "cropbox", "render-cap"])
def test_redrawn_figure_pages_match_on_rotated_cropped_and_capped_pages(monkeypatch, shape):
    """다시 그리기 경계: 회전 쪽, 원점이 0이 아닌 CropBox, 긴 변이 MAX_RENDER_SIDE(4000px)에 걸리는 쪽. 조립은 그림을
    잘라야 하는 쪽만 같은 scan.render로 다시 그리고 그 크기는 쪽 모델의 크기와 같으며, 결과는 parse()와 같다."""
    fake_layout(monkeypatch, CHART, CAPTION)
    data = pdf(chart_page, text_page, photo_page, **shape)
    whole = assert_same(data, "r.pdf", ocr=False)
    drawn = sorted({b["locator"]["page"] - 1 for b in whole.blocks if b["kind"] == "figure"})
    assert drawn and whole.assets
    models = chunked(data, "r.pdf", 1, ocr=False)
    if "size" in shape:
        assert max(models[0].size) == scan.MAX_RENDER_SIDE
    renders = record_renders(monkeypatch)
    PdfParser(ocr=False).parse(data, "r.pdf", models=models)
    assert renders == [(k, models[k].size) for k in drawn] and models[1].size is None


def test_unreliable_first_page_takes_only_the_asset_budget_left_when_chunked(monkeypatch):
    """unreliable 쪽이 앞에 있어 조립 순서(unreliable 쪽을 뒤로)와 자산 예산 순서가 바뀌는 문서: 예산이 그림 하나뿐이면
    뒤 digital 쪽 사진이 이미지를 갖고 앞 unreliable 쪽 사진은 이미지 없이 처리 이력에 남는다. 묶음으로 나눠도 같다."""
    unreliable_when(monkeypatch, BROKEN_CHARS)
    data = pdf(broken_page, photo_page)
    full = PdfParser(ocr=False, layout=False).parse(data, "b.pdf")
    monkeypatch.setattr(figures, "ASSET_LIMIT", max(len(png) for png in full.assets.values()))
    capped = assert_same(data, "b.pdf", ocr=False, layout=False)
    assert len(capped.assets) == 1 and "asset" in capped.blocks[-1]["figure"]
    assert [(r.region_id, r.fallback_reason) for r in capped.regions] == [
        ("p1-unreliable-text-layer", "unreliable_text_layer_kept"), ("p1-figure-1", pdf_parser.NO_IMAGE_PHOTO)]


def test_ocr_use_per_chunk_is_the_document_use_for_every_page(monkeypatch):
    """문서 단위 use_ocr(scanned·unreliable 쪽이 있고 OCR을 쓸 수 있음)와 쪽 묶음 단위가 쪽마다 같다: 쪽마다 따로 부른
    결과는 한 번에 부른 결과와 같고, digital 쪽만 있는 묶음은 OCR 설치를 확인하지도 않는다."""
    unreliable_when(monkeypatch, BROKEN_CHARS)
    fake_ocr(monkeypatch, ("다시 읽은 쪽 글자다.", 0.9, OVER))
    data = pdf(photo_page, broken_page)
    together = page_models(data, "e.pdf", range(2), layout=False)
    apart = {**page_models(data, "e.pdf", [1], layout=False), **page_models(data, "e.pdf", [0], layout=False)}
    assert apart == together and together[0].lines is None and together[1].lines is not None
    monkeypatch.setattr(ocr, "available", lambda: pytest.fail("digital 쪽만 있는 묶음은 OCR 설치를 보지 않는다"))
    assert page_models(data, "e.pdf", [0], layout=False) == {0: together[0]}


def test_assembly_with_every_page_model_runs_no_model_and_redraws_only_figure_pages(monkeypatch):
    """모든 쪽 모델이 있으면 조립은 OCR·레이아웃 설치 확인도 모델도 부르지 않고, 그림이 있는 쪽만 다시 그린다."""
    fake_layout(monkeypatch, CHART, CAPTION)
    data = pdf(text_page, chart_page, photo_page)
    models = chunked(data, "m.pdf", 2)
    for runtime, attr in ((layout, "available"), (layout, "get_detector"), (layout, "detect"), (ocr, "available"),
                          (ocr, "get_reader"), (ocr, "read_lines")):
        monkeypatch.setattr(runtime, attr, lambda *a, attr=attr: pytest.fail(f"조립은 {attr}를 부르지 않는다"))
    renders = record_renders(monkeypatch)
    PdfParser().parse(data, "m.pdf", models=models)
    assert [index for index, _ in renders] == [1, 2]


def test_models_for_a_page_the_document_does_not_have_are_refused():
    data = pdf(text_page)
    with pytest.raises(ValueError, match="does not have"):
        PdfParser(ocr=False, layout=False).parse(data, "x.pdf", models={1: PageModels("layer", None, None, None, None)})
