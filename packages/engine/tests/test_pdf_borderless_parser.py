"""선 없는 표의 파서 연결(B2): 렌더하지 않는 쪽의 검출기, 모델 게이트(표 밖 긴 가로선), 시도하지 않는 쪽(scan 모드·
회전 쪽)의 처리 이력, 신뢰도 0.4·처리 이력 borderless_table, 글자 장부, 문서 구조(본문 크기·제목·section_path), 섞인
문서. 레이아웃 모델 상자는 바꿔 끼운 가짜 검출기로 준다(onnxruntime 없이). 글자는 모두 지어낸 합성 글자다."""

import io
import unicodedata
from collections import Counter

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hanji.formats.pdf import PdfParser, borderless, figures, layout
from hanji.formats.pdf import parser as pdf_parser
from hanji.formats.pdf.extract import extract_pages
from hanji.formats.pdf.layout import LayoutBox
from hanji.formats.pdf.tables import find_tables

FONT = "HYGothic-Medium"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))
W, H = 595.0, 842.0
GRAY = Image.new("RGB", (40, 30), (128, 128, 128))


def put(c: Canvas, x: float, baseline: float, s: str, size: float = 10.0) -> None:
    c.setFont(FONT, size)
    c.drawString(x, H - baseline, s)


def rule(c: Canvas, y: float, x0: float, x1: float) -> None:
    c.line(x0, H - y, x1, H - y)


def rows_at(c: Canvas, rows, xs, top: float, pitch: float = 18.0, size: float = 10.0) -> None:
    for i, row in enumerate(rows):
        for x, s in zip(xs, row):
            if s:
                put(c, x, top + pitch * i, s, size)


def pdf(*pages, rotation: int = 0) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(W, H), invariant=1, pageCompression=0)
    for draw in pages:
        if rotation:
            c.setPageRotation(rotation)
        draw(c)
        c.showPage()
    c.save()
    return buf.getvalue()


def glyphs(texts) -> Counter:
    return Counter(ch for t in texts for ch in unicodedata.normalize("NFC", t) if not ch.isspace())


def fake_layout(monkeypatch, *found) -> list:
    """설치가 있는 것처럼 하고 detect가 (분류, 점수, 보이는 쪽 pt 상자)를 쪽 렌더 화소로 돌려준다(A4 바로 선 쪽)."""
    calls = []

    def detect(image):
        calls.append(image.size)
        sx, sy = image.width / W, image.height / H
        return [LayoutBox(cls, score, (x0 * sx, y0 * sy, x1 * sx, y1 * sy)) for cls, score, (x0, y0, x1, y1) in found]

    monkeypatch.setattr(layout, "available", lambda: True)
    monkeypatch.setattr(layout, "get_detector", lambda: object())
    monkeypatch.setattr(layout, "detect", detect)
    return calls


TABLE = [["구분", "2024", "2025"], ["수입", "120", "135"], ["지출", "98", "110"], ["잔액", "22", "25"]]


def report(c: Canvas) -> None:
    """제목·문단·선 없는 4×3 표·문단. 선도 그림도 없다(렌더하지 않는 쪽)."""
    put(c, 72, 80, "1. 수지 현황", 14)
    put(c, 72, 100, "올해 수지는 아래와 같다.")
    rows_at(c, TABLE, (72, 202, 332), 130)
    put(c, 72, 220, "잔액은 전년보다 늘었다.")


def test_page_without_paths_or_images_gets_a_borderless_table_without_the_model(monkeypatch):
    monkeypatch.setattr(layout, "available", lambda: pytest.fail("모델을 돌릴 쪽이 없으면 설치를 확인하지 않는다"))
    data = pdf(report)
    parsed = PdfParser(ocr=False).parse(data, "t.pdf")
    (chars,) = (p.chars for p in extract_pages(data, "t.pdf"))
    assert glyphs(b["text"] for b in parsed.blocks) == glyphs(c.text for c in chars)
    assert [(b["kind"], b["confidence"]) for b in parsed.blocks] == [
        ("heading", 0.6), ("paragraph", 0.7), ("table", 0.4), ("paragraph", 0.7)]
    table = parsed.blocks[2]["table"]
    assert [[c.text for c in table.cells if c.row == r] for r in range(table.n_rows)] == TABLE
    (note,) = parsed.regions
    assert (note.region_id, note.fallback_reason, note.locator.bbox.model_dump()) == (
        "p1-borderless-rule-1", "borderless_table", parsed.blocks[2]["locator"]["bbox"])
    (page,) = parsed.pages
    assert page.coverage.in_blocks == page.text_stats.chars and page.coverage.hidden == 0


def test_unreliable_page_keeps_its_borderless_table_under_the_page_cap(monkeypatch):
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    parsed = PdfParser(ocr=False, layout=False).parse(pdf(report), "t.pdf")
    assert [(b["kind"], b["confidence"]) for b in parsed.blocks] == [
        ("list_item", 0.2), ("paragraph", 0.2), ("table", 0.2), ("paragraph", 0.2)]  # unreliable 쪽에는 제목이 없다
    assert [r.fallback_reason for r in parsed.regions] == ["unreliable_text_layer_kept", "borderless_table"]
    assert parsed.pages[0].coverage is not None


def booktabs_page(c: Canvas) -> None:
    rule(c, 120, 72, 400)
    rows_at(c, TABLE, (72, 210, 320), 135)
    rule(c, 140, 72, 400)
    rule(c, 195, 72, 400)


def margin_rules_page(c: Canvas) -> None:
    rule(c, 60, 72, 523)
    put(c, 72, 100, "머리말 선과 꼬리말 선만 있는 쪽이다.")
    rule(c, 780, 72, 523)


def ruled_table_page(c: Canvas) -> None:
    xs, ys = (72, 172, 272, 372), (100, 120, 140, 160)
    for y in ys:
        rule(c, y, xs[0], xs[-1])
    for x in xs:
        c.line(x, H - ys[0], x, H - ys[-1])
    rows_at(c, [["가", "나", "다"], ["라", "마", "바"], ["사", "아", "자"]], (80, 180, 280), 114, 20)


def split_rule_page(c: Canvas) -> None:
    """같은 높이(± SNAP)의 긴 선 조각 셋은 한 줄, 다른 높이에 하나 더: 긴 가로선 2개."""
    rule(c, 100, 72, 300)
    rule(c, 100.8, 320, 523)
    rule(c, 101, 72, 260)
    rule(c, 200, 72, 523)


@pytest.mark.parametrize("draw,count,wanted", [(booktabs_page, 3, True), (margin_rules_page, 2, False),
                                               (ruled_table_page, 0, False), (split_rule_page, 2, False)])
def test_model_gate_counts_long_rules_outside_ruled_tables(draw, count, wanted):
    (page,) = extract_pages(pdf(draw), "t.pdf")
    found = find_tables(page)
    assert page.paths < figures.LAYOUT_MIN_PATHS and not figures.photo_boxes(page)
    assert figures.table_rules(page, found) == count
    assert figures.wants_layout(page, "layer", found) is wanted


def test_page_with_three_long_rules_runs_the_model(monkeypatch):
    calls = fake_layout(monkeypatch)
    parsed = PdfParser(ocr=False).parse(pdf(booktabs_page, margin_rules_page, ruled_table_page), "t.pdf")
    assert len(calls) == 1  # 첫 쪽(booktabs)만
    assert [(b["locator"]["page"], b["kind"]) for b in parsed.blocks if b["kind"] == "table"] == [(1, "table"),
                                                                                                    (3, "table")]
    assert [b["confidence"] for b in parsed.blocks if b["kind"] == "table"] == [0.4, 0.6]


def test_model_table_boxes_reach_settle_through_the_parser(monkeypatch):
    """바로 선 layer 쪽의 모델 table 상자는 settle로 간다: 표를 덮는 상자는 선 없는 표(모델 이름), 글자 없는 상자는
    복원 실패 기록. 시도한 쪽이라 LAYOUT_TABLE 기록은 없다."""
    fake_layout(monkeypatch, ("table", 0.9, (60.0, 110.0, 420.0, 205.0)), ("table", 0.9, (60.0, 400.0, 400.0, 500.0)))
    parsed = PdfParser(ocr=False).parse(pdf(booktabs_page), "t.pdf")
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-borderless-layout-1", "borderless_table"), ("p1-borderless-failed-1", "borderless_table_failed")]
    assert parsed.regions[0].attempts[0].model_id == layout.MODEL_ID == "PP-DocLayout_plus-L"
    assert not any("layout-table" in r.region_id for r in parsed.regions)
    assert [(b["kind"], b["confidence"]) for b in parsed.blocks] == [("table", 0.4)]
    table = parsed.blocks[0]["table"]
    assert [[c.text for c in table.cells if c.row == r] for r in range(table.n_rows)] == TABLE
    (page,) = parsed.pages
    assert page.coverage.in_blocks == page.text_stats.chars and page.coverage.hidden == 0



@pytest.mark.parametrize("baseline", [120.0, 130.0])
def test_table_and_paragraph_with_the_same_top_keep_one_order(monkeypatch, baseline):
    """윗변이 같은 선 없는 표와 오른쪽 문단: 표 윗변(소수 셋째 자리 bbox)과 조각 윗변을 같은 자리수로 견줘
    반올림 방향과 상관없이 늘 표가 먼저다(기존 규칙: 표 윗변 ≤ 조각 윗변이면 표 먼저)."""
    def draw(c: Canvas) -> None:
        rows_at(c, TABLE, (72, 202, 332), baseline)
        put(c, 450, baseline, "오른쪽 설명 글")
        for y in (700, 720, 740):  # 긴 가로선 3개: 모델 게이트를 켠다(표와 떨어져 있다)
            rule(c, y, 72, 523)

    top = baseline - 15
    fake_layout(monkeypatch, ("table", 0.9, (60.0, top, 400.0, top + 80)))
    parsed = PdfParser(ocr=False).parse(pdf(draw), "t.pdf")
    blocks = [(b["kind"], b["locator"]["bbox"]["y0"]) for b in parsed.blocks]
    assert [k for k, _ in blocks] == ["table", "paragraph"]
    assert blocks[0][1] == blocks[1][1]  # 두 블록 윗변이 같다(블록 상자는 소수 셋째 자리)
    assert [r.region_id for r in parsed.regions] == ["p1-borderless-layout-1"]

def test_gate_on_without_the_layout_install_still_gives_the_detector_table(monkeypatch):
    """긴 가로선 3개로 게이트가 켜져도 레이아웃 추가 설치가 없으면 모델 없이 검출기가 표를 찾는다."""
    monkeypatch.setattr(layout, "available", lambda: False)
    monkeypatch.setattr(layout, "detect", lambda image: pytest.fail("설치가 없으면 모델을 돌리지 않는다"))
    parsed = PdfParser(ocr=False).parse(pdf(booktabs_page), "t.pdf")
    assert [(b["kind"], b["confidence"]) for b in parsed.blocks] == [("table", 0.4)]
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [("p1-borderless-rule-1", "borderless_table")]


def lines_page(c: Canvas) -> None:
    """선 12개(path ≥ LAYOUT_MIN_PATHS)와 정렬된 글자."""
    for i in range(12):
        rule(c, 600 + 10 * i, 72, 300)
    rows_at(c, TABLE, (72, 202, 332), 400)  # 돌린 쪽은 MediaBox가 눕는다: 그 안에 그린다


def scanned_page(c: Canvas) -> None:
    """쪽의 30%를 덮는 그림과 글자 몇 개(scanned)."""
    c.drawImage(ImageReader(GRAY), 97.5, 300, width=400, height=370)
    rows_at(c, [["가", "나"], ["다", "라"]], (72, 200), 100)


@pytest.mark.parametrize("draw,rotation,state", [(lines_page, 90, "digital"), (scanned_page, 0, "scanned")])
def test_turned_and_scan_mode_pages_keep_the_layout_table_note(monkeypatch, draw, rotation, state):
    fake_layout(monkeypatch, ("table", 0.9, (60.0, 90.0, 400.0, 200.0)))
    parsed = PdfParser(ocr=False).parse(pdf(draw, rotation=rotation), "t.pdf")
    assert parsed.pages[0].text_layer == state
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-layout-table-1", "layout_table_not_attempted")]
    assert "table" not in [b["kind"] for b in parsed.blocks]


def small_table_report(c: Canvas) -> None:
    """9pt 표 글자가 문서 글자의 대부분: 표 글자를 본문 크기에서 빼야 11pt 문단이 제목이 되지 않는다."""
    put(c, 72, 80, "1. 수지 현황", 13)
    put(c, 72, 100, "올해 수지는 아래 표와 같다.", 11)
    put(c, 72, 114, "단위는 백만원이다.", 11)
    rows = [["구분", "상반기", "하반기", "합계"]] + [[f"항목{k}", f"{10 + k}", f"{20 + k}", f"{30 + 2 * k}"]
                                                  for k in range(1, 8)]
    rows_at(c, rows, (72, 180, 280, 380), 140, 14, 9)
    put(c, 72, 270, "2. 다음 과제", 13)
    put(c, 72, 290, "다음 과제는 따로 적는다.", 11)


def table_only(c: Canvas) -> None:
    rows_at(c, TABLE, (72, 202, 332), 130)


def test_borderless_table_chars_leave_body_size_headings_and_section_paths_to_the_text():
    parsed = PdfParser(ocr=False, layout=False).parse(pdf(small_table_report, table_only), "t.pdf")
    assert [(b["locator"]["page"], b["kind"], b.get("level"), b["section_path"]) for b in parsed.blocks] == [
        (1, "heading", 1, ()), (1, "paragraph", None, ("1. 수지 현황",)), (1, "table", None, ("1. 수지 현황",)),
        (1, "heading", 1, ()), (1, "paragraph", None, ("2. 다음 과제",)), (2, "table", None, ("2. 다음 과제",))]
    assert parsed.blocks[1]["text"] == "올해 수지는 아래 표와 같다.\n단위는 백만원이다."
    assert [b["table"].n_rows for b in parsed.blocks if b["kind"] == "table"] == [8, 4]


def noisy(c: Canvas) -> None:
    """작은 글자 30줄(본문 크기를 흔든다), 큰 글자 한 줄(제목 단계를 흔든다), 정렬된 표."""
    put(c, 72, 60, "깨진 큰 글자", 24)
    for i in range(30):
        put(c, 72, 100 + 12 * i, "작은 글자가 아주 많이 적힌 깨진 줄이다", 8)
    rows_at(c, TABLE, (72, 202, 332), 500)


def test_unreliable_page_does_not_change_the_borderless_table_page_before_it(monkeypatch):
    before = PdfParser(ocr=False, layout=False).parse(pdf(small_table_report, lambda c: None), "t.pdf")
    states = iter(["digital", "unreliable"])
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: next(states))
    after = PdfParser(ocr=False, layout=False).parse(pdf(small_table_report, noisy), "t.pdf")
    assert [b for b in after.blocks if b["locator"]["page"] == 1] == list(before.blocks)
    rough = [(b["kind"], b["confidence"]) for b in after.blocks if b["locator"]["page"] == 2]
    assert ("table", 0.2) in rough and {conf for _, conf in rough} == {0.2}


def test_parse_leaves_no_page_text_in_the_borderless_cache(monkeypatch):
    seen = []
    real = borderless._context

    def context(page):
        seen.append(page.page)
        return real(page)

    monkeypatch.setattr(borderless, "_context", context)
    PdfParser(ocr=False, layout=False).parse(pdf(report), "t.pdf")
    assert seen and borderless._last is None  # 쪽을 훑었고, 파싱이 끝나면 쪽 글자를 붙들지 않는다
