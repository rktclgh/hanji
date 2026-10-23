"""PdfParser의 그림·캡션(레이아웃 연결): digital 쪽 사진(이미지 객체)은 레이아웃 없이도 그림, 모델 상자는 바꿔 끼운
가짜 검출기로(onnxruntime 없이), 회전·CropBox 잘라내기, 그림 우선과 처리 이력, 바이트 상한, 켜고 끄기.
그림 속 글자 그림은 OS 글꼴에 기대지 않으려고 hanji-fonts 글꼴(Pillow)로 그린다."""

import gc
import io
import os
import sys
import time
import weakref
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageStat
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

import hanji_fonts
from hanji import LocalEngine, MemoryStore, SqliteStore, models
from hanji.core import build_tree
from hanji.errors import LayoutUnavailable
from hanji.formats.pdf import PdfParser, figures, layout, ocr, scan
from hanji.formats.pdf import parser as pdf_parser
from hanji.formats.pdf.layout import LayoutBox
from hanji.formats.pdf.scan import OcrText
from hanji_contracts import SourceInfo

FONT = "HYGothic-Medium"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))
NOTO = str(hanji_fonts.font_dir() / hanji_fonts.FONT_FILE)
W, H = 595.0, 842.0
PX = 200 / 72
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pdf" / "inputs"
RED = Image.new("RGB", (40, 30), (220, 30, 30))
BLUE = Image.new("RGB", (40, 30), (30, 60, 220))
CHART = ("chart", 0.9, (90.0, 140.0, 510.0, 300.0))  # 가짜 검출기 상자(보이는 쪽 pt)
CAPTION = ("figure_title", 0.8, (195.0, 312.0, 300.0, 325.0))


def put(c: Canvas, x: float, y: float, size: float, s: str) -> None:
    c.setFont(FONT, size)
    c.drawString(x, y, s)


def pdf(*pages, size=(W, H)) -> bytes:
    """pages: Canvas를 받아 한 쪽을 그리는 함수들."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=size, invariant=1, pageCompression=0)
    for draw in pages:
        draw(c)
        c.showPage()
    c.save()
    return buf.getvalue()


def source(name: str, pages: int = 1) -> SourceInfo:
    return SourceInfo(name=name, mime="application/pdf", content_hash="sha256:" + "0" * 64, page_count=pages)


def glyphs(blocks) -> Counter:
    """블록 글자의 다중집합(공백 제외): 레이아웃을 켜고 꺼도 같아야 한다(스펙 §1-3)."""
    return Counter(ch for b in blocks for ch in b["text"] if not ch.isspace())


def fake_layout(monkeypatch, *found) -> list:
    """설치가 있는 것처럼 하고 detect가 (분류, 점수, 보이는 쪽 pt 상자)를 쪽 렌더 화소로 돌려준다(A4 쪽).
    호출마다 렌더 크기를 기록한 목록을 돌려준다."""
    calls = []

    def detect(image):
        calls.append(image.size)
        sx, sy = image.width / W, image.height / H
        return [LayoutBox(cls, score, (x0 * sx, y0 * sy, x1 * sx, y1 * sy)) for cls, score, (x0, y0, x1, y1) in found]

    monkeypatch.setattr(layout, "available", lambda: True)
    monkeypatch.setattr(layout, "get_detector", lambda: object())
    monkeypatch.setattr(layout, "detect", detect)
    return calls


def photo_page(c: Canvas) -> None:
    put(c, 72, 770, 11, "사진 위 문단이다.")
    c.drawImage(ImageReader(RED), 150, 300, width=300, height=200)  # 쪽 면적 12%: digital 쪽의 사진
    put(c, 72, 250, 11, "사진 아래 문단이다.")


def chart_page(c: Canvas) -> None:
    """선 12개(path ≥ LAYOUT_MIN_PATHS)로 그린 '차트'와 안의 이름표, 아래 캡션. 상자는 가짜 검출기가 준다."""
    put(c, 72, 770, 11, "차트 위 문단이다.")
    for i in range(12):
        c.line(100, 580 + 10 * i, 500, 580 + 10 * i)
    put(c, 120, 560, 9, "1분기")
    put(c, 300, 560, 9, "2분기")
    put(c, 200, 520, 10, "그림 1. 분기별 실적")
    put(c, 72, 470, 11, "차트 아래 문단이다.")


def grid_table(c: Canvas, top: float) -> None:
    """선 있는 2×3 표(칸마다 한 글자)."""
    xs, ys = [100, 200, 300, 400], [top, top - 24, top - 48]
    for y in ys:
        c.line(xs[0], y, xs[-1], y)
    for x in xs:
        c.line(x, ys[0], x, ys[-1])
    for r, row in enumerate([["가", "나", "다"], ["라", "마", "바"]]):
        for k, s in enumerate(row):
            put(c, xs[k] + 8, ys[r] - 17, 11, s)


def text_image(width: float, height: float, lines) -> Image.Image:
    """보이는 쪽 크기(pt)의 흰 그림에 (왼쪽 pt, 윗변 pt, 크기 pt, 글자) 줄을 그린다(200 DPI)."""
    img = Image.new("L", (round(width * PX), round(height * PX)), 255)
    draw = ImageDraw.Draw(img)
    for x, top, size, s in lines:
        draw.text((x * PX, top * PX), s, font=ImageFont.truetype(NOTO, round(size * PX)), fill=0)
    return img


def near(box: dict, expected) -> bool:
    return all(abs(box[k] - v) <= 0.002 for k, v in zip(("x0", "y0", "x1", "y1"), expected))


def require_models(*names: str) -> None:
    """실제 모델 파일이 필요한 테스트: 찾을 수 없으면 건너뛰고, HANJI_CI_REQUIRE_MODELS=1이면 실패한다."""
    missing = [name for name in names if models.find(name) is None]
    if missing:
        if os.environ.get("HANJI_CI_REQUIRE_MODELS") == "1":
            pytest.fail(f"model files not found: {missing}; run `hanji models fetch`")
        pytest.skip(f"model files not found: {missing} (hanji models fetch)")


def test_photo_image_object_is_a_figure_with_its_png_even_with_layout_off():
    parsed = PdfParser(ocr=False, layout=False).parse(pdf(photo_page), "p.pdf")
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [
        ("paragraph", "사진 위 문단이다."), ("figure", ""), ("paragraph", "사진 아래 문단이다.")]
    fig = parsed.blocks[1]
    assert near(fig["locator"]["bbox"], (150 / W, 342 / H, 450 / W, 542 / H))
    image = fig["figure"]
    assert (image["category"], image["mime"], image["dpi"], fig["text_source"]) == ("image", "image/png", 200, "text_layer")
    png = Image.open(io.BytesIO(parsed.assets[image["asset"]]))
    assert png.size == (image["width_px"], image["height_px"]) and abs(png.width - 300 * PX) <= 3
    r, g, b = png.convert("RGB").getpixel((png.width // 2, png.height // 2))
    assert abs(r - 220) <= 3 and abs(g - 30) <= 3 and abs(b - 30) <= 3


@pytest.mark.parametrize("rotation,crop,y,expected", [
    (90, None, 300, (300 / W, 100 / H, 450 / W, 300 / H)),  # MediaBox 842×595, 그림 PDF (100, 300)~(300, 450)
    (0, (50, 100, 545, 800), 600, (50 / 495, 50 / 700, 250 / 495, 200 / 700)),  # CropBox: 보이는 쪽 495×700pt
])
def test_rotated_and_cropbox_pages_crop_the_right_pixels(rotation, crop, y, expected):
    def draw(c):
        if crop:
            c.setCropBox(crop)
        c.setPageRotation(rotation)
        c.drawImage(ImageReader(RED), 100, y, width=200, height=150)

    parsed = PdfParser(ocr=False, layout=False).parse(pdf(draw), "r.pdf")
    (fig,) = [b for b in parsed.blocks if b["kind"] == "figure"]
    assert near(fig["locator"]["bbox"], expected)
    red, green, blue = ImageStat.Stat(Image.open(io.BytesIO(parsed.assets[fig["figure"]["asset"]])).convert("RGB")).mean
    assert red > 180 and green < 80 and blue < 80  # 잘라 낸 곳이 빨간 그림(흰 바탕이 아니다)


def test_layout_figures_captions_and_block_order(monkeypatch):
    calls = fake_layout(monkeypatch, CHART, CAPTION)
    data = pdf(chart_page)
    parsed = PdfParser(ocr=False).parse(data, "c.pdf")
    tree = build_tree(parsed, "d", 1, source("c.pdf"))
    assert [(b.kind, b.text) for b in tree.blocks] == [
        ("paragraph", "차트 위 문단이다."), ("figure", "1분기\n2분기"), ("caption", "그림 1. 분기별 실적"),
        ("paragraph", "차트 아래 문단이다.")]
    figure = tree.blocks[1]
    assert figure.figure.caption_block_id == tree.blocks[2].block_id and figure.figure.category == "chart"
    assert figure.figure.asset in parsed.assets and calls == [(1653, 2339)]
    assert glyphs(parsed.blocks) == glyphs(PdfParser(ocr=False, layout=False).parse(data, "c.pdf").blocks)


def test_figure_wins_drops_the_table_inside_and_records_it_in_the_history(monkeypatch):
    def draw(c):
        put(c, 72, 770, 11, "표가 그림 안에 있다.")
        grid_table(c, 700)

    data = pdf(draw)
    off = PdfParser(ocr=False, layout=False).parse(data, "g.pdf")
    assert [b["kind"] for b in off.blocks] == ["paragraph", "table"]
    monkeypatch.setattr(figures, "LAYOUT_MIN_PATHS", 1)  # 선 7개뿐인 쪽도 모델을 돌린다
    fake_layout(monkeypatch, ("chart", 0.9, (90.0, 130.0, 410.0, 200.0)))
    on = PdfParser(ocr=False).parse(data, "g.pdf")
    assert [b["kind"] for b in on.blocks] == ["paragraph", "figure"]
    assert [(r.region_id, r.kind, r.attempts[0].model_id, r.fallback_reason) for r in on.regions] == [
        ("p1-table-in-figure-1", "table", "PP-DocLayout_plus-L", "table_in_figure_dropped")]
    assert glyphs(on.blocks) == glyphs(off.blocks)


def test_asset_limit_gives_figures_without_image_and_a_history_note(monkeypatch):
    def two_photos(c):
        put(c, 72, 770, 11, "사진 둘이 있는 쪽이다.")
        c.drawImage(ImageReader(RED), 150, 500, width=300, height=150)
        c.drawImage(ImageReader(BLUE), 150, 200, width=300, height=150)

    data = pdf(two_photos)
    full = PdfParser(ocr=False, layout=False).parse(data, "two.pdf")
    first = next(b for b in full.blocks if b["kind"] == "figure")
    monkeypatch.setattr(figures, "ASSET_LIMIT", len(full.assets[first["figure"]["asset"]]))  # 첫 그림만 들어간다
    capped = PdfParser(ocr=False, layout=False).parse(data, "two.pdf")
    red, blue = [b for b in capped.blocks if b["kind"] == "figure"]
    assert "figure" in red and "figure" not in blue and list(capped.assets) == [red["figure"]["asset"]]
    (note,) = capped.regions
    assert (note.region_id, note.kind, note.fallback_reason) == ("p1-figure-2", "figure", "figure_image_not_stored")
    engine = LocalEngine(MemoryStore(), [PdfParser(ocr=False, layout=False)])
    tree = engine.get_tree(engine.ingest_bytes(data, "two.pdf").document_id)
    assert [b.figure is not None for b in tree.blocks if b.kind == "figure"] == [True, False]
    assert engine.history(tree.document_id).regions == capped.regions
    # 리뷰 I7: 이미지를 담지 못한 그림의 짝 캡션은 가리킬 곳(caption_block_id는 figure 안)이 없어 캡션 블록으로 따로
    # 남는다. 그 그림은 처리 이력에 남는다(README "그림 한계")
    monkeypatch.setattr(figures, "ASSET_LIMIT", 0)
    none = PdfParser(ocr=False, layout=False).parse(data, "two.pdf")  # 이미지를 담지 못한 그림마다 처리 이력 한 줄
    assert not none.assets and [(r.region_id, r.kind) for r in none.regions] == [
        ("p1-figure-1", "figure"), ("p1-figure-2", "figure")]
    fake_layout(monkeypatch, CHART, CAPTION)
    parsed = PdfParser(ocr=False).parse(pdf(chart_page), "c.pdf")
    dangling = build_tree(parsed, "d", 1, source("c.pdf"))
    assert [b.kind for b in dangling.blocks] == ["paragraph", "figure", "caption", "paragraph"]
    assert dangling.blocks[1].figure is None and dangling.blocks[2].text == "그림 1. 분기별 실적"
    assert [(r.region_id, r.attempts[0].model_id) for r in parsed.regions] == [("p1-figure-1", "PP-DocLayout_plus-L")]


def test_identical_photos_on_two_pages_share_one_asset():
    parsed = PdfParser(ocr=False, layout=False).parse(pdf(photo_page, photo_page), "same.pdf")
    first, second = [b for b in parsed.blocks if b["kind"] == "figure"]
    assert first["figure"]["asset"] == second["figure"]["asset"] and len(parsed.assets) == 1
    a, b = [x for x in build_tree(parsed, "d", 1, source("same.pdf", 2)).blocks if x.kind == "figure"]
    assert a.content_hash == b.content_hash and a.block_id != b.block_id


def test_page_with_hundreds_of_icons_needs_no_render_or_model(monkeypatch):
    icon = Image.new("RGB", (8, 8), (20, 120, 20))

    def icons(c):
        put(c, 72, 770, 11, "아이콘이 많은 쪽이다.")
        for k in range(300):
            c.drawImage(ImageReader(icon), 20 + (k % 30) * 18, 100 + (k // 30) * 18, width=12, height=12)

    monkeypatch.setattr(scan, "render", lambda *a: pytest.fail("그림이 될 이미지가 없으면 쪽을 그리지 않는다"))
    monkeypatch.setattr(layout, "available", lambda: pytest.fail("모델을 돌릴 쪽이 없으면 설치를 확인하지 않는다"))
    start = time.perf_counter()
    parsed = PdfParser(ocr=False).parse(pdf(icons), "icons.pdf")
    assert [b["kind"] for b in parsed.blocks] == ["paragraph"] and not parsed.assets
    assert time.perf_counter() - start < 30.0  # 느린 CI 러너에도 넉넉히(사전 리뷰 6)


def test_layout_true_without_the_install_fails_at_construction(monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setattr(layout, "_detector", None)
    with pytest.raises(LayoutUnavailable, match=r"hanji\[layout\]"):
        PdfParser(layout=True)
    assert PdfParser().layout is None and PdfParser(layout=False).layout is False  # 자동·끔은 만들 때 확인하지 않는다


def test_auto_mode_with_a_broken_layout_install_raises_before_rendering(monkeypatch):
    """자동 모드에서 설치는 있는데(available) 검출기를 못 만들면 조용히 물러나지 않고 LayoutUnavailable(설정 오류)."""
    def broken():
        raise LayoutUnavailable("layout model could not be loaded: x; run with --no-layout")

    data = (FIXTURES / "image_page.pdf").read_bytes()
    monkeypatch.setattr(layout, "available", lambda: True)
    monkeypatch.setattr(layout, "get_detector", broken)
    monkeypatch.setattr(scan, "render", lambda *a: pytest.fail("깨진 설치는 쪽을 그리기 전에 알린다"))
    with pytest.raises(LayoutUnavailable, match="--no-layout"):
        PdfParser(ocr=False).parse(data, "image_page.pdf")
    assert [b["text"] for b in PdfParser(ocr=False, layout=False).parse(data, "image_page.pdf").blocks] == ["- 3 -"]


def test_auto_mode_does_not_check_the_layout_install_on_text_only_documents(monkeypatch):
    monkeypatch.setattr(layout, "available", lambda: pytest.fail("모델을 돌릴 쪽이 없으면 설치를 확인하지 않는다"))
    parsed = PdfParser(ocr=False).parse((FIXTURES / "report.pdf").read_bytes(), "report.pdf")
    assert parsed.blocks and not parsed.assets  # 2쪽 두 줄 두 열 정렬 글자는 선 없는 표(검출기, 모델 없이)
    assert [r.fallback_reason for r in parsed.regions] == ["borderless_table"]


def test_layout_off_or_not_installed_gives_the_same_output_without_the_model(monkeypatch):
    monkeypatch.setattr(layout, "detect", lambda image: pytest.fail("모델을 돌리지 않는다"))
    data = pdf(chart_page, photo_page)
    off = PdfParser(ocr=False, layout=False).parse(data, "c.pdf")
    monkeypatch.setattr(layout, "available", lambda: False)
    assert PdfParser(ocr=False).parse(data, "c.pdf") == off
    kinds = [b["kind"] for b in off.blocks]
    assert kinds.count("figure") == 1 and "caption" not in kinds  # 사진만 그림, 캡션 짝은 모델이 있어야


def test_auto_mode_without_a_fetched_model_behaves_as_not_installed(monkeypatch):
    """리뷰 C2: 레이아웃 모듈은 있는데 모델 파일을 받지 않았으면(fetch 전) 자동 모드는 설치가 없을 때와 같다(조용히
    모델 없이). 찾은 모델 파일이 깨졌으면 크게 알린다(test_layout_runtime)."""
    real_find = models.find
    monkeypatch.setattr(models, "find", lambda name: None if name == "layout" else real_find(name))
    monkeypatch.setattr(layout, "MODULES", ())
    monkeypatch.setattr(layout, "detect", lambda image: pytest.fail("모델 파일이 없으면 모델을 돌리지 않는다"))
    data = pdf(chart_page, photo_page)
    assert layout.available() is False
    assert PdfParser(ocr=False).parse(data, "c.pdf") == PdfParser(ocr=False, layout=False).parse(data, "c.pdf")


def test_scanned_page_figure_takes_its_ocr_lines(monkeypatch):
    pytest.importorskip("onnxruntime")
    require_models("ocr-det", "ocr-rec", "ocr-rec-config")  # 레이아웃은 가짜 검출기, OCR은 실제 모델
    image = text_image(W, H, [(60, 100, 14, "스캔한 쪽의 글자를 읽는다."), (60, 400, 14, "다음 문단은 한 줄이다.")])
    data = pdf(lambda c: c.drawImage(ImageReader(image), 0, 0, width=W, height=H))
    fake_layout(monkeypatch, ("image", 0.9, (50.0, 380.0, 400.0, 440.0)))
    parsed = PdfParser().parse(data, "s.pdf")
    assert [(b["kind"], b["text_source"], b["text"]) for b in parsed.blocks] == [
        ("paragraph", "ocr", "스캔한 쪽의 글자를 읽는다."), ("figure", "ocr", "다음 문단은 한 줄이다.")]
    assert parsed.blocks[1]["figure"]["asset"] in parsed.assets


def test_parallel_parses_with_the_layout_model_give_the_same_blocks():
    """여러 스레드가 동시에 파싱해도(렌더는 PDFIUM_LOCK, 모델 세션은 공유) 결과가 같다."""
    pytest.importorskip("onnxruntime")
    require_models("layout", "layout-config")
    data = pdf(chart_page, photo_page)
    expected = PdfParser(ocr=False).parse(data, "c.pdf")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: PdfParser(ocr=False).parse(data, "c.pdf"), range(6)))
    assert expected.assets and all(r == expected for r in results)


def test_parsing_never_downloads_model_files(monkeypatch):
    """실행 중에는 아무것도 내려받지 않는다(스펙 §5): 실제 OCR·레이아웃 모델로 scanned 쪽과 차트 쪽을 파싱하는 동안
    받기 연결·fetch를 부르면 실패한다."""
    pytest.importorskip("onnxruntime")
    require_models("ocr-det", "ocr-rec", "ocr-rec-config", "layout", "layout-config")
    monkeypatch.setattr(models, "_urlopen", lambda url: pytest.fail("parse must not download"))
    monkeypatch.setattr(models, "fetch", lambda *a, **k: pytest.fail("parse must not fetch"))
    calls = []
    real = layout.detect
    monkeypatch.setattr(layout, "detect", lambda image: calls.append(image.size) or real(image))
    image = text_image(W, H, [(60, 100, 14, "스캔한 쪽의 글자를 읽는다.")])
    parsed = PdfParser().parse(pdf(lambda c: c.drawImage(ImageReader(image), 0, 0, width=W, height=H), chart_page),
                               "s.pdf")
    assert ("paragraph", "ocr", "스캔한 쪽의 글자를 읽는다.") in [(b["kind"], b["text_source"], b["text"])
                                                              for b in parsed.blocks]
    assert len(calls) == 2  # 두 쪽 모두 실제 레이아웃 모델을 돌렸다


def test_ingest_stores_the_figure_png_in_sqlite(tmp_path):
    with SqliteStore(tmp_path / "state.db") as store:
        engine = LocalEngine(store, [PdfParser(ocr=False, layout=False)])
        tree = engine.get_tree(engine.ingest_bytes(pdf(photo_page), "p.pdf").document_id)
        (fig,) = [b for b in tree.blocks if b.kind == "figure"]
        assert engine.get_asset(fig.figure.asset).startswith(b"\x89PNG\r\n\x1a\n")
        assert engine.history(tree.document_id).regions == ()


def test_background_image_behind_a_table_keeps_the_table():
    """쪽 전체 배경 이미지 위에 본문과 표(보이는 글자 50자 이상): 배경은 그림이 아니고 표·문단은 그대로."""
    paper = Image.new("RGB", (60, 85), (250, 245, 230))

    def draw(c):
        c.drawImage(ImageReader(paper), 0, 0, width=W, height=H)
        for i in range(4):
            put(c, 72, 770 - 16 * i, 11, "배경 그림 위에 쓴 본문 문장이 여러 줄 이어진다.")
        grid_table(c, 600)

    parsed = PdfParser(ocr=False, layout=False).parse(pdf(draw), "bg.pdf")
    assert parsed.pages[0].text_layer == "digital"
    assert [b["kind"] for b in parsed.blocks] == ["paragraph", "table"] and not parsed.assets


def test_small_ruled_table_on_a_shading_image_stays_a_table_without_the_model():
    """리뷰 I1: 옅은 음영 이미지(쪽 면적 약 5.9%) 위에 그린 3×3 선 있는 표(글자 9자: 배경 기준 50자 미만). 모델이
    없으면 표를 확인할 모델 table 상자도 없다: 모델이 찾지 않은 이미지 객체는 표를 버리지 않는다(A2처럼 표)."""
    shade = Image.new("RGB", (64, 18), (240, 240, 245))

    def draw(c):
        for i in range(5):
            put(c, 72, 770 - 16 * i, 11, "음영 위 표가 있는 쪽의 본문이다.")
        c.drawImage(ImageReader(shade), 137, 500, width=320, height=92)
        xs, ys = [147, 247, 347, 447], [582, 558, 534, 510]
        for y in ys:
            c.line(xs[0], y, xs[-1], y)
        for x in xs:
            c.line(x, ys[0], x, ys[-1])
        for r, row in enumerate(["가나다", "라마바", "사아자"]):
            for k, s in enumerate(row):
                put(c, xs[k] + 8, ys[r] - 17, 11, s)

    parsed = PdfParser(ocr=False, layout=False).parse(pdf(draw), "shade.pdf")
    (table,) = [b for b in parsed.blocks if b["kind"] == "table"]
    assert (table["table"].n_rows, table["table"].n_cols) == (3, 3)
    assert [c.text for c in table["table"].cells] == list("가나다라마바사아자")
    assert not parsed.regions  # 그림이 표를 버린 처리 이력이 없다
    assert "".join(b["text"] for b in parsed.blocks if b["kind"] == "figure") == ""  # 표 글자는 그림으로 가지 않는다


def test_image_boxes_are_found_once_per_page_and_no_page_is_kept_after_parsing(monkeypatch):
    """리뷰 M2: 쪽마다 wants_layout·photo_boxes·arrange가 이미지 객체 상자를 한 번만 구하고(쪽 루프 안에서 정한다),
    파싱이 끝나면 모듈이 쪽(PageText)을 붙잡아 두지 않는다."""
    counted = []
    real_counts = figures._counts
    monkeypatch.setattr(figures, "_counts",
                        lambda points, boxes: counted.append(len(boxes)) or real_counts(points, boxes))
    kept = []
    real_extract = pdf_parser.extract_pages

    def extract(data, name):
        pages = real_extract(data, name)
        kept.extend(weakref.ref(p) for p in pages)
        return pages

    monkeypatch.setattr(pdf_parser, "extract_pages", extract)
    parsed = PdfParser(ocr=False, layout=False).parse(pdf(photo_page, photo_page), "two.pdf")
    assert [b["kind"] for b in parsed.blocks].count("figure") == 2
    assert counted == [1, 1]  # 쪽마다 한 번(사진 하나)
    gc.collect()
    assert len(kept) == 2 and all(ref() is None for ref in kept)


def test_history_credits_the_model_only_for_figures_the_model_found(monkeypatch):
    """리뷰 M3: 모델이 돈 쪽이라도 모델이 찾지 않은 이미지 객체 그림은 모델 이름 없이 적는다(사유 코드는 같다)."""
    def chart_and_photo(c):
        for i in range(12):
            c.line(100, 580 + 10 * i, 500, 580 + 10 * i)
        c.drawImage(ImageReader(RED), 150, 100, width=300, height=150)  # 모델이 찾지 않는 사진

    fake_layout(monkeypatch, CHART)
    monkeypatch.setattr(figures, "ASSET_LIMIT", 0)
    parsed = PdfParser(ocr=False).parse(pdf(chart_and_photo), "cp.pdf")
    assert [(r.region_id, r.attempts[0].model_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-figure-1", "PP-DocLayout_plus-L", "figure_image_not_stored"),
        ("p1-figure-2", None, "figure_image_not_stored")]


def test_a_page_needing_ocr_the_model_and_a_figure_crop_is_rendered_once(monkeypatch):
    """리뷰 M9: 쪽 렌더는 쪽마다 한 번. scanned 쪽은 OCR 줄·레이아웃 상자·그림 잘라내기가 같은 렌더를 쓰고, 사진이
    있는 digital 쪽도 한 번 그린다. OCR은 가짜 줄(onnxruntime 없이)."""
    renders = []
    real_render = scan.render

    def render(data, name, index):
        image = real_render(data, name, index)
        renders.append((index, image))
        return image

    read = []
    monkeypatch.setattr(scan, "render", render)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "get_reader", lambda: object())
    monkeypatch.setattr(scan, "page_lines", lambda image, page: read.append(image) or [
        OcrText("스캔한 쪽의 글자를 읽는다.", 0.9, 60, 100, 300, 114)])
    fake_layout(monkeypatch, ("image", 0.9, (50.0, 380.0, 400.0, 440.0)))
    detected = []
    fake_detect = layout.detect
    monkeypatch.setattr(layout, "detect", lambda image: detected.append(image) or fake_detect(image))
    scanned = text_image(W, H, [(60, 100, 14, "스캔한 쪽의 글자를 읽는다.")])
    parsed = PdfParser().parse(pdf(lambda c: c.drawImage(ImageReader(scanned), 0, 0, width=W, height=H), photo_page),
                               "s.pdf")
    assert [p.text_layer for p in parsed.pages] == ["scanned", "digital"]
    assert [index for index, _ in renders] == [0, 1]  # 쪽마다 한 번
    assert len(read) == 1 and read[0] is renders[0][1]  # OCR은 scanned 쪽 렌더 그대로
    assert len(detected) == 2 and all(d is r for d, (_, r) in zip(detected, renders, strict=True))
    first = [b for b in parsed.blocks if b["locator"]["page"] == 1]
    assert [(b["kind"], b["text_source"]) for b in first] == [("paragraph", "ocr"), ("figure", "ocr")]
    assert first[1]["figure"]["asset"] in parsed.assets


def rough_chart(c: Canvas) -> None:
    """chart_page에 차트 상자 안 이름표 하나를 더한 쪽: 그림 이미지 바이트가 chart_page 그림과 다르다."""
    chart_page(c)
    put(c, 400, 560, 9, "3분기")


def test_unreliable_page_keeps_capped_figures_and_takes_only_the_asset_budget_left(monkeypatch):
    """unreliable 쪽도 그림·짝 캡션(이미지 포함)을 내고 그 쪽 블록은 모두 신뢰도 0.2 이하다. 그 쪽 그림 이미지는
    digital·scanned 쪽 그림을 다 담은 뒤 남은 예산만 쓴다: 앞 unreliable 쪽 때문에 뒤 digital 쪽 그림의 이미지·처리
    이력이 바뀌지 않는다(digital 쪽 명세는 caption_ref를 가리키는 캡션 글자로 맞춰 보면 앞 쪽이 빈 쪽일 때와 같다)."""
    fake_layout(monkeypatch, CHART, CAPTION)

    def parse(data, states):
        order = iter(states)
        monkeypatch.setattr(pdf_parser, "classify", lambda stats: next(order))
        return PdfParser(ocr=False).parse(data, "m.pdf")

    def second(parsed):
        """둘째 쪽 명세(caption_ref는 가리키는 캡션 글자로)와 처리 이력."""
        blocks = [{**b, "figure": {**b["figure"], "caption_ref": parsed.blocks[b["figure"]["caption_ref"]]["text"]}}
                  if "caption_ref" in b.get("figure", {}) else b for b in parsed.blocks]
        return [b for b in blocks if b["locator"]["page"] == 2], [r for r in parsed.regions if r.locator.page == 2]

    data = pdf(rough_chart, chart_page)
    full = parse(data, ["unreliable", "digital"])
    rough = [b for b in full.blocks if b["locator"]["page"] == 1]
    assert [b["kind"] for b in rough] == ["paragraph", "figure", "caption", "paragraph"]
    assert {b["confidence"] for b in rough} == {0.2} and len(full.assets) == 2
    tree = build_tree(full, "m", 1, source("m.pdf", 2))
    assert tree.blocks[1].figure.caption_block_id == tree.blocks[2].block_id  # 그 쪽 그림도 짝 캡션을 가리킨다
    monkeypatch.setattr(figures, "ASSET_LIMIT", max(len(png) for png in full.assets.values()))  # 그림 하나만 들어간다
    alone = parse(pdf(lambda c: None, chart_page), ["digital", "digital"])
    capped = parse(data, ["unreliable", "digital"])
    assert second(capped) == second(alone) and "figure" in second(alone)[0][1] and not second(alone)[1]
    assert "figure" not in capped.blocks[1] and {b["confidence"] for b in capped.blocks[:4]} == {0.2}
    assert [(r.region_id, r.fallback_reason) for r in capped.regions] == [
        ("p1-unreliable-text-layer", "unreliable_text_layer_kept"), ("p1-figure-1", pdf_parser.NO_IMAGE)]
    for parsed in (full, capped):  # 글자 장부: 이미지가 있든 없든 그림·캡션·문단 글자가 쪽마다 정확히 한 번
        assert [(p.coverage.in_blocks, p.coverage.hidden) for p in parsed.pages] == [
            (p.text_stats.chars, 0) for p in parsed.pages]
