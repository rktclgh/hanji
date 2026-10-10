import ctypes
import io

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hanji.errors import ParseError
from hanji.formats.pdf import extract
from hanji.formats.pdf.extract import PageText, decode_unicode, extract_pages, normalize_point

FONT = "HYGothic-Medium"  # reportlab 내장 CID 글꼴: ascent 752, descent -142, 한글 너비 1000
pdfmetrics.registerFont(UnicodeCIDFont(FONT))
OTHER_CID_FONT = "STSong-Light"  # 이름이 한국어 글꼴이 아닌 미임베드 CID 글꼴
pdfmetrics.registerFont(UnicodeCIDFont(OTHER_CID_FONT))
GRAY_JPEG = bytes.fromhex(  # 8×8 회색 JPEG
    "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123251d283a333d"
    "3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffc0000b080008000801011100ffc40014000100"
    "000000000000000000000000000005ffc40014100100000000000000000000000000000000ffda0008010100003f0041ffd9")


def make_pdf(draw, size=(595.0, 842.0), **kw) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=size, invariant=1, pageCompression=0, **kw)
    draw(c)
    c.showPage()
    c.save()
    return buf.getvalue()


def put(c, x, y, size, s, mode=0, font=FONT):
    c.saveState()
    t = c.beginText(x, y)
    t.setFont(font, size)
    t.setTextRenderMode(mode)
    t.textOut(s)
    c.drawText(t)
    c.restoreState()


def only_page(data: bytes):
    pages = extract_pages(data, "t.pdf")
    assert len(pages) == 1
    return pages[0]


def test_page_size_rotation_and_korean_text():
    page = only_page(make_pdf(lambda c: put(c, 72, 770, 18, "가나 다")))
    assert (page.page, page.width_pt, page.height_pt, page.rotation) == (1, 595.0, 842.0, 0)
    assert "".join(c.text for c in page.chars) == "가나 다"
    assert page.image_coverage == ()


def test_char_box_uses_font_metrics_not_glyph_outline():
    """글리프 외곽(대체 글꼴에 따라 OS마다 다름)이 아니라 글꼴 사전의 너비·ascent·descent로 계산한다."""
    first = only_page(make_pdf(lambda c: put(c, 72, 770, 18, "가"))).chars[0]
    assert first.x0 == pytest.approx(72 / 595) and first.x1 == pytest.approx(90 / 595)
    assert first.y0 == pytest.approx((842 - (770 + 0.752 * 18)) / 842)
    assert first.y1 == pytest.approx((842 - (770 - 0.142 * 18)) / 842)
    assert first.baseline == pytest.approx((842 - 770) / 842) and first.size == pytest.approx(18)


def test_effective_size_uses_text_matrix_scale():
    """한글 프로그램 PDF처럼 단위 크기 125로 그리고 행렬로 0.12배 줄이면 실제 크기는 15pt."""
    def draw(c):
        c.saveState()
        c.scale(0.12, 0.12)
        put(c, 600, 6000, 125, "가나")
        c.restoreState()
        c.saveState()
        c.scale(0.9, 1.0)  # 장평 90%: 세로 배율만 크기에 쓴다
        put(c, 80, 600, 10, "다")
        c.restoreState()

    chars = only_page(make_pdf(draw)).chars
    assert [c.size for c in chars] == pytest.approx([15.0, 15.0, 10.0])
    assert (chars[0].x1 - chars[0].x0) * 595 == pytest.approx(15.0)
    assert (chars[2].x1 - chars[2].x0) * 595 == pytest.approx(9.0)


def test_render_modes_invisible_and_fill_stroke_bold():
    def draw(c):
        put(c, 72, 770, 11, "숨은", mode=3)
        put(c, 72, 750, 11, "굵게", mode=2)
        put(c, 72, 730, 11, "보통")

    chars = only_page(make_pdf(draw)).chars
    assert [(c.text, c.invisible, c.bold) for c in chars] == [
        ("숨", True, False), ("은", True, False), ("굵", False, True), ("게", False, True),
        ("보", False, False), ("통", False, False)]


def test_render_mode_is_inherited_across_text_objects():
    """Tr은 그래픽 상태라 다음 BT로 이어진다(reportlab은 0 Tr을 생략). PDFium이 보는 대로 따른다."""
    def draw(c):
        put(c, 72, 770, 11, "가", mode=0)
        t = c.beginText(72, 750)
        t.setFont(FONT, 11)
        t.setTextRenderMode(3)
        t.textOut("나")
        c.drawText(t)
        t = c.beginText(72, 730)
        t.setFont(FONT, 11)
        t.textOut("다")  # Tr 없음 → 3을 물려받는다
        c.drawText(t)

    assert [(c.text, c.invisible) for c in only_page(make_pdf(draw)).chars] == [
        ("가", False), ("나", True), ("다", True)]


def test_image_coverage_top_level_and_clipped_to_page():
    def draw(c):
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 0, 0, width=595, height=421)  # 쪽 절반
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), -100, 800, width=200, height=100)  # 쪽 밖으로 잘림

    coverage = only_page(make_pdf(draw)).image_coverage
    assert coverage[0] == pytest.approx(0.5, abs=1e-3)
    assert coverage[1] == pytest.approx(100 * 42 / (595 * 842), abs=1e-4)


def test_large_form_with_small_image_counts_only_the_image():
    """쪽 전체 서식 폼(테두리) 안의 작은 로고: 폼 상자가 아니라 그림 상자를, 폼 행렬(이동·축소)을 거쳐 잰다."""
    def draw(c):
        c.beginForm("template")
        c.rect(10, 10, 575, 822)
        c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 50, 700, width=60, height=60)
        c.endForm()
        c.saveState()
        c.translate(20, -30)
        c.scale(0.5, 0.5)
        c.doForm("template")
        c.restoreState()

    assert only_page(make_pdf(draw)).image_coverage == pytest.approx((30 * 30 / (595 * 842),))


def clip(c, x, y, w, h):
    path = c.beginPath()
    path.rect(x, y, w, h)
    c.clipPath(path, stroke=0, fill=0)


def full_page_image(c):
    c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 0, 0, width=595, height=842)


def clipped_top_level(c):
    c.saveState()
    clip(c, 50, 700, 40, 40)
    full_page_image(c)
    c.restoreState()


def clipped_twice(c):  # 클리핑 경로 둘의 교집합(x 200~300)
    c.saveState()
    clip(c, 0, 0, 300, 842)
    clip(c, 200, 0, 395, 842)
    full_page_image(c)
    c.restoreState()


def clipped_inside_form(c):  # 폼 안 클리핑은 폼 좌표: 폼 행렬(0.5배·이동)을 거친다
    c.beginForm("logo")
    clip(c, 50, 700, 40, 40)
    full_page_image(c)
    c.endForm()
    c.saveState()
    c.translate(10, 10)
    c.scale(0.5, 0.5)
    c.doForm("logo")
    c.restoreState()


def clipped_around_form(c):  # 폼을 그리기 전 클리핑은 폼 객체에 붙고 폼 안 그림에도 적용된다
    c.beginForm("page")
    full_page_image(c)
    c.endForm()
    c.saveState()
    clip(c, 100, 100, 40, 40)
    c.translate(10, 10)
    c.scale(0.5, 0.5)
    c.doForm("page")
    c.restoreState()


@pytest.mark.parametrize("draw,area", [(clipped_top_level, 40 * 40), (clipped_twice, 100 * 842),
                                       (clipped_inside_form, 20 * 20), (clipped_around_form, 40 * 40)])
def test_image_coverage_counts_only_the_clipped_visible_part(draw, area):
    assert only_page(make_pdf(draw)).image_coverage == pytest.approx((area / (595 * 842),), abs=1e-6)


def test_unreadable_clip_path_falls_back_to_the_image_box(monkeypatch):
    monkeypatch.setattr(pdfium_c, "FPDFClipPath_CountPaths", lambda clip_path: -1)
    assert only_page(make_pdf(clipped_top_level)).image_coverage == pytest.approx((1.0,))


@pytest.mark.parametrize("rotation,axes", [(0, (0, 1)), (90, (1, 2)), (180, (2, 3)), (270, (3, 0))])
def test_reading_axes_and_baseline_follow_page_rotation(rotation, axes):
    """axes = (진행 방향, 줄 아래 방향), 보이는 쪽의 +x·+y·−x·−y = 0·1·2·3. baseline은 줄 아래 방향 축 위 원점의
    위치라 회전과 관계없이 원래 쪽의 위에서부터 잰 값이다. reportlab은 90·270도면 MediaBox를 842×595로 눕힌다."""
    def draw(c):
        c.setPageRotation(rotation)
        put(c, 72, 300, 11, "가")

    page = only_page(make_pdf(draw))
    assert (page.width_pt, page.height_pt) == (595.0, 842.0)
    (char,) = page.chars
    assert char.axes == axes
    assert char.baseline == pytest.approx(1 - 300 / (842 if rotation in (0, 180) else 595))


def test_reading_axes_of_negative_size_and_mirrored_text():
    def draw(c):
        t = c.beginText(300, 400)
        t.setFont(FONT, -11)
        t.textOut("가")
        c.drawText(t)
        c.saveState()
        c.transform(-1, 0, 0, 1, 595, 0)
        put(c, 300, 300, 11, "나")
        c.restoreState()

    flipped, mirrored = only_page(make_pdf(draw)).chars
    assert (flipped.axes, mirrored.axes) == ((2, 3), (2, 1))
    assert flipped.baseline == pytest.approx(400 / 842) and mirrored.baseline == pytest.approx((842 - 300) / 842)


def test_negative_font_size_gives_positive_size(monkeypatch):
    """음수 Tf도 글꼴 사전 상자를 쓴다(OS마다 다른 느슨한 상자로 빠지지 않는다). 글자는 원점 왼쪽·아래로 뒤집힌다."""
    def draw(c):
        t = c.beginText(300, 400)
        t.setFont(FONT, -11)  # 글자가 뒤집혀 왼쪽으로 진행한다
        t.textOut("가나")
        c.drawText(t)

    def no_loose_box(*args):
        raise AssertionError("loose char box fallback used")

    monkeypatch.setattr(pdfium_c, "FPDFText_GetLooseCharBox", no_loose_box)
    chars = only_page(make_pdf(draw)).chars
    assert [c.size for c in chars] == pytest.approx([11.0, 11.0])
    assert [(c.x0 * 595, c.x1 * 595) for c in chars] == pytest.approx([(289, 300), (278, 289)])
    for c in chars:
        assert c.y0 == pytest.approx((842 - (400 + 0.142 * 11)) / 842)
        assert c.y1 == pytest.approx((842 - (400 - 0.752 * 11)) / 842)
        assert c.baseline == pytest.approx(400 / 842)  # 줄 아래 방향(보이는 −y) 축의 원점 위치


def host(monkeypatch, draws_hangul):
    """컴퓨터 흉내: draws_hangul(BaseFont 이름)이 참인 미임베드 글꼴만 '가'를 그린다(거짓이면 한글 글꼴 없는 Linux)."""
    monkeypatch.setattr(extract, "_draws_hangul", lambda pdf, font: draws_hangul(extract._base_font_name(font)))


def test_dropped_space_in_latin_font_is_not_an_error_without_hangul_font(monkeypatch):
    """공백 한 칸 객체는 모든 OS에서 텍스트 쪽에서 빠진다. 한글이 없는 문서의 라틴 글꼴이면 한글 글꼴이 없어도 정상."""
    data = make_pdf(lambda c: (put(c, 72, 770, 11, "AB", font="Helvetica"), put(c, 72, 750, 11, " ", font="Helvetica")))
    host(monkeypatch, lambda name: False)
    assert [c.text for c in only_page(data).chars] == ["A", "B"]


def test_dropped_object_in_korean_named_font_is_an_error_even_without_hangul(monkeypatch):
    """한글이 하나도 안 나와도 빠진 객체의 글꼴 이름이 한국어 글꼴(HY…)이면 실패: 한글 한 글자 객체였을 수 있고,
    문서가 한국어 글꼴을 쓰므로 한글 글꼴을 설치하는 것이 맞는 해결이다(의도한 동작으로 고정)."""
    data = make_pdf(lambda c: (put(c, 72, 770, 11, "AB"), put(c, 72, 750, 11, " ")))
    host(monkeypatch, lambda name: True)
    assert [c.text for c in only_page(data).chars] == ["A", "B"]  # 한글을 그리는 컴퓨터: 빠진 공백은 문제없다
    host(monkeypatch, lambda name: False)
    with pytest.raises(ParseError, match="no Hangul glyphs"):
        extract_pages(data, "t.pdf")


def test_per_glyph_korean_document_without_hangul_font_is_an_error(monkeypatch):
    """글자마다 객체를 따로 쓰는 한국어 문서: 한글 글꼴이 없으면 PDFium 텍스트 쪽이 통째로 비어(실측, 아래는 그 흉내)
    한글이 하나도 나오지 않는다. 글꼴 이름이 한국어라 빈 쪽을 조용히 내지 않는다."""
    data = make_pdf(lambda c: [put(c, 72 + 11 * i, 770, 11, s) for i, s in enumerate("가나다")])
    host(monkeypatch, lambda name: False)
    monkeypatch.setattr(pdfium_c, "FPDFText_CountChars", lambda textpage: 0)
    with pytest.raises(ParseError, match="no Hangul glyphs"):
        extract_pages(data, "t.pdf")


def mixed_korean_document(c):  # 여러 글자 한글 줄은 살아남고, 이름이 한국어가 아닌 CID 글꼴의 한 글자 객체는 빠진다
    put(c, 72, 770, 11, "가나")
    put(c, 72, 750, 11, " ", font=OTHER_CID_FONT)


def test_dropped_object_in_other_font_is_an_error_when_hangul_is_not_drawable(monkeypatch):
    """한글이 나왔는데 그 한글을 낸 글꼴도 '가'를 못 그리면 이 컴퓨터에 한글 글꼴이 없다: 이름이 한국어가 아닌
    글꼴의 빠진 객체도 한글이었을 수 있다."""
    host(monkeypatch, lambda name: False)
    with pytest.raises(ParseError, match="no Hangul glyphs"):
        extract_pages(make_pdf(mixed_korean_document), "t.pdf")


def test_dropped_object_in_font_without_hangul_is_fine_where_hangul_is_drawable(monkeypatch):
    """한글을 그리는 컴퓨터(macOS 실측)에서도 중국·일본 CID 글꼴은 한글을 담지 못해 '가'를 못 그린다. 그 글꼴의
    빠진 공백 객체 때문에 한국어 문서가 실패하지 않는다."""
    host(monkeypatch, lambda name: name != OTHER_CID_FONT)
    assert [c.text for c in only_page(make_pdf(mixed_korean_document)).chars] == ["가", "나"]


@pytest.mark.parametrize("name,korean", [
    ("HYGothic-Medium", True), ("ABCDEF+HYSMyeongJo-Medium", True), ("Batang-UniKS-UCS2-H", True), ("KoPubDotumMedium", True),
    ("HCRDotum", True), ("함초롬바탕", True), ("NotoSansCJKkr-Regular", True), ("NotoSansKR-Bold", True), ("MalgunGothic", True),
    ("Pretendard-Regular", True), ("SpoqaHanSansNeo", True), ("나눔고딕", True),
    ("Helvetica", False), ("STSong-Light", False), ("Hypatia", False), ("TimesNewRomanPSMT", False),
    ("MS-Mincho", False), ("ArialMT", False)])
def test_korean_font_name(name, korean):
    assert bool(extract._KOREAN_FONT.search(name)) is korean


def test_chars_outside_page_are_dropped():
    page = only_page(make_pdf(lambda c: (put(c, -300, 770, 11, "밖"), put(c, 72, 770, 11, "안"))))
    assert [c.text for c in page.chars] == ["안"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_normalize_point_matches_pdfium_device_mapping(rotation):
    doc = pdfium.PdfDocument.new()
    page = doc.new_page(300, 500)
    page.set_mediabox(10, 20, 310, 520)
    page.set_rotation(rotation)
    width, height = page.get_size()
    for x, y in [(10, 20), (310, 520), (60, 120), (200, 400)]:
        dx, dy = ctypes.c_int(), ctypes.c_int()
        pdfium_c.FPDF_PageToDevice(page, 0, 0, int(width * 1000), int(height * 1000), 0, x, y, dx, dy)
        expected = (dx.value / 1000 / width, dy.value / 1000 / height)
        assert normalize_point(x, y, page.get_bbox(), rotation) == pytest.approx(expected, abs=1e-5)
    doc.close()


def test_decode_unicode_pairs_and_unmapped():
    assert decode_unicode(ord("가"), None) == ("가", False, False)
    assert decode_unicode(0xD835, 0xDC00) == ("\U0001D400", False, True)
    assert decode_unicode(0xD835, ord("a")) == ("\ufffd", True, False)
    assert decode_unicode(0, None) == ("\ufffd", True, False)
    assert decode_unicode(0xFFFD, None) == ("\ufffd", True, False)


def test_encrypted_pdf_is_parse_error():
    data = make_pdf(lambda c: put(c, 72, 770, 11, "비밀"), encrypt="secret")
    with pytest.raises(ParseError) as info:
        extract_pages(data, "암호.pdf")
    assert (info.value.reason, info.value.location) == ("encrypted PDF", "암호.pdf")


@pytest.mark.parametrize("data", [b"", b"%PDF-1.4\n", b"not a pdf at all"])
def test_corrupt_pdf_is_parse_error(data):
    with pytest.raises(ParseError, match="invalid PDF") as info:
        extract_pages(data, "깨짐.pdf")
    assert info.value.location == "깨짐.pdf"


@pytest.mark.parametrize("cut", ["xref", "stream", "header"])
def test_truncated_pdf_is_parse_error_or_readable(cut):
    """잘린 PDF는 PDFium이 고쳐 읽거나(쪽이 나온다) ParseError다. 다른 예외는 없다(문서·쪽 단계 모두)."""
    data = make_pdf(lambda c: put(c, 72, 770, 11, "가나다"))
    end = {"xref": data.index(b"xref"), "stream": data.index(b"stream") + 10, "header": 20}[cut]
    try:
        pages = extract_pages(data[:end], "잘림.pdf")
    except ParseError as exc:
        assert exc.location.startswith("잘림.pdf")
    else:
        assert pages and all(isinstance(p, PageText) for p in pages)


def zero_page_pdf() -> bytes:
    """쪽 트리가 비어 있는(/Count 0) 최소 PDF."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 3\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref


def test_pdf_without_pages_is_parse_error():
    with pytest.raises(ParseError) as info:
        extract_pages(zero_page_pdf(), "빈.pdf")
    assert info.value.location == "빈.pdf"


@pytest.mark.parametrize("content", ["text", "image", "empty"])
def test_empty_page_box_is_parse_error(content):
    """CropBox가 MediaBox 밖이면 PDFium의 쪽 상자는 0×0이다. 0으로 나누지 않고 ParseError(이름:쪽)."""
    def draw(c):
        c.setCropBox((700, 700, 800, 800))
        if content == "text":
            put(c, 72, 770, 11, "가")
        elif content == "image":
            c.drawImage(ImageReader(io.BytesIO(GRAY_JPEG)), 0, 0, width=100, height=100)

    with pytest.raises(ParseError) as info:
        extract_pages(make_pdf(draw), "상자.pdf")
    assert (info.value.reason, info.value.location) == ("PDF page has an empty box", "상자.pdf:1")


RED = Image.new("RGB", (20, 10), (220, 30, 30))


def test_image_boxes_are_visible_page_fractions():
    def draw(c):
        c.drawImage(ImageReader(RED), 100, 600, width=200, height=150)
        c.drawImage(ImageReader(RED), -100, 800, width=200, height=100)  # 쪽 밖으로 잘린 부분은 뺀다

    first, cut = only_page(make_pdf(draw)).images
    assert first == pytest.approx((100 / 595, 92 / 842, 300 / 595, 242 / 842))
    assert cut == pytest.approx((0.0, 0.0, 100 / 595, 42 / 842))


@pytest.mark.parametrize("rotation,expected", [
    (90, (300 / 595, 100 / 842, 450 / 595, 300 / 842)),
    (180, (295 / 595, 300 / 842, 495 / 595, 450 / 842)),
    (270, (145 / 595, 542 / 842, 295 / 595, 742 / 842)),
])
def test_image_boxes_follow_page_rotation(rotation, expected):
    """보이는 쪽(렌더와 같은 틀) 0~1. reportlab은 90·270도면 MediaBox를 842×595로 눕힌다: 그림 PDF (100, 300)~(300, 450)."""
    def draw(c):
        c.setPageRotation(rotation)
        c.drawImage(ImageReader(RED), 100, 300, width=200, height=150)

    (rect,) = only_page(make_pdf(draw)).images
    assert rect == pytest.approx(expected)


def test_image_boxes_are_relative_to_the_crop_box():
    def draw(c):
        c.setCropBox((50, 100, 545, 800))  # 보이는 쪽 495×700pt
        c.drawImage(ImageReader(RED), 100, 600, width=200, height=150)

    page = only_page(make_pdf(draw))
    assert (page.width_pt, page.height_pt) == (495.0, 700.0)
    (rect,) = page.images
    assert rect == pytest.approx((50 / 495, 50 / 700, 250 / 495, 200 / 700))


def test_clipped_image_box_is_only_its_visible_part():
    def draw(c):
        c.saveState()
        path = c.beginPath()
        path.rect(50, 700, 40, 40)
        c.clipPath(path, stroke=0, fill=0)
        c.drawImage(ImageReader(RED), 0, 0, width=595, height=842)
        c.restoreState()

    (rect,) = only_page(make_pdf(draw)).images
    assert rect == pytest.approx((50 / 595, 102 / 842, 90 / 595, 142 / 842))


def test_path_count_counts_path_objects_in_forms_and_stops_at_the_cap(monkeypatch):
    def draw(c):
        for i in range(12):
            c.line(72, 100 + 10 * i, 300, 100 + 10 * i)
        c.beginForm("f")
        c.rect(10, 10, 50, 50)
        c.endForm()
        c.doForm("f")
        put(c, 72, 770, 11, "가")  # 글자는 path가 아니다

    data = make_pdf(draw)
    assert only_page(data).paths == 13
    monkeypatch.setattr(extract, "MAX_PATH_COUNT", 5)
    assert only_page(data).paths == 5


def test_image_boxes_stop_at_the_cap_but_coverage_keeps_every_image(monkeypatch):
    def draw(c):
        c.drawImage(ImageReader(RED), -300, 0, width=100, height=100)  # 쪽 밖: 상자는 빠지고 덮는 비율은 0
        for i in range(3):
            c.drawImage(ImageReader(RED), 100 + 100 * i, 600, width=50, height=50)

    data = make_pdf(draw)
    full = only_page(data)
    assert len(full.images) == 3 and len(full.image_coverage) == 4 and full.image_coverage[0] == 0.0
    monkeypatch.setattr(extract, "MAX_IMAGE_OBJECTS", 2)
    capped = only_page(data)
    assert capped.images == full.images[:2]
    assert capped.image_coverage == full.image_coverage


def test_chars_carry_their_index_on_the_page_as_id():
    """Char.id는 page.chars 순번이다(0부터, 낸 글자만 센다): 쪽 밖이라 버린 글자는 순번을 차지하지 않는다."""
    def draw(c):
        put(c, 72, 770, 11, "가나 다")
        put(c, -50, 700, 11, "라")  # 상자 중심이 쪽 밖: Char를 만들지 않는다
        put(c, 72, 600, 11, "마", mode=3)

    page = only_page(make_pdf(draw))
    assert "".join(c.text for c in page.chars) == "가나 다마"
    assert [c.id for c in page.chars] == list(range(5))
