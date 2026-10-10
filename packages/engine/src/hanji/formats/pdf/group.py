"""보이는 글자 → 줄 조각 → 블록 명세(제목·문단·목록·머리말·꼬리말). 길이 단위는 pt.

줄·순서·간격은 글자마다 읽기 좌표(x = 진행 방향, y = 줄 아래 방향, 원점은 그 방향으로 읽을 때의 왼쪽 위)에서
잰다. 바로 선 글자면 보이는 쪽 좌표와 같고, 회전한 쪽·음수 Tf·거울 글자도 진행 방향 순서대로 읽는다."""

import re
import unicodedata
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from hanji_contracts import TextLayerState

from .extract import UPRIGHT, Axes, Char, PageText
from .scan import OcrParagraph
from .triage import PageMode, page_mode

if TYPE_CHECKING:  # tables.py·figures.py가 이 모듈을 import하므로 실행 중에는 가져오지 않는다
    from .figures import Figure
    from .tables import TableSpec

SAME_LINE = 0.5  # 기준선 차이 ≤ 실제 크기 × 0.5면 같은 줄
SPLIT_GAP = 3.0  # 줄 안 글자 간격 > 실제 크기 × 3이면 줄 조각을 나눈다(표 칸·다단)
SPACE_GAP = 0.2  # 글자 간격 − 보통 자간 > 실제 크기 × 0.2면 공백을 끼운다(공백 글자가 없는 PDF, 공공누리로 조정)
SIZE_STEP = 0.5  # 크기는 0.5pt 단위로 묶는다
HEADING_RATIO = 1.15  # 제목: 본문 크기 × 1.15 이상
BOLD_HEADING_RATIO = 1.05  # 굵으면 × 1.05 이상
HEADING_MAX_LINES = 2  # 이어지는 제목 크기 줄이 이보다 많으면 문단
MAX_LEVEL = 6
PARA_GAP = 0.8  # 같은 블록: 줄 사이 빈 간격 ≤ 줄 높이 × 0.8
PARA_SIZE_DIFF = 0.5  # 크기 차 ≤ 0.5pt
PARA_INDENT = 1.0  # 왼쪽 시작 차 ≤ 본문 크기 × 1
MARGIN = 0.08  # 머리말·꼬리말 영역: 쪽 높이의 위·아래 8%(줄의 세로 중심 기준)
SAME_POSITION = 0.02  # 같은 위치: 세로 중심 차 ≤ 쪽 높이의 2%
MIN_PAGES_FOR_REPEAT = 3
UNRELIABLE_CONFIDENCE = 0.2  # 깨진 글자층으로 블록을 만든(layer 모드) unreliable 쪽 블록의 신뢰도 상한(쪽 단위)
BORDERLESS_CONFIDENCE = 0.4  # 선 없는 표 블록(정책값, 보정된 확률 아님: 칸 배정·칸 안 순서를 장담하지 못한다)
RESCUED_CONFIDENCE = 0.3  # 구조 문단(어느 블록에도 들지 않은 글자를 줄로 묶어 살린 문단: 배치·순서를 모른다)
CONFIDENCE = {"paragraph": 0.7, "list_item": 0.7, "heading": 0.6, "page_header": 0.8, "page_footer": 0.8,
              "table": 0.6, "figure": 0.7, "caption": 0.7}  # 그림·캡션은 모델 점수(CPU마다 다르다)를 넣지 않는다
# 스펙 §5.2-5의 앞머리 + 공공누리에서 본 글머리표(ㅇ ㆍ · ∙ ‣ ▸ ▪ ⇨ →). 차례 글자는 가~하 열네 글자뿐
# (유니코드 범위 가-하가 아니다). 숫자 뒤에 "10. "·"10.03"처럼 숫자 차례가 또 오면 날짜("2026. 10. 3.",
# "2026. 10.03.")라 표지가 아니다. "1.5배"처럼 숫자 차례 뒤가 숫자·공백이 아니면 표지다("2. 1.5배 증가").
LIST_MARKER = re.compile(
    r"^\s*(\d+[.)](?!\s*\d+[.)](?:\s|$|\d+[.)]))|\(\d+\)|[가나다라마바사아자차카타파하][.)]|[①-⑳]"
    r"|[□■○●◦◆◇▶▷\-–•※ㅇㆍ·∙‣▸▪⇨→])\s")
_OPENERS = frozenset("(〈《「『[［（\"“'‘")  # 여는 괄호·따옴표는 앞머리가 아니다("( 단위: 백만원 )")
_DIGITS = re.compile(r"\d")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Fragment:
    """줄 조각. 좌표는 읽기 좌표 pt(axes 방향, 바로 선 글자면 보이는 쪽 좌표), size는 0.5pt 단위 대표 크기
    (가장 많은 크기, 같으면 큰 쪽)."""

    page: int
    text: str
    chars: tuple[Char, ...]  # 공백이 아닌 글자(읽기 좌표 0~1). Char.id는 쪽 글자 순번 그대로다
    x0: float
    y0: float
    x1: float
    y1: float
    baseline: float
    size: float
    bold: bool  # 글자 과반이 굵다
    content_x0: float  # 앞머리(목록 표지 또는 글자·숫자가 아닌 한 글자) 다음 글자의 시작. 앞머리가 없으면 x0
    axes: Axes = UPRIGHT


@dataclass(frozen=True, slots=True)
class FigureBlock:
    """그림 블록 하나(+짝 캡션). figure는 figures.Figure(보이는 쪽 pt), text_source는 글자 출처(text_layer·ocr),
    image는 FigureImage 필드(asset·mime·width_px·height_px·dpi·category) 또는 None(자른 그림을 담지 못했다)."""

    figure: "Figure"
    text_source: str
    image: dict[str, Any] | None = None

    @property
    def char_ids(self) -> frozenset[int]:
        """그림과 짝 캡션이 가져간 텍스트 레이어 글자(page.chars 순번): 줄·조각에서 뺀다."""
        caption = self.figure.caption
        return self.figure.char_ids | (caption.char_ids if caption is not None else frozenset())

    def top(self, page: PageText) -> float:
        """블록 묶음(위 캡션이 있으면 캡션부터)의 보이는 쪽 윗변(0~1)."""
        caption = self.figure.caption
        y = caption.box[1] if caption is not None and caption.above else self.figure.box[1]
        return y / page.height_pt


@dataclass(frozen=True, slots=True)
class Ledger:
    """쪽 하나에서 블록이 된 텍스트 레이어 글자(보이는, 공백이 아닌 글자)를 글자 id(page.chars 순번)로 센다. in_blocks는
    서로 다른 id 수, doubled는 이미 다른 블록에 든 id를 또 넣은 횟수(블록 둘이 같은 글자를 나눠 가졌다: 버그 신호)와
    쪽에 없는 글자 순번을 받은 횟수(이것도 버그 신호). rescued는 구조 문단으로 살린 글자 수로 in_blocks 안에서 센다
    (따로 더하지 않는다: TextCoverage 검사가 rescued ≤ in_blocks를 본다)."""

    in_blocks: int = 0
    doubled: int = 0
    rescued: int = 0  # in_blocks 가운데 구조 문단(쪽 블록 끝)으로 살린 글자


def step(size: float) -> float:
    return round(size / SIZE_STEP) * SIZE_STEP


def frame_size(page: PageText, axes: Axes) -> tuple[float, float]:
    """읽기 좌표의 (너비, 높이) pt. 진행 방향이 보이는 쪽의 세로면 너비·높이가 바뀐다."""
    w, h = page.width_pt, page.height_pt
    return (w, h) if axes[0] % 2 == 0 else (h, w)


def _span(lo: float, hi: float, axis: int) -> tuple[float, float]:
    """보이는 쪽 축 위 구간(0~1) → axis 방향(+x·+y·−x·−y = 0·1·2·3)으로 잰 구간. 같은 식이 역변환이다."""
    return (lo, hi) if axis < 2 else (1 - hi, 1 - lo)


def _turn(c: Char) -> Char:
    """글자 상자를 읽기 좌표(0~1)로 옮긴다. baseline은 이미 읽기 좌표다."""
    if c.axes == UPRIGHT:
        return c
    (x0, x1), (y0, y1) = (_span(*((c.x0, c.x1) if axis % 2 == 0 else (c.y0, c.y1)), axis) for axis in c.axes)
    return replace(c, x0=x0, y0=y0, x1=x1, y1=y1)


def _fragment(page: PageText, chars: Sequence[Char], axes: Axes) -> Fragment | None:
    """공백 글자 없이 벌어진 곳에 공백을 끼운다. 간격에서 보통 자간을 뺀 값으로 본다: 보통 자간은 글자·숫자끼리
    간격의 중앙값(음수일 때만, 목차 점선 같은 기호는 빼고). 공백만 있으면 None. chars는 읽기 좌표."""
    w, h = frame_size(page, axes)
    gaps = sorted((b.x0 - a.x1) * w for a, b in zip(chars, chars[1:]) if a.text.isalnum() and b.text.isalnum())
    # 중앙값. 작은 쪽 중앙값은 실제 문서에서 남는 공백을 늘려 쓰지 않음. 자간을 좁힌 문서(한글 -25% 등)
    tracking = min(gaps[len(gaps) // 2], 0.0) if gaps else 0.0
    parts = [chars[0].text]
    for a, b in zip(chars, chars[1:]):
        if (not a.text.isspace() and not b.text.isspace()
                and (b.x0 - a.x1) * w - tracking > SPACE_GAP * max(a.size, b.size)):
            parts.append(" ")
        parts.append(b.text)
    text = "".join(parts).strip()
    ink = tuple(c for c in chars if not c.text.isspace())
    if not ink:
        return None
    sizes = Counter(step(c.size) for c in ink)
    skip = _marker_length(text)
    return Fragment(page=page.page, text=text, chars=ink,
                    x0=min(c.x0 for c in ink) * w, y0=min(c.y0 for c in ink) * h,
                    x1=max(c.x1 for c in ink) * w, y1=max(c.y1 for c in ink) * h,
                    baseline=ink[0].baseline * h, size=max(sizes, key=lambda s: (sizes[s], s)),
                    bold=sum(c.bold for c in ink) * 2 > len(ink),
                    content_x0=ink[skip if skip < len(ink) else 0].x0 * w, axes=axes)


def _marker_length(text: str) -> int:
    """줄 앞머리의 (공백 아닌) 글자 수: 목록 표지, 또는 공백이 뒤따르는 글자·숫자가 아닌 한 글자(✅ ➊ * 등,
    여는 괄호·따옴표는 빼고). 없으면 0."""
    marker = LIST_MARKER.match(text)
    if marker:
        return len(_SPACES.sub("", marker.group(0)))
    head = text.split(maxsplit=1)
    return 1 if (len(head) == 2 and len(head[0]) == 1 and not head[0].isalnum()
                 and head[0] not in _OPENERS) else 0


def fragments(page: PageText) -> list[Fragment]:
    """보이는 글자를 읽는 방향(axes)별로 나눠 읽기 좌표에서 기준선으로 줄에 묶고, 줄 안의 큰 간격에서 나눈다.
    순서는 방향마다 위→아래, 왼쪽→오른쪽(읽기 좌표). 방향은 글자가 많은 것부터(같으면 axes 순)."""
    by_axes: dict[Axes, list[Char]] = defaultdict(list)
    for c in page.chars:
        if not c.invisible:
            by_axes[c.axes].append(_turn(c))
    out: list[Fragment] = []
    for axes in sorted(by_axes, key=lambda a: (-len(by_axes[a]), a)):
        w, h = frame_size(page, axes)
        lines: list[list[Char]] = []
        for c in sorted(by_axes[axes], key=lambda c: (c.baseline, c.x0)):
            anchor = lines[-1][0] if lines else None
            if anchor is None or (c.baseline - anchor.baseline) * h > SAME_LINE * max(c.size, anchor.size):
                lines.append([])
            lines[-1].append(c)
        for line in lines:
            line.sort(key=lambda c: c.x0)
            start = 0
            for i in range(1, len(line) + 1):
                if i == len(line) or (line[i].x0 - line[i - 1].x1) * w > SPLIT_GAP * max(
                        line[i].size, line[i - 1].size):
                    fragment = _fragment(page, line[start:i], axes)
                    if fragment is not None:
                        out.append(fragment)
                    start = i
    return out


def body_size(pages: Sequence[Sequence[Fragment]]) -> float | None:
    """문서 전체에서 글자 수가 가장 많은 크기(0.5pt 단위). 같으면 작은 쪽. 글자가 없으면 None."""
    counts = Counter(step(c.size) for frags in pages for f in frags for c in f.chars)
    return max(counts, key=lambda s: (counts[s], -s)) if counts else None


def _center(f: Fragment, page: PageText) -> float:
    """읽기 좌표에서 조각의 세로 중심(0~1)."""
    return (f.y0 + f.y1) / 2 / frame_size(page, f.axes)[1]


def _zone(f: Fragment, page: PageText) -> str | None:
    center = _center(f, page)
    if center <= MARGIN:
        return "page_header"
    if center >= 1 - MARGIN:
        return "page_footer"
    return None


def repeated_margins(pages: Sequence[PageText], frags: Sequence[Sequence[Fragment]]) -> dict[tuple[int, int], str]:
    """{(쪽 순번, 조각 순번): page_header|page_footer}. 위·아래 8% 안의 줄이 숫자를 지운 글자 기준으로
    같은 위치(±2%)에 문서 쪽 수의 절반 이상 반복되면 머리말·꼬리말. 숫자를 지우면 글자(문자)가 남지 않는 줄은
    그 쪽의 같은 영역에 그런 줄이 하나뿐일 때만 센다(쪽 번호. 표의 숫자 칸은 여럿). 3쪽 미만 문서는 없음."""
    if len(pages) < MIN_PAGES_FOR_REPEAT:
        return {}
    candidates: dict[tuple[str, str], list[tuple[int, int, float]]] = defaultdict(list)
    for p, (page, page_frags) in enumerate(zip(pages, frags, strict=True)):
        for i, f in enumerate(page_frags):
            zone = _zone(f, page)
            if zone is not None:
                key = _SPACES.sub("", _DIGITS.sub("", unicodedata.normalize("NFC", f.text)))
                candidates[(zone, key)].append((p, i, _center(f, page)))
    letterless = Counter((zone, p) for (zone, key), items in candidates.items()
                         if not any(ch.isalpha() for ch in key) for p, _, _ in items)
    found: dict[tuple[int, int], str] = {}
    for (zone, key), items in candidates.items():
        if not any(ch.isalpha() for ch in key):
            items = [item for item in items if letterless[(zone, item[0])] == 1]
        for p, i, center in items:
            if len({q for q, _, c in items if abs(c - center) <= SAME_POSITION}) * 2 >= len(pages):
                found[(p, i)] = zone
    return found


def _widen(lo: float, hi: float) -> tuple[float, float]:
    """0~1 구간을 소수 셋째 자리로 반올림한다. 반올림으로 폭이 0이 되면 0.001 넓힌다(1이면 안쪽으로)."""
    a, b = (round(min(max(v, 0.0), 1.0), 3) for v in (lo, hi))
    if b > a:
        return a, b
    return (a, round(a + 0.001, 3)) if a < 1.0 else (round(b - 0.001, 3), b)


def unit_box(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    """보이는 쪽 0~1 상자 → 블록 bbox(소수 셋째 자리, 폭 0이면 0.001 넓힘: _widen)."""
    (a, b), (c, d) = _widen(x0, x1), _widen(y0, y1)
    return {"x0": a, "y0": c, "x1": b, "y1": d}


def _numbered(page: PageText) -> PageText:
    """Char.id가 page.chars 순번과 같은 쪽. extract가 매긴 쪽은 그대로, 아니면(손으로 만든 쪽 등) 순번을 매긴 사본."""
    if all(c.id == i for i, c in enumerate(page.chars)):
        return page
    return replace(page, chars=tuple(replace(c, id=i) for i, c in enumerate(page.chars)))


def _unassigned(page: PageText, owned: set[int]) -> list[Fragment]:
    """블록에 들지 않은(owned 밖) 보이는 공백 아닌 글자의 줄 조각, 보이는 쪽 윗변 순서(_box의 y0, 같으면 fragments
    순서). 읽는 방향과 상관없다: 180° 뒤집힌 줄도, 방향이 섞인 쪽도 보이는 쪽 위에서 아래로. 공백 글자도 넘겨 줄 안
    띄어쓰기에 쓰고, 공백·숨은 글자뿐인 줄은 fragments가 버린다. 그런 글자가 없으면 fragments를 부르지 않는다. page는
    Char.id가 순번인 쪽(_numbered)."""
    rest = tuple(c for c in page.chars if c.id not in owned)
    if all(c.invisible or c.text.isspace() for c in rest):
        return []
    return sorted(fragments(replace(page, chars=rest)), key=lambda f: _box([f], page)["y0"])


def _box(frags: Sequence[Fragment], page: PageText) -> dict[str, float]:
    """보이는 쪽 기준 0~1(읽기 좌표에서 되돌린다), 소수 셋째 자리 반올림(_widen). frags는 같은 axes."""
    axes = frags[0].axes
    w, h = frame_size(page, axes)
    spans = {axes[0] % 2: _span(min(f.x0 for f in frags) / w, max(f.x1 for f in frags) / w, axes[0]),
             axes[1] % 2: _span(min(f.y0 for f in frags) / h, max(f.y1 for f in frags) / h, axes[1])}
    (x0, x1), (y0, y1) = _widen(*spans[0]), _widen(*spans[1])
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _is_heading_size(f: Fragment, body: float) -> bool:
    return f.size >= body * (BOLD_HEADING_RATIO if f.bold else HEADING_RATIO)


def _continues(group: Sequence[Fragment], cur: Fragment, body: float) -> bool:
    """cur가 group에 이어지는가: 같은 읽는 방향, 아래 줄, 빈 간격 ≤ 줄 높이 × 0.8, 크기 차 ≤ 0.5pt, 왼쪽 시작 차
    ≤ 본문 크기(첫 줄에 앞머리가 있으면 앞머리 다음 글자의 시작도 기준: 내어쓰기), 새 목록 앞머리 아님, 제목 크기
    여부가 같음."""
    first, prev = group[0], group[-1]
    return (cur.axes == first.axes
            and cur.baseline > prev.baseline
            and cur.y0 - prev.y1 <= PARA_GAP * (prev.y1 - prev.y0)
            and abs(cur.size - prev.size) <= PARA_SIZE_DIFF
            and min(abs(cur.x0 - first.x0), abs(cur.x0 - first.content_x0)) <= PARA_INDENT * body
            and not LIST_MARKER.match(cur.text)
            and _is_heading_size(cur, body) == _is_heading_size(first, body))


def _table_corner(table: "TableSpec", page: PageText, axes: Axes) -> tuple[float, float]:
    """표 bbox(보이는 쪽 0~1)의 읽기 좌표(axes) (윗변, 왼변) pt."""
    x0, y0, x1, y1 = table.bbox
    left, _ = _span(*((x0, x1) if axes[0] % 2 == 0 else (y0, y1)), axes[0])
    top, _ = _span(*((x0, x1) if axes[1] % 2 == 0 else (y0, y1)), axes[1])
    w, h = frame_size(page, axes)
    return top * h, left * w


def _top(item: "list[Fragment] | TableSpec | OcrParagraph | FigureBlock", page: PageText) -> float:
    """블록의 보이는 쪽 윗변(0~1)."""
    if isinstance(item, list):
        return _box(item, page)["y0"]
    if isinstance(item, FigureBlock):  # 블록 상자처럼 소수 셋째 자리: 같은 높이면 텍스트가 먼저(리뷰 M4)
        return round(item.top(page), 3)
    return round(item.bbox[1], 3)  # OCR 문단도 같은 자리수로 견준다(표 bbox는 이미 셋째 자리)


def _merge_figures(paras: Sequence[OcrParagraph], page_figures: Sequence[FigureBlock],
                   page: PageText) -> list[OcrParagraph | FigureBlock]:
    """OCR 문단(XY 분할 순서)과 그림 블록(윗변 순서)을 윗변으로 합친다. 윗변이 같으면 문단이 먼저다."""
    queue = deque(sorted(page_figures, key=lambda f: f.top(page)))
    out: list[OcrParagraph | FigureBlock] = []
    for para in paras:
        while queue and _top(queue[0], page) < _top(para, page):  # 반올림한 윗변: 같으면 문단 먼저(사전 리뷰 5)
            out.append(queue.popleft())
        out.append(para)
    return out + list(queue)


def _merge_ocr(page_items: list[tuple[PageText, str | None, Any]], extras: Sequence[OcrParagraph | FigureBlock],
               page: PageText) -> list[tuple[PageText, str | None, Any]]:
    """같은 쪽의 텍스트 레이어 블록(읽기 순서)과 OCR 문단·그림 블록(_merge_figures 순서)을 윗변 기준으로 합친다.
    두 목록 안의 순서는 그대로 두고, 윗변이 같으면 텍스트 레이어 블록이 먼저다."""
    queue = deque(extras)
    out: list[tuple[PageText, str | None, Any]] = []
    for item in page_items:
        top = _top(item[2], page)
        while queue and _top(queue[0], page) < top:
            out.append((page, None, queue.popleft()))
        out.append(item)
    return out + [(page, None, extra) for extra in queue]


def _figure_specs(block: FigureBlock, page: PageText, path: tuple[str, ...], start: int) -> list[dict[str, Any]]:
    """그림 블록(+짝 캡션) 명세. 위 캡션은 그림 앞, 아래 캡션은 그림 바로 뒤. start는 이 명세들의 첫 순번: 그림의
    figure.caption_ref는 캡션 명세의 순번(엔진 core.build가 caption_block_id로 바꾼다)."""
    fig, caption = block.figure, block.figure.caption
    w, h = page.width_pt, page.height_pt

    def spec(kind: str, text: str, box: tuple[float, float, float, float]) -> dict[str, Any]:
        return {"kind": kind, "text": text, "section_path": path, "confidence": CONFIDENCE[kind], "state": "det",
                "text_source": block.text_source,
                "locator": {"kind": "page", "page": page.page,
                            "bbox": unit_box(box[0] / w, box[1] / h, box[2] / w, box[3] / h)}}

    figure = spec("figure", fig.text, fig.box)
    if caption is None:
        if block.image is not None:
            figure["figure"] = dict(block.image)
        return [figure]
    if block.image is not None:
        figure["figure"] = {**block.image, "caption_ref": start if caption.above else start + 1}
    note = spec("caption", caption.text, caption.box)
    return [note, figure] if caption.above else [figure, note]


def build_specs(pages: Sequence[PageText], states: Sequence[TextLayerState],
                tables: Sequence[Sequence["TableSpec"]] | None = None,
                ocr: Sequence[Sequence[OcrParagraph]] | None = None,
                figures: Sequence[Sequence[FigureBlock]] | None = None,
                modes: Sequence[PageMode] | None = None) -> list[dict[str, Any]]:
    """build_page_specs의 블록 명세만."""
    return build_page_specs(pages, states, tables, ocr, figures, modes)[0]


def build_page_specs(pages: Sequence[PageText], states: Sequence[TextLayerState],
                     tables: Sequence[Sequence["TableSpec"]] | None = None,
                     ocr: Sequence[Sequence[OcrParagraph]] | None = None,
                     figures: Sequence[Sequence[FigureBlock]] | None = None,
                     modes: Sequence[PageMode] | None = None,
                     ) -> tuple[list[dict[str, Any]], dict[int, Ledger]]:
    """(블록 명세, 쪽 번호 → 글자 장부). 장부는 명세를 만들 때 글자 id(page.chars 순번)로 센다: 표·그림·캡션은
    char_ids(그림과 짝 캡션은 따로), 줄·조각은 블록이 된 조각 글자의 Char.id(쪽마다 _numbered로 순번을 맞춘다). 숨은
    글자·공백은 세지 않는다. 블록 명세(계약 build_blocks 입력). 모든 쪽에서 보이는 글자로 블록을 만든다(숨은 글자는 fragments가 버린다).
    unreliable 쪽(깨진 글자층, layer 모드)도 같은 경로지만 그 쪽 조각은 본문 크기·머리말 반복·제목 단계에 쓰지 않고(섞인 문서의
    digital 쪽 블록이 바뀌지 않게. digital·scanned 글자가 없는 문서만 본문 크기를 그 쪽 글자로 정한다), 제목·머리말을
    만들지 않으며(section_path를 바꾸지 않는다), 그 쪽 블록 신뢰도는 UNRELIABLE_CONFIDENCE 이하다.
    tables는 쪽마다 표(tables.find_tables): 표 글자(char_ids)는 줄·조각에서
    빼고(본문 크기·머리말 판정에도 쓰지 않는다), 표마다 table 블록 하나를 표 윗변 위치에 끼운다(앞 문단과 잇지
    않는다). 표는 같은 읽기 방향(TableSpec.axes) 조각 사이에, 그 방향 조각이 없으면 쪽의 첫 방향 조각 사이에
    그 방향 읽기 좌표의 윗변으로 끼운다(조각이 없는 쪽은 표 자신의 방향). 윗변이 같으면 그 좌표의 왼쪽 표가
    먼저다. 그 방향 조각보다 아래인 표는 그 방향 조각 끝(다음 방향 조각 앞)에 둔다. 순서: 쪽 → 위→아래 → 왼→오.
    ocr는 쪽마다 OCR 문단(scan.ocr_pages): 문단 블록(text_source="ocr")으로 그 쪽 블록 사이에 윗변 기준으로 끼우고,
    section_path는 앞 블록을 따른다. 제목·목록·머리말 판정과 본문 크기에는 쓰지 않는다.
    figures는 쪽마다 그림 블록(FigureBlock): 그림·짝 캡션이 가져간 글자(char_ids)는 표처럼 줄·조각에서 빼고(본문 크기·
    머리말 판정에도 쓰지 않는다), 그림(위 캡션이 있으면 캡션)의 윗변 위치에 caption·figure 블록을 끼운다(OCR 문단과
    같은 규칙. 아래 캡션은 그림 바로 뒤). 그림 글자가 비어도 그림 블록은 남는다.
    구조 문단: 위 블록 어디에도 들지 않은 보이는 공백 아닌 글자(버그 울타리, 지금 코드에서는 생기지 않는다)는 줄 조각마다
    paragraph 블록(text_layer, 신뢰도 RESCUED_CONFIDENCE)으로 그 쪽 블록 끝에 보이는 쪽 윗변 순서로 낸다. section_path는
    쪽 끝의 제목 경로다(제자리가 아니다: 앞쪽에서 잃은 글자도 그 쪽 마지막 제목 아래에 들고, 앞 블록이 머리말·꼬리말이어도
    제목 경로를 쓴다). 제목 단계·본문 크기·머리말 판정에는 쓰지 않는다. 장부 in_blocks에 들고 rescued로 센다. 텍스트
    대조가 아니라 글자 id로 고른다. ocr 쪽은 하지 않는다(글자층 글자는 블록에 들지 않는 것이 정상: 파서가 replaced로 센다).
    modes는 쪽마다 처리 모드(triage.PageMode. 파서가 넘기고, None이면 쪽 상태에서 page_mode(OCR 없음)): 쪽 상태와
    따로다. layer·scan은 텍스트 레이어 조각으로 블록을 만든다(scan 쪽 OCR 문단은 ocr로 받는다). ocr 쪽(깨진 글자층 대신
    OCR로 읽는 unreliable)은 텍스트 레이어 조각을 만들지 않고(장부 in_blocks에 들지 않는다) 받은 OCR 문단·그림만 낸다.
    전제(파서가 보장한다): ocr 쪽은 tables가 비어 있고 그림에 텍스트 레이어 char_ids가 없다.
    위 unreliable 쪽 처리(문서 판정에서 빼기·신뢰도 상한)는 layer 모드 unreliable 쪽에만 쓴다."""
    pages = [_numbered(page) for page in pages]  # 글자 id = page.chars 순번(장부가 id로 센다)
    found = list(tables) if tables is not None else [[] for _ in pages]
    read = list(ocr) if ocr is not None else [[] for _ in pages]
    pictures = list(figures) if figures is not None else [[] for _ in pages]
    modes = list(modes) if modes is not None else [page_mode(s) for s in states]
    if len(modes) != len(pages):
        raise ValueError("modes must give one PageMode per page")
    frags: list[list[Fragment]] = []
    for page, mode, page_tables, page_figures in zip(pages, modes, found, pictures, strict=True):
        if mode == "ocr":  # 깨진 글자층 대신 OCR로 읽는 쪽: 텍스트 레이어 글자는 블록이 되지 않는다(파서가 replaced로 센다)
            frags.append([])
            continue
        taken = {i for t in page_tables for i in t.char_ids} | {i for f in page_figures for i in f.char_ids}
        rest = replace(page, chars=tuple(c for c in page.chars if c.id not in taken)) if taken else page
        frags.append(fragments(rest))
    # 깨진 글자층으로 블록을 만드는 쪽(layer 모드 unreliable): 문서 판정에서 빼고 신뢰도를 누른다(ocr 쪽 블록은 OCR이
    # 읽은 글자라 누르지 않는다)
    rough = {page.page for page, state, mode in zip(pages, states, modes, strict=True)
             if state == "unreliable" and mode == "layer"}
    clean = [[] if page.page in rough else page_frags for page, page_frags in zip(pages, frags, strict=True)]
    body = body_size(clean)
    if body is None:  # digital·scanned 글자가 없다: unreliable 쪽 글자로 줄을 잇는다(바뀔 digital 블록이 없다)
        body = body_size(frags)
    margins = repeated_margins(pages, clean)
    # (쪽, 머리말·꼬리말 종류, 조각 묶음 또는 표 또는 OCR 문단 또는 그림 블록, 또는 None = 쪽 끝(구조 문단 자리))
    items: list[tuple[PageText, str | None, list[Fragment] | TableSpec | OcrParagraph | FigureBlock | None]] = []
    for p, (page, page_frags, page_tables, paras, page_figures) in enumerate(
            zip(pages, frags, found, read, pictures, strict=True)):
        start = len(items)
        present = {f.axes for f in page_frags}
        fallback = page_frags[0].axes if page_frags else UPRIGHT
        placed: dict[Axes, list[tuple[tuple[float, float], TableSpec]]] = defaultdict(list)  # 방향 → ((윗변, 왼변), 표)
        for t in page_tables:
            axes = t.axes if t.axes in present or not page_frags else fallback
            placed[axes].append((_table_corner(t, page, axes), t))
        queues = {axes: deque(sorted(q, key=lambda item: item[0])) for axes, q in placed.items()}
        for i, f in enumerate(page_frags):
            if i and f.axes != page_frags[i - 1].axes:  # 앞 방향 조각이 끝났다: 그 방향에 남은 표를 먼저
                items += [(page, None, t) for _, t in queues.pop(page_frags[i - 1].axes, ())]
            queue = queues.get(f.axes)
            h = frame_size(page, f.axes)[1]
            # 표 윗변(bbox는 소수 셋째 자리)과 조각 윗변을 같은 자리수(보이는 쪽 0~1, 셋째 자리)로 견준다: 윗변이
            # 같으면 반올림 방향과 상관없이 표가 먼저다
            while queue and round(queue[0][0][0] / h, 3) <= round(f.y0 / h, 3):
                items.append((page, None, queue.popleft()[1]))
            margin = margins.get((p, i))
            last = items[-1] if items else None
            if (margin is None and last is not None and last[0] is page and last[1] is None
                    and isinstance(last[2], list) and body is not None and _continues(last[2], f, body)):
                last[2].append(f)
            else:
                items.append((page, margin, [f]))
        items += [(page, None, t) for queue in queues.values() for _, t in queue]
        extras = _merge_figures(paras, page_figures, page)
        if extras:
            items[start:] = _merge_ocr(items[start:], extras, page)
        if modes[p] != "ocr":
            items.append((page, None, None))

    def is_heading(page: PageText, group: Sequence[Fragment]) -> bool:
        return (body is not None and page.page not in rough and _is_heading_size(group[0], body)
                and len(group) <= HEADING_MAX_LINES)

    sizes = sorted({g[0].size for pg, m, g in items if isinstance(g, list) and m is None and is_heading(pg, g)},
                   reverse=True)
    specs: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []  # (단계, 제목 글자)
    owned: dict[int, set[int]] = defaultdict(set)  # 쪽 번호 → 블록이 된 글자 id(page.chars 순번)
    doubled: Counter[int] = Counter()
    rescued: Counter[int] = Counter()  # 쪽 번호 → 구조 문단으로 살린 글자 수

    def own(page: PageText, char_ids: Iterable[int]) -> None:
        for i in char_ids:
            if not 0 <= i < len(page.chars):  # 쪽에 없는 글자 순번: 예외 대신 장부 오류(coverage_mismatch)로
                doubled[page.page] += 1
                continue
            if page.chars[i].invisible or page.chars[i].text.isspace():
                continue
            doubled[page.page] += i in owned[page.page]
            owned[page.page].add(i)

    for page, margin, group in items:
        extra: dict[str, Any] = {}
        if group is None:  # 쪽 끝: 이 쪽 블록이 모두 글자를 가져간 뒤 남은 글자를 구조 문단으로
            for f in _unassigned(page, owned[page.page]):
                specs.append({"kind": "paragraph", "text": unicodedata.normalize("NFC", f.text),
                              "section_path": tuple(t for _, t in stack), "confidence": RESCUED_CONFIDENCE,
                              "state": "det", "text_source": "text_layer",
                              "locator": {"kind": "page", "page": page.page, "bbox": _box([f], page)}})
                own(page, (c.id for c in f.chars))
                rescued[page.page] += len(f.chars)
            continue
        if isinstance(group, FigureBlock):
            specs.extend(_figure_specs(group, page, tuple(t for _, t in stack), len(specs)))
            own(page, group.figure.char_ids)
            if group.figure.caption is not None:
                own(page, group.figure.caption.char_ids)
            continue
        if isinstance(group, OcrParagraph):
            text = unicodedata.normalize("NFC", group.text)
            if not text.strip():  # 텍스트 레이어 조각과 같이 빈 글자는 블록으로 만들지 않는다
                continue
            (x0, x1), (y0, y1) = _widen(group.bbox[0], group.bbox[2]), _widen(group.bbox[1], group.bbox[3])
            specs.append({"kind": "paragraph", "text": text,
                          "section_path": tuple(t for _, t in stack), "confidence": group.confidence,
                          "state": "det", "text_source": "ocr",
                          "locator": {"kind": "page", "page": page.page,
                                      "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}})
            continue
        if not isinstance(group, list):
            (x0, x1), (y0, y1) = _widen(group.bbox[0], group.bbox[2]), _widen(group.bbox[1], group.bbox[3])
            specs.append({"kind": "table", "table": group.table, "text": group.table.plain_text(),
                          "section_path": tuple(t for _, t in stack),
                          "confidence": CONFIDENCE["table"] if group.ruled else BORDERLESS_CONFIDENCE,
                          "state": "det", "text_source": "text_layer",
                          "locator": {"kind": "page", "page": page.page,
                                      "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}})
            own(page, group.char_ids)
            continue
        # 블록이 실제로 내는 조각. 머리말·꼬리말 묶음은 지금 늘 조각 하나다(위 잇기 조건: margin이 없는 조각만 잇고,
        # 머리말·꼬리말 묶음 뒤에는 잇지 않는다). shown은 낸 글자만 장부에 들게 하는 지킴이다
        shown = group[:1] if margin is not None else group
        if margin is not None:
            kind, text, path = margin, shown[0].text, ()
        elif is_heading(page, group):
            kind, text = "heading", unicodedata.normalize("NFC", " ".join(f.text for f in group))
            extra["level"] = min(sizes.index(group[0].size) + 1, MAX_LEVEL)
            while stack and stack[-1][0] >= extra["level"]:
                stack.pop()
            path = tuple(t for _, t in stack)
            stack.append((extra["level"], text))
        else:
            kind = "list_item" if LIST_MARKER.match(group[0].text) else "paragraph"
            text, path = "\n".join(f.text for f in group), tuple(t for _, t in stack)
        text = unicodedata.normalize("NFC", text)
        if not text.strip():
            continue
        specs.append({"kind": kind, "text": text, "section_path": path, "confidence": CONFIDENCE[kind],
                      "state": "det", "text_source": "text_layer",
                      "locator": {"kind": "page", "page": page.page, "bbox": _box(shown, page)}, **extra})
        # 낸 조각 글자만 장부에 든다: 나머지는 주인 없이 남아 구조 문단(TC-C)이 살린다
        own(page, (c.id for f in shown for c in f.chars))
    for spec in specs:
        if spec["locator"]["page"] in rough:
            spec["confidence"] = min(spec["confidence"], UNRELIABLE_CONFIDENCE)
    ledgers = {page.page: Ledger(len(owned[page.page]), doubled[page.page], rescued[page.page]) for page in pages}
    return specs, ledgers
