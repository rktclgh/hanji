"""pypdfium2로 쪽마다 글자·그림을 읽는다.

PDFium은 스레드 안전하지 않다. 문서가 달라도 동시에 부르면 프로세스가 죽으므로 PDFium을 부르는 동안(문서를
열고 모두 닫을 때까지) 패키지에 하나뿐인 PDFIUM_LOCK을 잡는다. 쪽 그림 렌더러도 같은 잠금을 쓴다.
글자 상자는 글꼴 사전의 너비(/W)·ascent·descent와 글자 원점으로 계산한다. PDFium의 글리프 상자는
미임베드 글꼴이면 OS의 대체 글꼴에 따라 달라지므로 그 정보가 없을 때만 쓴다.
미임베드 글꼴은 PDFium이 시스템 글꼴로 대신 그린다. 한글 글꼴이 없는 컴퓨터(글꼴 없는 Linux 등)에서는 한 글자짜리
글자 객체가 텍스트에서 통째로 빠지므로 조용히 버리지 않고 ParseError로 알린다(_HangulCheck).
"""

import ctypes
import itertools
import math
import re
import threading
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal

with warnings.catch_warnings():  # pypdfium2_raw는 import할 때 버전 파일을 인코딩 없이 연다(EncodingWarning)
    warnings.simplefilter("ignore", EncodingWarning)
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

from ...errors import ParseError
from . import fonts  # pypdfium2를 위 경고 억제 블록에서 먼저 import한 뒤에 import한다

Box = tuple[float, float, float, float]  # left, bottom, right, top (PDF 쪽 좌표)
Box01 = tuple[float, float, float, float]  # x0, y0, x1, y1 (보이는 쪽 기준 0~1, 원점 왼쪽 위, 회전 보정 후)
Axes = tuple[int, int]  # (진행 방향, 줄 아래 방향). 보이는 쪽의 +x·+y·−x·−y = 0·1·2·3
UPRIGHT: Axes = (0, 1)

PDFIUM_LOCK = threading.Lock()  # PDFium 호출 전체를 줄 세운다(문서가 달라도)

# 선 모으기(표 검출 입력). 길이 단위는 pt
AXIS_TOL = 0.5  # 선분의 다른 축 변화 ≤ 0.5pt면 가로·세로 선. 사선은 버린다
THIN = 2.5  # 채운 사각형의 짧은 변 ≤ 2.5pt면 선(가운데 선을 stroke로)
MIN_RULE = 2.0  # 이보다 짧은 선분은 버린다(점선의 점은 아래처럼 이어 붙인 뒤 잰다)
LEN_EPS = 1e-3  # 길이를 MIN_RULE·DASH와 비교할 때의 여유(좌표 변환의 부동소수 오차로 정확히 2pt인 선이 빠지지 않게)
DASH = 0.2  # 점선 조각: 길이 DASH 이상 MIN_RULE 미만인 가로·세로 선분(한글 프로그램 점선은 0.48pt 점이 1.2pt 간격.
# 잇지 않으면 공공누리 정답 표 채점에서 찾은 표 37→36, 완벽 30→28)
DASH_GAP = 2.0  # 같은 위치의 점선 조각 사이가 ≤ 2pt면 한 선으로 잇는다
DASH_DRIFT = 0.05  # 이은 점선의 위치 흐름이 AXIS_TOL보다 크면 길이 1pt마다 0.05pt까지만(천천히 흐르는 점선은 선, 사선 점선은 아니다)
WHITE = 250  # 빨강·초록·파랑이 모두 이 이상이면 하얀색(배경과 같은 색)으로 보고 버린다(98% 이상 밝은 회색 칠도 버린다)
MAX_RULE_SEGMENTS = 200_000  # 쪽마다 훑는 path 구간(점선 조각도) 상한. 넘으면 그 쪽 선은 없다(악성 PDF가 잠금·메모리를
# 오래 쥐지 않게). 공공누리 정답 표 문서 8개의 쪽 최대는 15,560(path 객체 7,777)으로 약 13배 여유
MAX_PATH_COUNT = 10_000  # 쪽마다 path 객체는 이만큼까지만 센다(레이아웃 모델을 돌릴지 정하는 데만 쓴다)
MAX_IMAGE_OBJECTS = 10_000  # 쪽마다 PageText.images에 담는 그림 상자 상한. 넘는 그림은 빠진다(악성 PDF가 작은 파일로
# 메모리를 크게 쥐지 않게). 아이콘 수백 개 쪽도 넉넉하다. image_coverage에는 적용하지 않는다(스캔 판정은 그대로)

_BOLD_NAME = re.compile(r"bold|black|heavy", re.IGNORECASE)
_BOLD_WEIGHT = 600
# 한국어 글꼴로 보이는 BaseFont 이름(부분집합 접두어 ABCDEF+ 허용, 대소문자 무시. HY·HCR만 대문자 그대로).
# 대체 글꼴 이름(FPDFFont_GetFamilyName)은 컴퓨터마다 달라 쓰지 않는다. CMap 이름이 붙은 Type0 이름(…-UniKS-UCS2-H)도 잡는다
_KOREAN_FONT = re.compile(
    r"(?:^|\+)(?-i:HY|HCR)|batang|gulim|dotum|gungs(?:uh|eo)|malgun|nanum|hamchorom|myeongjo|myungjo|kopub|spoqa"
    r"|pretendard|applesd|noto\s*-?\s*(?:sans|serif)\s*-?\s*(?:cjk\s*-?\s*)?kr|source\s*han\s*(?:sans|serif)\s*-?\s*k"
    r"|uniks|\bksc|korea1|함초롬|한컴|바탕|굴림|돋움|궁서|맑은|명조|고딕", re.IGNORECASE)
_HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]")


@dataclass(frozen=True, slots=True)
class Char:
    """글자 하나. 상자는 보이는 쪽 기준 0~1(원점 왼쪽 위, 회전 보정 후), size는 실제 크기(pt).
    baseline은 줄 아래 방향 축(axes[1]) 위 글자 원점의 위치(0~1). 바로 선 글자면 보이는 y와 같다."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    baseline: float
    size: float
    bold: bool = False
    invisible: bool = False  # 렌더 모드 3
    unmapped: bool = False  # 유니코드 0·U+FFFD·매핑 오류(text는 U+FFFD)
    axes: Axes = UPRIGHT  # 읽는 방향: 회전한 쪽·음수 Tf·거울 행렬이면 바로 선 글자와 다르다
    # 쪽 안 순번(= page.chars 순번, extract가 매긴다). 줄·조각·블록까지 따라가 글자 장부를 id로 센다. 값 비교·해시에는
    # 쓰지 않는다(같은 자리 같은 글자 둘은 여전히 값이 같다)
    id: int = field(default=-1, compare=False)


RuleAxis = Literal["h", "v"]
RuleKind = Literal["stroke", "fill"]


@dataclass(frozen=True, slots=True)
class Rule:
    """가로(h)·세로(v) 선분 하나. 좌표는 보이는 쪽 pt(회전 보정, 원점 왼쪽 위: 글자 상자 × 쪽 너비·높이와 같은 틀),
    소수 셋째 자리. h면 pos = y, start·end = x 구간, v면 pos = x, start·end = y 구간(start < end).
    kind: stroke = 그은 선·얇은 채운 사각형, fill = 넓은 채운 사각형(칸 배경)의 변."""

    axis: RuleAxis
    pos: float
    start: float
    end: float
    kind: RuleKind = "stroke"


@dataclass(frozen=True, slots=True, weakref_slot=True)  # 약한 참조: figures 쪽 기억이 쪽을 붙잡지 않는다
class PageText:
    page: int
    width_pt: float  # 회전 보정 후
    height_pt: float
    rotation: int
    chars: tuple[Char, ...]
    image_coverage: tuple[float, ...]  # 그림마다 쪽 면적 대비 비율
    rules: tuple[Rule, ...] = ()  # 가로·세로 선분(표 검출 입력), 그린 순서
    images: tuple[Box01, ...] = ()  # 그림(이미지 객체)마다 쪽 안에 보이는 부분(보이는 쪽 0~1), 그린 순서. 쪽 밖·넓이 0은
    # 빠지고 MAX_IMAGE_OBJECTS까지만 담는다(image_coverage와 순서가 맞지 않는다)
    paths: int = 0  # path 객체 수(폼 안 포함, MAX_PATH_COUNT까지)


def open_pdf(data: bytes, name: str) -> pdfium.PdfDocument:
    """암호화·손상 PDF는 ParseError. 부르는 쪽이 PDFIUM_LOCK을 잡고 있어야 한다."""
    fonts.register_bundled_fonts()
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
            raise ParseError("encrypted PDF", name) from None
        raise ParseError(f"invalid PDF: {exc}", name) from None


def extract_pages(data: bytes, name: str) -> tuple[PageText, ...]:
    """쪽이 없거나(PDFium은 대개 열기부터 실패) 쪽을 읽지 못하면 ParseError."""
    with PDFIUM_LOCK:
        pdf = open_pdf(data, name)
        try:
            if len(pdf) == 0:
                raise ParseError("PDF has no pages", name)
            pages = []
            check = _HangulCheck()
            for index in range(len(pdf)):
                location = f"{name}:{index + 1}"
                try:
                    pages.append(_page(pdf, index, location, check))
                except pdfium.PdfiumError as exc:
                    raise ParseError(f"invalid PDF page: {exc}", location) from None
            check.raise_if_lost()
            return tuple(pages)
        finally:
            pdf.close()


def normalize_point(x: float, y: float, box: Box, rotation: int) -> tuple[float, float]:
    """PDF 쪽 좌표 → 보이는 쪽 기준 0~1(원점 왼쪽 위). rotation은 /Rotate(시계 방향)."""
    left, bottom, right, top = box
    u, v = (x - left) / (right - left), (top - y) / (top - bottom)
    match rotation:
        case 90:
            return 1 - v, u
        case 180:
            return 1 - u, 1 - v
        case 270:
            return v, 1 - u
        case _:
            return u, v


def _axis(dx: float, dy: float) -> int:
    """보이는 쪽(원점 왼쪽 위) 벡터의 주된 방향: +x·+y·−x·−y = 0·1·2·3."""
    if abs(dx) >= abs(dy):
        return 0 if dx >= 0 else 2
    return 1 if dy > 0 else 3


def _reading_axes(advance: tuple[float, float], up: tuple[float, float], box: Box, rotation: int) -> Axes:
    """PDF 쪽 좌표의 진행 벡터·글자 위쪽 벡터 → 보이는 쪽의 (진행 방향, 줄 아래 방향). 둘이 나란하면(기울임이
    지나친 행렬) 진행 방향의 시계 방향 90°를 줄 아래로 본다. 진행 벡터가 0이면 바로 선 글자."""
    left, bottom, right, top = box
    w, h = (right - left, top - bottom) if rotation in (0, 180) else (top - bottom, right - left)
    x0, y0 = normalize_point(left, bottom, box, rotation)

    def visible(vx: float, vy: float) -> tuple[float, float]:
        x1, y1 = normalize_point(left + vx, bottom + vy, box, rotation)
        return (x1 - x0) * w, (y1 - y0) * h

    ax, ay = visible(*advance)
    if ax == 0 and ay == 0:
        return UPRIGHT
    along = _axis(ax, ay)
    ux, uy = visible(*up)
    down = _axis(-ux, -uy) if (ux or uy) else (along + 1) % 4
    return (along, down) if down % 2 != along % 2 else (along, (along + 1) % 4)


def _on_axis(x: float, y: float, axis: int) -> float:
    """보이는 쪽 점(0~1)의 axis 방향 좌표(0~1)."""
    return (x, y, 1 - x, 1 - y)[axis]


def decode_unicode(code: int, following: int | None) -> tuple[str, bool, bool]:
    """(글자, 매핑 실패, 다음 코드를 함께 썼는지). UTF-16 대리쌍은 합치고 짝 없는 대리 코드는 매핑 실패."""
    if 0xD800 <= code < 0xDC00 and following is not None and 0xDC00 <= following < 0xE000:
        return chr(0x10000 + ((code - 0xD800) << 10) + (following - 0xDC00)), False, True
    if code in (0, 0xFFFD) or 0xD800 <= code < 0xE000:
        return "\ufffd", True, False
    return chr(code), False, False


def _address(handle: object) -> int:
    return ctypes.cast(handle, ctypes.c_void_p).value or 0


def _page(pdf: pdfium.PdfDocument, index: int, location: str, check: "_HangulCheck") -> PageText:
    page = pdf[index]
    try:
        width, height = page.get_size()
        rotation = page.get_rotation()
        box = page.get_bbox()
        left, bottom, right, top = box
        if not (right > left and top > bottom):  # 예: CropBox가 MediaBox 밖(0×0). 좌표를 0~1로 바꿀 수 없다
            raise ParseError("PDF page has an empty box", location)
        textpage = page.get_textpage()
        seen: set[int] = set()
        hangul_fonts: dict[int, object] = {}
        try:
            chars = tuple(_chars(textpage, box, rotation, seen, hangul_fonts))
        finally:
            textpage.close()
        check.add_page(pdf, page, seen, hangul_fonts, location)
        boxes = list(_image_boxes(page))  # 한 번만 훑어 image_coverage·images가 같이 쓴다
        return PageText(page=index + 1, width_pt=width, height_pt=height, rotation=rotation, chars=chars,
                        image_coverage=tuple(_image_coverage(boxes, box)),
                        rules=_rules(page, box, rotation, width, height),
                        images=tuple(itertools.islice(_image_rects(boxes, box, rotation), MAX_IMAGE_OBJECTS)),
                        paths=_path_count(page))
    finally:
        page.close()


class _Fonts:
    """글꼴 핸들별 (1pt당 ascent, 1pt당 descent, 이름이 굵은 글꼴인지)."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple[float, float, bool]] = {}

    def get(self, font: object) -> tuple[float, float, bool]:
        key = _address(font)
        if key not in self._cache:
            ascent, descent, one = ctypes.c_float(), ctypes.c_float(), ctypes.c_float(1.0)
            if not (pdfium_c.FPDFFont_GetAscent(font, one, ascent) and pdfium_c.FPDFFont_GetDescent(font, one, descent)):
                ascent.value = descent.value = 0.0
            length = pdfium_c.FPDFFont_GetBaseFontName(font, None, 0)
            name = ctypes.create_string_buffer(max(length, 1))
            pdfium_c.FPDFFont_GetBaseFontName(font, name, length)
            self._cache[key] = (ascent.value, descent.value, bool(_BOLD_NAME.search(name.value.decode("latin-1"))))
        return self._cache[key]


def _base_font_name(font: object) -> str:
    length = pdfium_c.FPDFFont_GetBaseFontName(font, None, 0)
    name = ctypes.create_string_buffer(max(length, 1))
    pdfium_c.FPDFFont_GetBaseFontName(font, name, length)
    for encoding in ("utf-8", "cp949"):  # 한글 이름은 UTF-8이나 EUC-KR(#B9#D9…) 바이트로 온다
        try:
            return name.value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return name.value.decode("latin-1")


@dataclass(slots=True)
class _HangulCheck:
    """PDFium의 텍스트 쪽은 상자 너비가 0에 가까운 글자 객체를 건너뛴다. 객체 상자는 글리프 외곽으로 재므로 미임베드
    글꼴의 글리프를 이 컴퓨터의 어떤 글꼴에서도 찾지 못하면 한 글자짜리 객체가 글자째 빠진다(실측: 한글 글꼴 없는
    Linux). 공백 한 칸 객체도 모든 OS에서 빠지고 빠진 객체의 내용은 알 수 없으므로 문서 전체를 보고 판단한다.
    빠진 객체의 미임베드 글꼴이 이 컴퓨터에서 '가'를 그리지 못하고, 그 글꼴 이름이 한국어 글꼴이거나 문서에서 나온
    한글을 그린 글꼴 중 '가'를 그리는 것이 하나도 없으면(이 컴퓨터에 한글 글꼴이 없다) ParseError. 라틴 문서의
    공백 객체나, 한글이 제대로 그려지는 컴퓨터에서 한글을 담을 수 없는 중국·일본 글꼴의 공백 객체는 통과한다.
    리눅스에서 hanji-fonts(번들 한글 글꼴)가 설치되어 있으면 open_pdf가 PDFium에 등록하므로 이 상황이 생기지 않는다."""

    hangul: bool = False  # 한글 글자가 나왔다
    rendered: bool = False  # 그 한글을 낸 글꼴 중 하나가 이 컴퓨터에서 '가'를 그린다
    dropped: list[tuple[str, bool]] = field(default_factory=list)  # (쪽 위치, 글꼴 이름이 한국어인지)

    def add_page(self, pdf: pdfium.PdfDocument, page: pdfium.PdfPage, seen: set[int],
                 hangul_fonts: dict[int, object], location: str) -> None:
        """seen은 텍스트 쪽에 글자가 있는 객체 주소, hangul_fonts는 한글 글자를 낸 글꼴(주소 → 핸들).
        글꼴 핸들은 쪽을 닫으면 풀릴 수 있어 쪽마다 시험한다."""
        draws: dict[int, bool] = {}

        def can_draw(font: object) -> bool:
            key = _address(font)
            if key not in draws:
                draws[key] = _draws_hangul(pdf, font)
            return draws[key]

        self.hangul = self.hangul or bool(hangul_fonts)
        self.rendered = self.rendered or any(can_draw(font) for font in hangul_fonts.values())
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT]):
            if _address(obj.raw) in seen:
                continue
            if pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw) == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE:
                continue  # 숨은 글자(스캔 쪽 OCR 글자층)는 Char.invisible로 어차피 버린다: 빠져도 잃은 글자가 아니다
            font = pdfium_c.FPDFTextObj_GetFont(obj.raw)
            if font and pdfium_c.FPDFFont_GetIsEmbedded(font) != 1 and not can_draw(font):
                self.dropped.append((location, bool(_KOREAN_FONT.search(_base_font_name(font)))))

    def raise_if_lost(self) -> None:
        for location, korean_name in self.dropped:
            if korean_name or (self.hangul and not self.rendered):
                raise ParseError("PDF text uses a non-embedded font that has no Hangul glyphs on this system; "
                                 "install a Korean font (e.g. fonts-noto-cjk) or pip install \"hanji[fonts]\"",
                                 location)


def _draws_hangul(pdf: pdfium.PdfDocument, font: object) -> bool:
    """이 글꼴로 '가'를 그리면 상자가 생기는가(PDFium이 대신 쓸 한글 글리프를 찾았는가). 쪽에 넣지 않는 임시 객체."""
    obj = pdfium_c.FPDFPageObj_CreateTextObj(pdf.raw, font, ctypes.c_float(1.0))
    if not obj:
        return False
    try:
        text = ctypes.create_string_buffer("가\0".encode("utf-16-le"))
        if not pdfium_c.FPDFText_SetText(obj, ctypes.cast(text, ctypes.POINTER(pdfium_c.FPDF_WCHAR))):
            return False
        left, bottom, right, top = (ctypes.c_float() for _ in range(4))
        return bool(pdfium_c.FPDFPageObj_GetBounds(obj, left, bottom, right, top)) and right.value > left.value
    finally:
        pdfium_c.FPDFPageObj_Destroy(obj)


def _chars(textpage: pdfium.PdfTextPage, box: Box, rotation: int, seen: set[int],
           hangul_fonts: dict[int, object]) -> Iterator[Char]:
    fonts = _Fonts()
    modes: dict[int, int] = {}
    count = textpage.count_chars()
    skip = False
    n = 0  # 낸 글자 수 = 다음 Char.id(PDFium이 끼운 글자·쪽 밖 글자처럼 건너뛴 글자는 세지 않는다)
    for i in range(count):
        if skip:
            skip = False
            continue
        if pdfium_c.FPDFText_IsGenerated(textpage, i) == 1:  # PDFium이 끼운 공백·줄바꿈
            continue
        obj = pdfium_c.FPDFText_GetTextObject(textpage, i)
        if not obj:
            continue
        code = pdfium_c.FPDFText_GetUnicode(textpage, i)
        high = 0xD800 <= code < 0xDC00 and i + 1 < count  # UTF-16 대리쌍의 앞 절반
        following = pdfium_c.FPDFText_GetUnicode(textpage, i + 1) if high else None
        text, unmapped, skip = decode_unicode(code, following)
        unmapped = unmapped or pdfium_c.FPDFText_HasUnicodeMapError(textpage, i) == 1
        if unmapped:
            text = "\ufffd"
        key = _address(obj)
        seen.add(key)
        if key not in modes:
            modes[key] = pdfium_c.FPDFTextObj_GetTextRenderMode(obj)
        font = pdfium_c.FPDFTextObj_GetFont(obj)
        if _HANGUL.match(text):
            hangul_fonts[_address(font)] = font
        ascent, descent, bold_name = fonts.get(font)
        font_size = pdfium_c.FPDFText_GetFontSize(textpage, i)
        m = pdfium_c.FS_MATRIX()
        pdfium_c.FPDFText_GetMatrix(textpage, i, m)
        ox, oy = ctypes.c_double(), ctypes.c_double()
        pdfium_c.FPDFText_GetCharOrigin(textpage, i, ox, oy)
        width = ctypes.c_float()
        # 너비는 유니코드 → 글자 코드 역매핑으로 찾는다. 같은 유니코드에 글자 코드가 여럿이면 하나만 보므로
        # 너비가 조금 다를 수 있다(bbox·공백 판단에만 영향, 글자는 그대로).
        has_width = pdfium_c.FPDFFont_GetGlyphWidth(font, ord(text[0]), ctypes.c_float(abs(font_size)), width)
        sign = math.copysign(1.0, font_size)  # Tf가 음수면 진행·위쪽이 모두 뒤집힌다(180°)
        axes = _reading_axes((m.a * sign, m.b * sign), (m.c * sign, m.d * sign), box, rotation)
        if has_width and width.value > 0 and ascent > descent:
            # Tf가 음수면 글자가 원점에서 왼쪽·아래로 뒤집혀 그려진다: 진행 폭도 높이처럼 Tf 부호를 따른다
            advance = math.copysign(width.value, font_size)
            corners = [(ox.value + m.a * x + m.c * y, oy.value + m.b * x + m.d * y)
                       for x in (0.0, advance) for y in (descent * font_size, ascent * font_size)]
        else:  # 글꼴 정보가 없으면 PDFium의 느슨한 상자(대체 글꼴에 따라 달라질 수 있다)
            rect = pdfium_c.FS_RECTF()
            pdfium_c.FPDFText_GetLooseCharBox(textpage, i, rect)
            corners = [(rect.left, rect.bottom), (rect.right, rect.top)]
        points = [normalize_point(x, y, box, rotation) for x, y in corners]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        if not (0 <= cx <= 1 and 0 <= cy <= 1):  # 쪽 밖 글자는 보이지 않는다
            continue
        weight = pdfium_c.FPDFText_GetFontWeight(textpage, i)
        mode = modes[key]
        yield Char(text=text, x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
                   baseline=_on_axis(*normalize_point(ox.value, oy.value, box, rotation), axes[1]),
                   size=abs(font_size) * math.hypot(m.c, m.d),  # Tf가 음수면 글자가 뒤집힐 뿐 크기는 양수
                   bold=weight >= _BOLD_WEIGHT or bold_name or mode == pdfium_c.FPDF_TEXTRENDERMODE_FILL_STROKE,
                   invisible=mode == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE, unmapped=unmapped, axes=axes, id=n)
        n += 1


_MAX_FORM_DEPTH = 15


def _clip_box(obj: pdfium.PdfObject, matrix: pdfium.PdfMatrix | None) -> Box | None:
    """객체 클리핑 경로들의 교집합 상자(쪽 좌표, 경로마다 점들의 외접 상자). 클리핑이 없거나 읽지 못하면 None,
    읽지 못한 경로 하나는 건너뛴다(넓게 잡는 쪽으로). 폼 안 객체의 경로는 폼 좌표라 matrix로 옮긴다(실측)."""
    clip = pdfium_c.FPDFPageObj_GetClipPath(obj)
    if not clip:
        return None
    out: Box | None = None
    for path in range(max(pdfium_c.FPDFClipPath_CountPaths(clip), 0)):
        points = []
        for index in range(max(pdfium_c.FPDFClipPath_CountPathSegments(clip, path), 0)):
            segment = pdfium_c.FPDFClipPath_GetPathSegment(clip, path, index)
            x, y = ctypes.c_float(), ctypes.c_float()
            if not (segment and pdfium_c.FPDFPathSegment_GetPoint(segment, x, y)):
                points = []
                break
            points.append(matrix.on_point(x.value, y.value) if matrix is not None else (x.value, y.value))
        if not points:
            continue
        box = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points),
               max(p[1] for p in points))
        out = box if out is None else _intersect(out, box)
    return out


def _intersect(a: Box, b: Box | None) -> Box:
    """겹치지 않으면 폭이나 높이가 음수인 상자(면적 계산에서 0)."""
    if b is None:
        return a
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


def _image_boxes(page: pdfium.PdfPage, form: pdfium.PdfObject | None = None, matrix: pdfium.PdfMatrix | None = None,
                 clip: Box | None = None, depth: int = 0) -> Iterator[Box]:
    """그림마다 보이는 부분의 쪽 좌표 상자(그림 상자 ∩ 클리핑 상자). 폼 XObject 안 객체의 get_bounds()·클리핑
    경로는 폼 좌표라(실측) 폼 행렬을 거쳐 옮긴다. 폼 객체의 클리핑은 폼 안 객체에도 적용된다.
    폼 상자 자체는 그림으로 보지 않는다(쪽 전체 서식 폼 안의 작은 로고가 쪽 전체 그림이 되지 않게)."""
    for obj in page.get_objects(max_depth=1, form=form):
        if obj.type not in (pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_FORM):
            continue
        own = _clip_box(obj, matrix)
        visible = clip if own is None else _intersect(own, clip)
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            bounds = obj.get_bounds()
            yield _intersect(matrix.on_rect(*bounds) if matrix is not None else bounds, visible)
        elif depth < _MAX_FORM_DEPTH:
            inner = obj.get_matrix() if matrix is None else obj.get_matrix().multiply(matrix)
            yield from _image_boxes(page, obj, inner, visible, depth + 1)


def _image_coverage(images: list[Box], box: Box) -> Iterator[float]:
    """그림(_image_boxes)마다 쪽 상자 안에 보이는 면적 / 쪽 면적."""
    left, bottom, right, top = box
    area = (right - left) * (top - bottom)
    for x0, y0, x1, y1 in images:
        overlap = max(0.0, min(x1, right) - max(x0, left)) * max(0.0, min(y1, top) - max(y0, bottom))
        yield min(1.0, overlap / area)


def _image_rects(images: list[Box], box: Box, rotation: int) -> Iterator[Box01]:
    """그림(_image_boxes)마다 쪽 상자 안에 보이는 부분을 보이는 쪽 0~1로(렌더 그림과 같은 틀: 회전·CropBox 반영).
    쪽 밖·넓이 0은 뺀다."""
    for rect in images:
        left, bottom, right, top = _intersect(rect, box)
        if right <= left or top <= bottom:
            continue
        (x0, y0), (x1, y1) = normalize_point(left, bottom, box, rotation), normalize_point(right, top, box, rotation)
        yield (min(max(min(x0, x1), 0.0), 1.0), min(max(min(y0, y1), 0.0), 1.0),
               min(max(max(x0, x1), 0.0), 1.0), min(max(max(y0, y1), 0.0), 1.0))


Matrix = tuple[float, float, float, float, float, float]  # a b c d e f: (x, y) → (ax + cy + e, bx + dy + f)
Point = tuple[float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _then(inner: Matrix, outer: Matrix) -> Matrix:
    """inner를 적용한 뒤 outer를 적용하는 행렬."""
    a, b, c, d, e, f = inner
    p, q, r, s, t, u = outer
    return (a * p + b * r, a * q + b * s, c * p + d * r, c * q + d * s, e * p + f * r + t, e * q + f * s + u)


def _object_matrix(obj: object) -> Matrix:
    m = pdfium_c.FS_MATRIX()
    if not pdfium_c.FPDFPageObj_GetMatrix(obj, m):
        return _IDENTITY
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def _path_objects(parent: object, form: bool, matrix: Matrix, depth: int = 0) -> Iterator[tuple[object, Matrix]]:
    """(path 객체, path 좌표 → 쪽 좌표 행렬). path 점은 객체 행렬을 적용하기 전 좌표이고, 폼 안 객체의 행렬은
    폼 좌표로 옮긴다(실측). 폼은 _MAX_FORM_DEPTH 단계까지 들어간다."""
    count = (pdfium_c.FPDFFormObj_CountObjects if form else pdfium_c.FPDFPage_CountObjects)(parent)
    get = pdfium_c.FPDFFormObj_GetObject if form else pdfium_c.FPDFPage_GetObject
    for i in range(max(count, 0)):
        obj = get(parent, i)
        if not obj:
            continue
        kind = pdfium_c.FPDFPageObj_GetType(obj)
        if kind == pdfium_c.FPDF_PAGEOBJ_PATH:
            yield obj, _then(_object_matrix(obj), matrix)
        elif kind == pdfium_c.FPDF_PAGEOBJ_FORM and depth < _MAX_FORM_DEPTH:
            yield from _path_objects(obj, True, _then(_object_matrix(obj), matrix), depth + 1)


def _path_count(page: pdfium.PdfPage) -> int:
    """path 객체 수(폼 안 포함). MAX_PATH_COUNT에서 멈춘다(레이아웃 모델을 돌릴지 정하는 데만 쓴다)."""
    return sum(1 for _ in itertools.islice(_path_objects(page.raw, False, _IDENTITY), MAX_PATH_COUNT))


def _visible_color(getter: Callable[..., int], obj: object) -> bool:
    """하얀색(배경과 같은 색)·완전 투명이면 False. 색을 읽지 못하면(무늬 색 등) 보이는 것으로 본다."""
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not getter(obj, r, g, b, a):
        return True
    return a.value > 0 and min(r.value, g.value, b.value) < WHITE


@dataclass(slots=True)
class _Subpath:
    points: list[Point]
    edges: list[tuple[Point, Point]]  # 직선 변(닫는 변 포함)
    curved: bool = False


def _subpaths(obj: object, matrix: Matrix, box: Box, rotation: int, width: float, height: float) -> list[_Subpath]:
    """보이는 쪽 pt 좌표(회전 보정, 원점 왼쪽 위)의 부분 경로들. 곡선(베지에) 구간은 변으로 넣지 않는다.
    점을 읽지 못하면 빈 목록."""
    a, b, c, d, e, f = matrix
    out: list[_Subpath] = []
    x, y = ctypes.c_float(), ctypes.c_float()
    for k in range(max(pdfium_c.FPDFPath_CountSegments(obj), 0)):
        segment = pdfium_c.FPDFPath_GetPathSegment(obj, k)
        if not (segment and pdfium_c.FPDFPathSegment_GetPoint(segment, x, y)):
            return []
        u, v = normalize_point(a * x.value + c * y.value + e, b * x.value + d * y.value + f, box, rotation)
        point = (u * width, v * height)
        kind = pdfium_c.FPDFPathSegment_GetType(segment)
        if kind == pdfium_c.FPDF_SEGMENT_MOVETO or not out:
            out.append(_Subpath([point], []))
        else:
            sub = out[-1]
            if kind == pdfium_c.FPDF_SEGMENT_LINETO:
                sub.edges.append((sub.points[-1], point))
            else:
                sub.curved = True
            sub.points.append(point)
        if pdfium_c.FPDFPathSegment_GetClose(segment) and out[-1].points[-1] != out[-1].points[0]:
            out[-1].edges.append((out[-1].points[-1], out[-1].points[0]))
    return out


def _edge_rule(p: Point, q: Point) -> Rule | None:
    """가로·세로 직선 변이면 stroke Rule(길이 DASH 이상, MIN_RULE 미만이면 점선 조각), 사선이면 None."""
    (x0, y0), (x1, y1) = p, q
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    if dy <= AXIS_TOL and dx >= DASH - LEN_EPS and dy < dx:
        return Rule("h", (y0 + y1) / 2, min(x0, x1), max(x0, x1))
    if dx <= AXIS_TOL and dy >= DASH - LEN_EPS and dx < dy:
        return Rule("v", (x0 + x1) / 2, min(y0, y1), max(y0, y1))
    return None


def _rect(sub: _Subpath) -> tuple[float, float, float, float] | None:
    """곡선이 없고 꼭짓점이 모두 외접 상자의 모서리 ± AXIS_TOL, 네 모서리가 모두 있고 이웃 꼭짓점 사이(닫는 변 포함)가
    모두 가로·세로(± AXIS_TOL)이며 둘러싼 넓이가 외접 상자 넓이인 사각형이면 (left, top, right, bottom), 아니면
    None(대각선으로 도는 나비 모양, 되짚어 가는 길 등)."""
    if sub.curved or len(sub.points) < 4:
        return None
    if any(min(abs(x1 - x0), abs(y1 - y0)) > AXIS_TOL
           for (x0, y0), (x1, y1) in zip(sub.points, sub.points[1:] + sub.points[:1], strict=True)):
        return None
    xs, ys = [p[0] for p in sub.points], [p[1] for p in sub.points]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    if any(min(abs(x - left), abs(x - right)) > AXIS_TOL or min(abs(y - top), abs(y - bottom)) > AXIS_TOL
           for x, y in sub.points):
        return None
    if not all(any(abs(x - cx) <= AXIS_TOL and abs(y - cy) <= AXIS_TOL for x, y in sub.points)
               for cx in (left, right) for cy in (top, bottom)):  # 예: 채운 직각삼각형
        return None
    # 둘러싼 넓이(신발끈 공식, 겹친 점은 더해지지 않는다)가 외접 상자 넓이와 같아야 한다: 되짚어 가는 길·나비 모양은
    # 넓이가 0이나 그보다 작다. 여유는 1% 또는 꼭짓점이 AXIS_TOL만큼 어긋날 때의 넓이 차
    w, h = right - left, bottom - top
    pts = sub.points
    area = abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1], strict=True))) / 2
    if abs(area - w * h) > max(0.01 * w * h, AXIS_TOL * (w + h)):
        return None
    return left, top, right, bottom


def _fill_rules(sub: _Subpath) -> list[Rule]:
    """채운 사각형(_rect): 두 변이 모두 THIN보다 길면 네 변(fill), 아니면 짧은 쪽이 THIN 이하인 방향의 가운데
    선(stroke. 두 변 차가 AXIS_TOL 이하인 네모 점은 가로·세로 둘 다: 점선 조각). 넓이 0(보이지 않는다)이거나 사각형이
    아니면 없음."""
    rect = _rect(sub)
    if rect is None:
        return []
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if min(w, h) < 1e-6:  # 넓이 0인 채움은 그려지지 않는다
        return []
    if min(w, h) > THIN:
        return [Rule("h", top, left, right, "fill"), Rule("h", bottom, left, right, "fill"),
                Rule("v", left, top, bottom, "fill"), Rule("v", right, top, bottom, "fill")]
    out = []
    # 점선 조각(두 변 모두 MIN_RULE 미만)이면 두 변의 비교에 AXIS_TOL 여유를 둔다(좌표 변환의 부동소수 오차로 네모 점이
    # 한 방향만 내지 않게). 그보다 큰 사각형은 그대로 비교해 긴 쪽 방향만
    slack = AXIS_TOL if max(w, h) < MIN_RULE - LEN_EPS else 0.0
    if h <= THIN and w >= max(DASH - LEN_EPS, h - slack):
        out.append(Rule("h", (top + bottom) / 2, left, right))
    if w <= THIN and h >= max(DASH - LEN_EPS, w - slack):
        out.append(Rule("v", (left + right) / 2, top, bottom))
    return out


def _join_dashes(dashes: list[Rule]) -> Iterator[Rule]:
    """점선 조각(MIN_RULE보다 짧은 선분)을 같은 축·같은 위치(위치 순으로 이웃 조각과 ± AXIS_TOL, 묶음 첫 조각과
    2 × AXIS_TOL 안)에서 사이 ≤ DASH_GAP이면 이어 한 선(stroke)으로.
    조각 둘 이상을 이어 MIN_RULE 이상이 된 것만 낸다(글자 모양 조각·눈금 하나는 버린다)."""
    for axis in ("h", "v"):
        groups: list[list[Rule]] = []
        for dash in sorted((d for d in dashes if d.axis == axis), key=lambda d: d.pos):
            # 바로 앞 조각과 비교(천천히 흐르는 점선)하되 묶음 첫 조각에서 2 × AXIS_TOL 안까지만
            if groups and dash.pos - groups[-1][-1].pos <= AXIS_TOL and dash.pos - groups[-1][0].pos <= 2 * AXIS_TOL:
                groups[-1].append(dash)
            else:
                groups.append([dash])
        for group in groups:
            chain: list[Rule] = []
            end = 0.0
            for dash in sorted(group, key=lambda d: d.start):
                if chain and dash.start - end > DASH_GAP:
                    yield from _chain(chain, end)
                    chain = []
                end = max(end, dash.end) if chain else dash.end
                chain.append(dash)
            yield from _chain(chain, end)


def _chain(chain: list[Rule], end: float) -> Iterator[Rule]:
    """조각 둘 이상, 길이 MIN_RULE 이상이고 곧은(위치 흐름 ≤ AXIS_TOL 또는 ≤ 길이 × DASH_DRIFT) 이음만 선."""
    if len(chain) < 2 or end - chain[0].start < MIN_RULE - LEN_EPS:
        return
    drift = max(d.pos for d in chain) - min(d.pos for d in chain)
    if drift <= AXIS_TOL or drift <= DASH_DRIFT * (end - chain[0].start):
        yield Rule(chain[0].axis, sum(d.pos for d in chain) / len(chain), chain[0].start, end)


def _clipped(rule: Rule, width: float, height: float) -> Rule | None:
    """쪽 밖 부분을 잘라 낸다. 쪽 밖이거나 잘라서 MIN_RULE보다 짧아지면 None. 좌표는 소수 셋째 자리."""
    span, depth = (width, height) if rule.axis == "h" else (height, width)
    start, end = max(rule.start, 0.0), min(rule.end, span)
    if not 0 <= rule.pos <= depth or end - start < MIN_RULE - LEN_EPS:
        return None
    return Rule(rule.axis, round(rule.pos, 3), round(start, 3), round(end, 3), rule.kind)


def _rules(page: pdfium.PdfPage, box: Box, rotation: int, width: float, height: float) -> tuple[Rule, ...]:
    """path 객체(폼 XObject 안 포함)의 가로·세로 선분. 그은 path의 직선 변은 stroke, 채운 사각형은 _fill_rules.
    하얀색·투명 선과 채움, 사선·곡선, 이어도 MIN_RULE보다 짧은 선분, 쪽 밖은 버린다. 좌표는 글자와 같은 보이는
    쪽 틀(회전 보정, 원점 왼쪽 위)의 pt. 순서는 그린 순서, 점선은 끝에. 그리고 채운 path는 stroke와 fill을 둘 다 낸다.
    알려진 한계: 클리핑 경로는 보지 않는다(잘려 안 보이는 선도 낸다).
    훑은 path 구간이나 점선 조각이 MAX_RULE_SEGMENTS개를 넘으면 그 쪽은 ()(표 검출을 건너뛴다. 글자는 그대로)."""
    fill_mode, stroke = ctypes.c_int(), ctypes.c_int()
    out: list[Rule] = []
    dashes: list[Rule] = []
    budget = MAX_RULE_SEGMENTS
    for obj, matrix in _path_objects(page.raw, False, _IDENTITY):
        budget -= max(pdfium_c.FPDFPath_CountSegments(obj), 1)  # 점을 읽기 전에 센다(넘으면 바로 멈춘다)
        if budget < 0:
            return ()
        if not pdfium_c.FPDFPath_GetDrawMode(obj, fill_mode, stroke):
            continue
        stroked = bool(stroke.value) and _visible_color(pdfium_c.FPDFPageObj_GetStrokeColor, obj)
        filled = (fill_mode.value != pdfium_c.FPDF_FILLMODE_NONE
                  and _visible_color(pdfium_c.FPDFPageObj_GetFillColor, obj))
        if not (stroked or filled):
            continue
        subs = _subpaths(obj, matrix, box, rotation, width, height)
        # 부분 경로가 여럿인 채운 path(속이 빈 틀, 칸 여러 개의 틀, 사각형과 다른 모양이 섞인 것)의 사각형은 채움
        # 규칙(even-odd·nonzero)을 따지지 않고 테두리로 본다: 넓은 사각형의 네 변도 fill이 아니라 stroke. 얇은 사각형은
        # 그대로(점선 조각), 사각형이 아닌 부분 경로는 무시. 맞바꾼 것: 실제로 속까지 칠한 배경을 이런 path로 그리면
        # 머리행 표시를 잃는다(머리행은 덧붙인 정보일 뿐이다)
        border = filled and len(subs) > 1
        for sub in subs:
            found = [_edge_rule(p, q) for p, q in sub.edges] if stroked else []
            fills = _fill_rules(sub) if filled else []
            if border:
                fills = [Rule(r.axis, r.pos, r.start, r.end) for r in fills]
            for rule in found + fills:
                if rule is None:
                    continue
                if rule.end - rule.start < MIN_RULE - LEN_EPS:
                    dashes.append(rule)
                elif (clipped := _clipped(rule, width, height)) is not None:
                    out.append(clipped)
        if len(dashes) > MAX_RULE_SEGMENTS:
            return ()
    out.extend(clipped for rule in _join_dashes(dashes) if (clipped := _clipped(rule, width, height)) is not None)
    return tuple(out)
