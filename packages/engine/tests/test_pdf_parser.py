import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hanji import LocalEngine, MemoryStore
from hanji.errors import OcrUnavailable, ParseError
from hanji.formats.detect import default_parsers, detect_parser
from hanji.formats.pdf import PdfParser, extract, group, ocr, scan
from hanji.formats.pdf import parser as pdf_parser
from hanji.formats.pdf.ocr import OcrLine
from hanji.formats.pdf.tables import TableSpec
from hanji_contracts import BBox, Cell, Table, TextCoverage

FONT = "HYGothic-Medium"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))
GRAY_JPEG = bytes.fromhex(  # 8×8 회색 JPEG
    "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123251d283a333d"
    "3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffc0000b080008000801011100ffc40014000100"
    "000000000000000000000000000005ffc40014100100000000000000000000000000000000ffda0008010100003f0041ffd9")


def make_pdf(pages: list[list[tuple[float, str, int]]], **kw) -> bytes:
    """쪽마다 (기준선 y, 글자, 렌더 모드) 줄 목록. 11pt, x=72."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(595.0, 842.0), invariant=1, pageCompression=0, **kw)
    for lines in pages:
        for y, s, mode in lines:
            c.saveState()
            t = c.beginText(72, y)
            t.setFont(FONT, 11)
            t.setTextRenderMode(mode)
            t.textOut(s)
            c.drawText(t)
            c.restoreState()
        c.showPage()
    c.save()
    return buf.getvalue()


def draw_pdf(draw, rotation: int = 0) -> bytes:
    """한 쪽짜리 PDF. rotation은 /Rotate."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(595.0, 842.0), invariant=1, pageCompression=0)
    c.setPageRotation(rotation)
    draw(c)
    c.showPage()
    c.save()
    return buf.getvalue()


def put(c: Canvas, x: float, y: float, size: float, s: str, mode: int = 0) -> None:
    c.saveState()
    t = c.beginText(x, y)
    t.setFont(FONT, size)
    t.setTextRenderMode(mode)
    t.textOut(s)
    c.drawText(t)
    c.restoreState()


def kinds_texts(parsed) -> list[tuple[str, str]]:
    return [(b["kind"], b["text"]) for b in parsed.blocks]


def write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


PARAS = [[(770, "첫째 문단이다.", 0), (730, "둘째 문단이다.", 0), (690, "셋째 문단이다.", 0)]]


def test_registered_for_pdf_extension_case_insensitive():
    parser = detect_parser("보고서.PDF", default_parsers())
    assert isinstance(parser, PdfParser) and parser.mimes == ("application/pdf",)


def test_pages_carry_state_stats_and_render_dpi():
    data = make_pdf([[(770, "보이는 쪽이다.", 0)], [(770, "숨은 글자층이다.", 3)]])
    parsed = PdfParser().parse(data, "a.pdf")
    assert parsed.mime == "application/pdf"
    assert [(p.page, p.text_layer, p.render_dpi, p.rotation) for p in parsed.pages] == [
        (1, "digital", 144, 0), (2, "scanned", 144, 0)]
    assert parsed.pages[0].text_stats.chars == 7 and parsed.pages[1].text_stats.invisible_ratio == 1.0
    assert [(b["text"], b["locator"]["page"]) for b in parsed.blocks] == [("보이는 쪽이다.", 1)]


def test_engine_ingest_pdf_source_and_page_count(tmp_path):
    engine = LocalEngine(MemoryStore())
    tree = engine.get_tree(engine.ingest(str(write(tmp_path / "보고서.pdf", make_pdf(PARAS * 2)))).document_id)
    assert (tree.source.name, tree.source.mime, tree.source.page_count) == ("보고서.pdf", "application/pdf", 2)
    assert len(tree.pages) == 2 and len(tree.blocks) == 6


@pytest.mark.parametrize("data,reason", [(make_pdf(PARAS, encrypt="secret"), "encrypted PDF"),
                                         (b"%PDF-1.7\n%%EOF\n", "invalid PDF")])
def test_encrypted_or_corrupt_pdf_leaves_store_untouched(tmp_path, data, reason):
    engine = LocalEngine(MemoryStore())
    with pytest.raises(ParseError, match=reason) as info:
        engine.ingest(str(write(tmp_path / "깨짐.pdf", data)))
    assert info.value.location == "깨짐.pdf"
    assert engine.documents() == () and engine.changes(None).changes == ()


def test_reingest_keeps_block_ids(tmp_path):
    path = write(tmp_path / "a.pdf", make_pdf(PARAS))
    engine = LocalEngine(MemoryStore())
    first = engine.ingest(str(path), document_id="d")
    v1 = engine.get_tree("d")
    assert engine.ingest(str(path), document_id="d") == first
    assert engine.ingest(str(path), document_id="d", force=True) == first  # 다시 파싱해도 블록이 같다
    write(path, make_pdf([[(770, "첫째 문단이다.", 0), (730, "고친 둘째 문단이다.", 0), (690, "셋째 문단이다.", 0)]]))
    assert engine.ingest(str(path), document_id="d").version == 2
    v2 = engine.get_tree("d")
    change = engine.changes(1).changes[0]
    assert (v2.blocks[0].block_id, v2.blocks[2].block_id) == (v1.blocks[0].block_id, v1.blocks[2].block_id)
    assert change.added == (v2.blocks[1].block_id,) and change.removed == (v1.blocks[1].block_id,)
    assert change.updated == ()


def test_page_state_change_without_block_change_is_a_new_version(tmp_path):
    path = write(tmp_path / "a.pdf", make_pdf([[(770, "숨은 글자층이다.", 3)]]))
    engine = LocalEngine(MemoryStore(), default_parsers(layout=False))  # 레이아웃을 켜면 회색 사각형이 그림 블록이 된다
    engine.ingest(str(path), document_id="d")
    write(path, draw_pdf(lambda c: (put(c, 72, 770, 11, "다른 숨은 글자층이다.", 3),
                                    c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 72, 72, width=100, height=100))))
    ref = engine.ingest(str(path), document_id="d")
    assert ref.version == 2 and engine.get_tree("d").pages[0].text_stats.max_image_coverage > 0
    change = engine.changes(1).changes[0]
    assert (change.added, change.updated, change.removed) == ((), (), ())


def test_parsing_from_many_threads_at_once_is_safe():
    """PDFium은 문서가 달라도 동시에 부르면 프로세스가 죽는다. 패키지 잠금으로 한 번에 하나씩 부른다."""
    data = make_pdf(PARAS * 3)
    expected = PdfParser().parse(data, "a.pdf")
    start = threading.Barrier(8)

    def run(_):
        start.wait()
        return [PdfParser().parse(data, "a.pdf") for _ in range(30)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [r for rs in pool.map(run, range(8)) for r in rs]
    assert len(results) == 240 and all(r == expected for r in results)


def test_extract_waits_for_the_package_pdfium_lock():
    """잠금은 패키지에 하나(쪽 그림 렌더러도 같이 쓴다). 다른 스레드가 잡고 있으면 추출은 기다린다."""
    data = make_pdf(PARAS)
    done = threading.Event()
    worker = threading.Thread(target=lambda: (extract.extract_pages(data, "a.pdf"), done.set()))
    with extract.PDFIUM_LOCK:
        worker.start()
        assert not done.wait(0.3)
    worker.join(10)
    assert done.is_set()


def test_clipped_logo_does_not_make_a_titled_page_scanned():
    """쪽 크기 그림을 40×40으로 잘라 보이는 로고: 그림 면적은 보이는 부분(클리핑 영역)만 센다."""
    def draw(c):
        c.saveState()
        clip = c.beginPath()
        clip.rect(50, 700, 40, 40)
        c.clipPath(clip, stroke=0, fill=0)
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 0, 0, width=595, height=842)
        c.restoreState()
        put(c, 72, 600, 18, "디지털 문서 제목과 부제")

    parsed = PdfParser().parse(draw_pdf(draw), "logo.pdf")
    (page,) = parsed.pages
    assert page.text_layer == "digital"
    assert page.text_stats.max_image_coverage == pytest.approx(1600 / (595 * 842), abs=1e-4)
    assert kinds_texts(parsed) == [("paragraph", "디지털 문서 제목과 부제")]


def test_scanned_image_page_keeps_visible_text_as_blocks():
    """scanned는 '그림 속 글자는 OCR이 필요하다'는 뜻: 보이는 글자(쪽 번호)는 블록으로 남는다."""
    def draw(c):
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 97.5, 300, width=400, height=370)
        put(c, 282, 30, 9, "- 3 -")

    parsed = PdfParser(layout=False).parse(draw_pdf(draw), "scan.pdf")  # 레이아웃은 회색 사각형을 그림으로 본다
    assert parsed.pages[0].text_layer == "scanned"
    assert [(b["text"], b["text_source"], b["locator"]["page"]) for b in parsed.blocks] == [("- 3 -", "text_layer", 1)]


def test_scanned_page_drops_only_invisible_ocr_text():
    only_ocr = PdfParser().parse(make_pdf([[(770, "숨은 글자층이다.", 3), (750, "보이지 않는다.", 3)]]), "a.pdf")
    assert only_ocr.pages[0].text_layer == "scanned" and only_ocr.blocks == ()
    mixed = PdfParser().parse(make_pdf([[(770, "숨은 글자층이다.", 3), (750, "보이지 않는다.", 3),
                                         (730, "보이는 글자.", 0)]]), "a.pdf")
    assert mixed.pages[0].text_layer == "scanned" and kinds_texts(mixed) == [("paragraph", "보이는 글자.")]


@pytest.mark.parametrize("mode,lost", [(3, False), (0, True)])
def test_single_glyph_invisible_ocr_text_is_not_lost_text_without_hangul_font(monkeypatch, mode, lost):
    """한글 글꼴 없는 컴퓨터(아래는 그 흉내: '가'를 못 그리고 PDFium 텍스트 쪽이 한 글자 객체를 뺀다)의 스캔 쪽.
    숨은 OCR 글자(렌더 모드 3)는 어차피 버리므로 빠져도 잃은 글자가 아니다. 보이는 글자가 빠지면 여전히 실패."""
    def draw(c):
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 0, 0, width=595, height=842)
        put(c, 72, 770, 11, "가", mode=mode)

    monkeypatch.setattr(extract, "_draws_hangul", lambda pdf, font: False)
    monkeypatch.setattr(extract.pdfium_c, "FPDFText_CountChars", lambda textpage: 0)
    if lost:
        with pytest.raises(ParseError, match="no Hangul glyphs"):
            PdfParser().parse(draw_pdf(draw), "scan.pdf")
    else:
        parsed = PdfParser().parse(draw_pdf(draw), "scan.pdf")
        assert parsed.pages[0].text_layer == "scanned" and parsed.blocks == ()


def two_lines(c):
    """공백 글자 없이 4pt 띄운 두 낱말 + 아랫줄(11pt). reportlab은 90·270도면 MediaBox를 가로로 눕히므로 아래쪽에 쓴다."""
    put(c, 72, 500, 11, "회전")
    put(c, 72 + 22 + 4, 500, 11, "글자")
    put(c, 72, 484, 11, "둘째 줄")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_page_text_comes_out_in_reading_order(rotation):
    parsed = PdfParser().parse(draw_pdf(two_lines, rotation), "rot.pdf")
    assert kinds_texts(parsed) == [("paragraph", "회전 글자\n둘째 줄")]
    box = parsed.blocks[0]["locator"]["bbox"]
    wide = (box["x1"] - box["x0"]) * parsed.pages[0].width_pt > (box["y1"] - box["y0"]) * parsed.pages[0].height_pt
    assert wide == (rotation in (0, 180))  # 상자는 보이는 쪽 기준


def test_negative_font_size_and_mirrored_text_keep_reading_order():
    """음수 Tf(180° 뒤집힘)와 거울 행렬(진행이 왼쪽)은 진행 방향을 따라 읽는다. 뒤집힌 글자는 쪽을 돌려 읽으므로
    PDF에서 아래에 있는 줄이 먼저다."""
    def negative(c):
        t = c.beginText(300, 400)
        t.setFont(FONT, -11)
        t.textOut("가나 다라")
        c.drawText(t)
        t = c.beginText(300, 300)
        t.setFont(FONT, -11)
        t.textOut("마바")
        c.drawText(t)
        t = c.beginText(300 - 22 - 4, 300)  # 공백 글자 없이 4pt 띄운 다음 낱말
        t.setFont(FONT, -11)
        t.textOut("사아")
        c.drawText(t)

    def mirrored(c):
        c.saveState()
        c.transform(-1, 0, 0, 1, 595, 0)
        put(c, 300, 400, 11, "가나 다라")
        c.restoreState()

    assert [t for _, t in kinds_texts(PdfParser().parse(draw_pdf(negative), "n.pdf"))] == ["마바 사아", "가나 다라"]
    assert [t for _, t in kinds_texts(PdfParser().parse(draw_pdf(mirrored), "m.pdf"))] == ["가나 다라"]


def test_parser_hands_each_page_mode_to_the_block_builder(monkeypatch):
    """파서는 쪽 상태와 함께 쪽마다 처리 모드를 블록 명세에 넘긴다(digital → layer, scanned → scan)."""
    seen = []
    build = pdf_parser.build_page_specs

    def spy(*args, **kw):
        seen.append(kw["modes"])
        return build(*args, **kw)

    monkeypatch.setattr(pdf_parser, "build_page_specs", spy)
    parsed = PdfParser(ocr=False, layout=False).parse(
        make_pdf([PARAS[0], [(770, "숨은 글자층이다.", 3), (30, "- 2 -", 0)]]), "a.pdf")
    assert [p.text_layer for p in parsed.pages] == ["digital", "scanned"] and seen == [["layer", "scan"]]


@pytest.mark.parametrize("setting", [False, None])
def test_unreliable_page_keeps_its_text_layer_when_ocr_is_off_or_not_installed(monkeypatch, setting):
    """OCR을 끄거나(--no-ocr) 추가 설치가 없으면 unreliable 쪽은 digital과 같은 경로로 깨진 글자층에서 블록을 만들고
    (신뢰도 0.2 이하) 쪽마다 처리 이력 한 줄을 남긴다. 같은 문서의 digital 쪽 블록은 그 쪽이 빈 쪽일 때와 같다."""
    monkeypatch.setattr(ocr, "available", lambda: False)
    before = PdfParser(ocr=setting, layout=False).parse(make_pdf([PARAS[0], [], PARAS[0]]), "a.pdf")
    states = iter(["digital", "unreliable", "digital"])
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: next(states))
    after = PdfParser(ocr=setting, layout=False).parse(
        make_pdf([PARAS[0], [(770, "깨진 쪽 글자다.", 0)], PARAS[0]]), "a.pdf")
    assert [p.text_layer for p in after.pages] == ["digital", "unreliable", "digital"]
    assert [b for b in after.blocks if b["locator"]["page"] != 2] == list(before.blocks)
    assert [(b["text"], b["confidence"]) for b in after.blocks if b["locator"]["page"] == 2] == [("깨진 쪽 글자다.", 0.2)]
    (note,) = after.regions
    assert (note.region_id, note.kind, note.chosen, note.fallback_reason) == (
        "p2-unreliable-text-layer", "paragraph", "det", "unreliable_text_layer_kept")
    assert (note.locator.page, note.locator.bbox) == (2, BBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0))
    assert note.attempts[0].model_id is None


@pytest.mark.parametrize("ocr", [False, None])
def test_coverage_counts_hidden_chars_on_digital_and_scanned_pages(ocr):
    """digital 쪽의 숨은 글자도 hidden이다(쪽 상태와 상관없이 버린다). OCR 문단은 글자층 글자가 아니라 세지 않는다.
    글자가 없는 쪽은 0."""
    data = make_pdf([[(770, "보이는 쪽이다.", 0), (740, "숨은", 3)], [(770, "숨은 글자층이다.", 3), (30, "- 2 -", 0)], []])
    parsed = PdfParser(ocr=ocr, layout=False).parse(data, "a.pdf")
    assert [p.text_layer for p in parsed.pages] == ["digital", "scanned", "digital"]
    assert [p.coverage for p in parsed.pages] == [TextCoverage(layer_chars=9, in_blocks=7, hidden=2),
                                                  TextCoverage(layer_chars=11, in_blocks=3, hidden=8),
                                                  TextCoverage(layer_chars=0, in_blocks=0, hidden=0)]
    assert not parsed.regions


def gate_numbers(note) -> dict[str, tuple[bool, float | None, str | None]]:
    """coverage_mismatch 처리 이력의 장부 숫자(검사 이름 → (통과, 값, 기준)). 게이트는 늘 실패다."""
    assert note.gate is not None and not note.gate.passed
    return {c.name: (c.passed, c.value, c.threshold) for c in note.gate.checks}


def test_lost_chars_leave_no_coverage_and_a_history_note(monkeypatch):
    """구조 문단으로도 살리지 못한 글자가 있으면(여기서는 fragments가 불릴 때마다 마지막 줄 조각을 잃어 구조 문단
    호출도 그 줄을 내지 못한다) 장부가 맞지 않는다: 파싱은 실패하지 않고, 그 쪽 coverage는 None이며 처리 이력에
    coverage_mismatch 한 줄만 남는다(살린 글자가 없어 unassigned_text는 없다)."""
    fragments = group.fragments
    monkeypatch.setattr(group, "fragments", lambda page: fragments(page)[:-1])
    parsed = PdfParser(ocr=False, layout=False).parse(make_pdf(PARAS), "a.pdf")
    assert [b["text"] for b in parsed.blocks] == ["첫째 문단이다.", "둘째 문단이다."]
    (page,) = parsed.pages
    assert page.coverage is None and page.text_stats.chars == 21
    (note,) = parsed.regions
    assert (note.region_id, note.kind, note.fallback_reason) == ("p1-coverage", "paragraph", "coverage_mismatch")
    assert gate_numbers(note) == {"in_blocks": (False, 14, "==21"), "doubled": (True, 0, "==0")}


def test_a_char_given_to_two_blocks_leaves_no_coverage_and_one_history_note(monkeypatch):
    """같은 글자가 두 블록에 들면(여기서는 첫 줄 앞 두 글자를 함께 가진 두 표) 장부 글자 수가 맞아도 이중 배정이라 그
    쪽 coverage는 None이고 처리 이력에 coverage_mismatch가 정확히 한 줄 남는다(파싱은 실패하지 않는다)."""
    cells = [Cell(row=0, col=0, text="첫째", text_source="text_layer")]
    twin = TableSpec(bbox=(0.1, 0.07, 0.3, 0.09), table=Table(n_rows=1, n_cols=1, cells=cells),
                     char_ids=frozenset(range(2)))
    monkeypatch.setattr(pdf_parser, "find_tables", lambda page: [twin, twin])
    parsed = PdfParser(ocr=False, layout=False).parse(make_pdf(PARAS), "a.pdf")
    assert [b["kind"] for b in parsed.blocks].count("table") == 2
    (page,) = parsed.pages
    assert page.coverage is None and page.text_stats.chars == 21
    assert [(r.region_id, r.kind, r.fallback_reason) for r in parsed.regions] == [
        ("p1-coverage", "paragraph", "coverage_mismatch")]
    assert gate_numbers(parsed.regions[0]) == {"in_blocks": (True, 21, "==21"), "doubled": (False, 2, "==0")}


def fake_ocr(monkeypatch, *reads) -> None:
    """OCR 추가 설치가 있는 것처럼(모델 없이): 그린 쪽마다 차례로 줄 목록 하나를 읽는다. 줄은 (글자, 점수, 보이는 쪽 pt
    상자)이고 그림 화소(A4 너비 기준 배율)로 바꿔 돌려준다(scan.to_page가 다시 pt로 옮긴다)."""
    order = iter(reads)

    def read_lines(image):
        px = image.size[0] / 595.0
        return [OcrLine(box=((x0 * px, y0 * px), (x1 * px, y0 * px), (x1 * px, y1 * px), (x0 * px, y1 * px)),
                        text=text, score=score) for text, score, (x0, y0, x1, y1) in next(order)]

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "get_reader", lambda: None)
    monkeypatch.setattr(ocr, "read_lines", read_lines)


OVER_BROKEN = (70.0, 60.0, 170.0, 78.0)  # 기준선 y=770(보이는 쪽 72pt) 11pt 줄을 덮는 OCR 줄 상자(pt)


def test_unreliable_page_is_read_by_ocr_instead_of_its_text_layer(monkeypatch):
    """OCR을 쓸 수 있으면 unreliable 쪽은 scanned처럼 쪽을 그려 OCR 문단으로 낸다: 깨진 글자층과 겹친 OCR 줄도 버리지
    않고(겹침 거르기를 쓰지 않는다), 표를 찾지 않으며, 보이는 글자층 글자는 replaced(숨은 글자는 그대로 hidden)이고
    처리 이력에 unreliable_text_layer_ocr를 남긴다. OCR 문단 신뢰도는 scanned 쪽처럼 평균 점수 × 0.5다(상한 0.2를
    씌우지 않는다). 같은 문서의 digital 쪽 블록은 그 쪽이 빈 쪽일 때와 같다."""
    before = PdfParser(layout=False).parse(make_pdf([PARAS[0], [], PARAS[0]]), "a.pdf")
    states = iter(["digital", "unreliable", "digital"])
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: next(states))
    fake_ocr(monkeypatch, [("다시 읽은 쪽 글자다.", 0.9, OVER_BROKEN)])
    tried = []
    find = pdf_parser.find_tables
    monkeypatch.setattr(pdf_parser, "find_tables", lambda page: tried.append(page.page) or find(page))
    after = PdfParser(layout=False).parse(
        make_pdf([PARAS[0], [(770, "깨진 쪽 글자다.", 0), (740, "숨은", 3)], PARAS[0]]), "a.pdf")
    assert [p.text_layer for p in after.pages] == ["digital", "unreliable", "digital"] and tried == [1, 3]
    assert [b for b in after.blocks if b["locator"]["page"] != 2] == list(before.blocks)
    assert [(b["text"], b["text_source"], b["confidence"]) for b in after.blocks if b["locator"]["page"] == 2] == [
        ("다시 읽은 쪽 글자다.", "ocr", 0.45)]
    assert after.pages[1].coverage == TextCoverage(layer_chars=9, in_blocks=0, hidden=2, replaced=7)
    (note,) = after.regions
    assert (note.region_id, note.kind, note.fallback_reason, note.locator.bbox, note.gate) == (
        "p2-unreliable-text-layer", "paragraph", "unreliable_text_layer_ocr", BBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0),
        None)


def test_scanned_page_keeps_the_text_layer_first_next_to_an_ocr_read_unreliable_page(monkeypatch):
    """겹침 거르기를 끄는 것은 OCR로 대신 읽는 unreliable 쪽뿐이다: 같은 문서의 scanned 쪽은 보이는 글자와 겹친 OCR
    줄을 그대로 버린다(텍스트 레이어 우선)."""
    states = iter(["scanned", "unreliable"])
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: next(states))
    fake_ocr(monkeypatch, [("다시 읽은 줄이다.", 0.9, OVER_BROKEN)], [("다시 읽은 줄이다.", 0.9, OVER_BROKEN)])
    parsed = PdfParser(layout=False).parse(make_pdf([[(770, "보이는 글자다.", 0)], [(770, "깨진 쪽 글자다.", 0)]]), "a.pdf")
    assert [(b["locator"]["page"], b["text_source"], b["text"]) for b in parsed.blocks] == [
        (1, "text_layer", "보이는 글자다."), (2, "ocr", "다시 읽은 줄이다.")]
    assert [p.coverage.replaced for p in parsed.pages] == [0, 7]


def test_unreliable_page_without_accepted_ocr_text_falls_back_to_its_text_layer(monkeypatch):
    """받아들인 OCR 글자가 없고(여기서는 점수가 낮은 줄뿐) 보이는 글자층이 있으면 그 쪽은 TC-A 유지 경로로 돌아간다:
    깨진 글자층 블록(사진 그림 포함, 신뢰도 0.2 이하), 장부 in_blocks(replaced 0), 처리 이력 unreliable_text_layer_kept와
    OCR 글자가 없었다는 실패 검사. 원문이 결과물에 남는다. 쪽은 한 번만 그린다(OCR에 쓴 그림을 사진 자르기에 다시 쓴다)."""
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    fake_ocr(monkeypatch, [("흐린 줄이다", 0.3, OVER_BROKEN)])
    renders = []
    render = scan.render
    monkeypatch.setattr(scan, "render", lambda data, name, index: renders.append(index) or render(data, name, index))

    def broken(c):
        put(c, 72, 770, 11, "깨진 쪽 글자다.")
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 72, 400, width=200, height=150)

    parsed = PdfParser(layout=False).parse(draw_pdf(broken), "a.pdf")
    assert [(b["kind"], b["text_source"], b["confidence"]) for b in parsed.blocks] == [
        ("paragraph", "text_layer", 0.2), ("figure", "text_layer", 0.2)]
    assert renders == [0] and len(parsed.assets) == 1
    assert parsed.pages[0].coverage == TextCoverage(layer_chars=7, in_blocks=7, hidden=0, replaced=0)
    (note,) = parsed.regions
    assert (note.region_id, note.fallback_reason, note.gate.passed) == (
        "p1-unreliable-text-layer", "unreliable_text_layer_kept", False)
    assert [(c.name, c.passed, c.value, c.threshold) for c in note.gate.checks] == [("ocr_text", False, 0, ">0")]


def test_unreliable_page_without_visible_text_or_ocr_text_has_no_blocks(monkeypatch):
    """보이는 글자층도 받아들인 OCR 글자도 없는 unreliable 쪽(숨은 글자뿐)은 되돌릴 원문이 없어 ocr 쪽 그대로다: 블록이
    없고 장부는 숨은 글자뿐이며(replaced 0) 처리 이력은 unreliable_text_layer_ocr다."""
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    fake_ocr(monkeypatch, [])
    parsed = PdfParser(layout=False).parse(make_pdf([[(770, "숨은", 3)]]), "a.pdf")
    assert parsed.blocks == () and parsed.pages[0].coverage == TextCoverage(layer_chars=2, in_blocks=0, hidden=2,
                                                                           replaced=0)
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-unreliable-text-layer", "unreliable_text_layer_ocr")]


@pytest.mark.parametrize("ledger,checks", [
    (group.Ledger(in_blocks=2, doubled=0), {"in_blocks": (False, 2, "==0"), "doubled": (True, 0, "==0")}),
    (group.Ledger(in_blocks=0, doubled=1), {"in_blocks": (True, 0, "==0"), "doubled": (False, 1, "==0")})])
def test_ocr_read_page_whose_ledger_does_not_add_up_leaves_no_coverage_and_one_note(monkeypatch, ledger, checks):
    """ocr 쪽은 블록에 든 텍스트 레이어 글자가 0이어야 한다. 깨진 글자가 블록에 새거나(in_blocks > 0) 두 번 들면
    (doubled) 파싱은 이어지고 그 쪽 coverage는 None, 처리 이력 coverage_mismatch 한 줄(기준 ==0)이 남는다."""
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    fake_ocr(monkeypatch, [("다시 읽은 쪽 글자다.", 0.9, OVER_BROKEN)])
    build = pdf_parser.build_page_specs
    monkeypatch.setattr(pdf_parser, "build_page_specs", lambda *a, **kw: (build(*a, **kw)[0], {1: ledger}))
    parsed = PdfParser(layout=False).parse(make_pdf([[(770, "깨진 쪽 글자다.", 0)]]), "a.pdf")
    assert [b["text"] for b in parsed.blocks] == ["다시 읽은 쪽 글자다."] and parsed.pages[0].coverage is None
    assert [r.fallback_reason for r in parsed.regions] == ["unreliable_text_layer_ocr", "coverage_mismatch"]
    assert gate_numbers(parsed.regions[1]) == checks


def test_auto_mode_with_a_broken_ocr_install_raises_on_an_unreliable_page(monkeypatch):
    """자동 모드: unreliable 쪽도 OCR로 읽을 쪽이라, 설치는 있는데 읽개를 못 만들면 깨진 글자층으로 조용히 물러나지 않고
    OcrUnavailable이다(scanned 쪽과 같다. 종료 코드 1). 끄면(--no-ocr) 글자층 유지 경로다."""
    def broken():
        raise OcrUnavailable("OCR models could not be loaded: x; run with --no-ocr")

    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "get_reader", broken)
    data = make_pdf([[(770, "깨진 쪽 글자다.", 0)]])
    kept = PdfParser(ocr=False, layout=False).parse(data, "a.pdf")
    assert [r.fallback_reason for r in kept.regions] == ["unreliable_text_layer_kept"]
    with pytest.raises(OcrUnavailable, match="--no-ocr"):
        PdfParser(layout=False).parse(data, "a.pdf")


def test_ocr_error_on_an_unreliable_page_is_raised_not_turned_into_the_kept_path(monkeypatch):
    """OCR로 읽을 unreliable 쪽에서 OCR 실행이 예외를 내면 그대로 오류다: 받아들인 OCR 글자가 없을 때의 유지 경로로
    조용히 돌아가지 않는다(유지 경로는 OCR이 돌았는데 글자가 없을 때뿐)."""
    def fail(image):
        raise RuntimeError("ocr run failed")

    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "get_reader", lambda: None)
    monkeypatch.setattr(ocr, "read_lines", fail)
    with pytest.raises(RuntimeError, match="ocr run failed"):
        PdfParser(layout=False).parse(make_pdf([[(770, "깨진 쪽 글자다.", 0)]]), "a.pdf")


def test_unreliable_page_whose_ocr_lines_are_all_blank_falls_back_to_its_text_layer(monkeypatch):
    """점수가 높아도 공백뿐인 OCR 줄은 받아들인 OCR 글자가 아니다: 보이는 글자층이 있으면 유지 경로(깨진 글자층 블록,
    처리 이력 unreliable_text_layer_kept + 실패한 ocr_text 검사)이고 OCR 처리 이력은 남지 않는다."""
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    fake_ocr(monkeypatch, [("   ", 0.99, OVER_BROKEN)])
    parsed = PdfParser(layout=False).parse(make_pdf([[(770, "깨진 쪽 글자다.", 0)]]), "a.pdf")
    assert [(b["text"], b["text_source"], b["confidence"]) for b in parsed.blocks] == [
        ("깨진 쪽 글자다.", "text_layer", 0.2)]
    assert parsed.pages[0].coverage == TextCoverage(layer_chars=7, in_blocks=7, hidden=0, replaced=0)
    (note,) = parsed.regions
    assert (note.region_id, note.fallback_reason) == ("p1-unreliable-text-layer", "unreliable_text_layer_kept")
    assert [(c.name, c.passed, c.value, c.threshold) for c in note.gate.checks] == [("ocr_text", False, 0, ">0")]


def losing(monkeypatch, *texts: str) -> None:
    """블록 명세가 쪽마다 처음 부르는 group.fragments(블록이 될 줄 묶기)에서 texts 줄 조각을 일부러 잃는다(배정
    빠뜨리기). 같은 쪽의 다음 호출(블록에 들지 않은 글자를 줄로 묶는 구조 문단)은 그대로다."""
    real, seen = group.fragments, set()

    def dropping(page):
        out = real(page)
        if page.page in seen:
            return out
        seen.add(page.page)
        return [f for f in out if f.text not in texts]

    monkeypatch.setattr(group, "fragments", dropping)


def test_lost_chars_come_back_once_as_a_structural_paragraph_with_a_history_note(monkeypatch):
    """어느 블록에도 들지 않은 보이는 글자(여기서는 첫 줄 조각을 일부러 잃는다)는 구조 문단(paragraph, 신뢰도 0.3)으로
    쪽 블록 끝에 정확히 한 번 나온다. 장부는 맞고(in_blocks에 들고 rescued로 센다) 처리 이력에 unassigned_text 한 줄과
    살린 글자 수가 남는다. 파싱은 실패하지 않는다."""
    losing(monkeypatch, "첫째 문단이다.")
    parsed = PdfParser(ocr=False, layout=False).parse(make_pdf(PARAS), "a.pdf")
    assert [(b["kind"], b["text"], b["text_source"], b["confidence"]) for b in parsed.blocks] == [
        ("paragraph", "둘째 문단이다.", "text_layer", 0.7), ("paragraph", "셋째 문단이다.", "text_layer", 0.7),
        ("paragraph", "첫째 문단이다.", "text_layer", 0.3)]
    (page,) = parsed.pages
    assert page.coverage == TextCoverage(layer_chars=21, in_blocks=21, hidden=0, rescued=7)
    (note,) = parsed.regions
    assert (note.region_id, note.kind, note.fallback_reason, note.locator.bbox) == (
        "p1-unassigned-text", "paragraph", "unassigned_text", BBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0))
    assert [(c.name, c.passed, c.value, c.threshold) for c in note.gate.checks] == [("rescued", False, 7, "==0")]


def test_a_char_in_two_blocks_is_still_a_mismatch_next_to_rescued_text(monkeypatch):
    """구조 문단은 블록에 들지 않은 글자만 살린다: 같은 글자가 두 블록에 든 것(두 표가 첫 줄 앞 두 글자를 함께 가졌다)은
    그대로 장부 오류다. 잃은 줄은 구조 문단과 unassigned_text로, 이중 배정은 coverage=None과 coverage_mismatch로
    남는다(in_blocks는 맞다)."""
    cells = [Cell(row=0, col=0, text="첫째", text_source="text_layer")]
    twin = TableSpec(bbox=(0.1, 0.07, 0.3, 0.09), table=Table(n_rows=1, n_cols=1, cells=cells),
                     char_ids=frozenset(range(2)))
    monkeypatch.setattr(pdf_parser, "find_tables", lambda page: [twin, twin])
    losing(monkeypatch, "셋째 문단이다.")
    parsed = PdfParser(ocr=False, layout=False).parse(make_pdf(PARAS), "a.pdf")
    assert [b["text"] for b in parsed.blocks if b["confidence"] == 0.3] == ["셋째 문단이다."]
    assert parsed.pages[0].coverage is None
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-unassigned-text", "unassigned_text"), ("p1-coverage", "coverage_mismatch")]
    assert gate_numbers(parsed.regions[1]) == {"in_blocks": (True, 21, "==21"), "doubled": (False, 2, "==0")}


def test_blank_ocr_fallback_page_rescues_a_lost_line_under_the_unreliable_cap(monkeypatch):
    """OCR이 공백 줄만 읽어 유지 경로(layer)로 돌아간 unreliable 쪽도 구조 문단을 쓴다(쪽 상태가 아니라 최종 모드로
    정한다): 잃은 줄은 구조 문단으로 나오고 신뢰도는 깨진 글자층 상한 0.2다. 처리 이력은 unreliable_text_layer_kept(ocr_text
    실패 검사) 다음 unassigned_text이고, 장부는 잃은 글자를 rescued로 센다(replaced 0)."""
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    fake_ocr(monkeypatch, [("   ", 0.99, OVER_BROKEN)])
    losing(monkeypatch, "깨진 첫 줄이다.")
    parsed = PdfParser(layout=False).parse(make_pdf([[(770, "깨진 첫 줄이다.", 0), (730, "깨진 둘째 줄이다.", 0)]]),
                                           "a.pdf")
    assert [(b["kind"], b["text"], b["text_source"], b["confidence"]) for b in parsed.blocks] == [
        ("paragraph", "깨진 둘째 줄이다.", "text_layer", 0.2), ("paragraph", "깨진 첫 줄이다.", "text_layer", 0.2)]
    assert parsed.pages[0].coverage == TextCoverage(layer_chars=15, in_blocks=15, hidden=0, replaced=0, rescued=7)
    kept, lost = parsed.regions
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-unreliable-text-layer", "unreliable_text_layer_kept"), ("p1-unassigned-text", "unassigned_text")]
    assert [(c.name, c.passed, c.value, c.threshold) for c in kept.gate.checks] == [("ocr_text", False, 0, ">0")]
    assert [(c.name, c.passed, c.value, c.threshold) for c in lost.gate.checks] == [("rescued", False, 7, "==0")]


def test_mixed_document_rescues_on_digital_and_scanned_pages_but_not_on_the_ocr_page(monkeypatch):
    """digital·scanned(OCR 문단 있음)·OCR로 읽는 unreliable 쪽이 섞인 문서에서 쪽마다 한 줄씩 배정을 잃으면: digital·
    scanned 쪽은 잃은 줄을 구조 문단과 unassigned_text로 살린다. ocr 쪽은 텍스트 레이어 글자가 블록에 들지 않는 것이
    정상이라 구조 문단도 unassigned_text도 없고 보이는 글자는 모두 replaced다(in_blocks 0)."""
    states = iter(["digital", "scanned", "unreliable"])
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: next(states))
    far = (70.0, 400.0, 250.0, 418.0)  # 글자층 줄과 겹치지 않는 OCR 줄 상자(pt)
    fake_ocr(monkeypatch, [("스캔에서 읽은 글자다.", 0.9, far)], [("다시 읽은 쪽 글자다.", 0.9, OVER_BROKEN)])
    losing(monkeypatch, "디지털 잃는 줄.", "스캔 잃는 줄.", "깨진 잃는 줄.")
    data = make_pdf([[(770, "디지털 잃는 줄.", 0), (730, "디지털 남는 줄.", 0)],
                     [(770, "스캔 잃는 줄.", 0), (730, "스캔 남는 줄.", 0)],
                     [(770, "깨진 잃는 줄.", 0), (730, "깨진 남는 줄.", 0)]])
    parsed = PdfParser(layout=False).parse(data, "a.pdf")
    assert [p.text_layer for p in parsed.pages] == ["digital", "scanned", "unreliable"]
    assert [(b["locator"]["page"], b["text"], b["text_source"], b["confidence"]) for b in parsed.blocks] == [
        (1, "디지털 남는 줄.", "text_layer", 0.7), (1, "디지털 잃는 줄.", "text_layer", 0.3),
        (2, "스캔 남는 줄.", "text_layer", 0.7), (2, "스캔에서 읽은 글자다.", "ocr", 0.45),
        (2, "스캔 잃는 줄.", "text_layer", 0.3), (3, "다시 읽은 쪽 글자다.", "ocr", 0.45)]
    assert [p.coverage for p in parsed.pages] == [
        TextCoverage(layer_chars=14, in_blocks=14, hidden=0, replaced=0, rescued=7),
        TextCoverage(layer_chars=12, in_blocks=12, hidden=0, replaced=0, rescued=6),
        TextCoverage(layer_chars=12, in_blocks=0, hidden=0, replaced=12, rescued=0)]
    assert [(r.region_id, r.fallback_reason) for r in parsed.regions] == [
        ("p1-unassigned-text", "unassigned_text"), ("p2-unassigned-text", "unassigned_text"),
        ("p3-unreliable-text-layer", "unreliable_text_layer_ocr")]
