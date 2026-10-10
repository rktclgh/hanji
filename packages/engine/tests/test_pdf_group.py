import unicodedata
from dataclasses import replace

import pytest

from hanji.formats.pdf.extract import Char, PageText
from hanji.formats.pdf.figures import Caption, Figure
from hanji.formats.pdf.group import (
    LIST_MARKER, FigureBlock, Ledger, body_size, build_page_specs, build_specs, fragments, unit_box,
)
from hanji.formats.pdf.scan import OcrParagraph
from hanji.formats.pdf.tables import TableSpec
from hanji.formats.pdf.triage import page_stats
from hanji_contracts import Cell, Table, build_blocks

W, H = 595.0, 842.0


def line(text: str, x: float, baseline: float, size: float = 11.0, bold: bool = False, gap: float = 0.0,
         invisible: bool = False) -> list[Char]:
    """x·baseline은 pt(원점 왼쪽 위). 한글·기호 너비 = size, ASCII = size/2, 글자 사이 gap(pt)."""
    out = []
    for ch in text:
        width = size / 2 if ord(ch) < 0x80 else size
        out.append(Char(text=ch, x0=x / W, y0=(baseline - 0.752 * size) / H, x1=(x + width) / W,
                        y1=(baseline + 0.142 * size) / H, baseline=baseline / H, size=size, bold=bold,
                        invisible=invisible))
        x += width + gap
    return out


def page(*lines: list[Char], number: int = 1) -> PageText:
    return PageText(page=number, width_pt=W, height_pt=H, rotation=0,
                    chars=tuple(c for chars in lines for c in chars), image_coverage=())


def specs(*pages: PageText, states=None):
    return build_specs(pages, states or ["digital"] * len(pages))


def kinds_texts(result) -> list[tuple[str, str]]:
    return [(s["kind"], s["text"]) for s in result]


def test_fragments_split_on_wide_gap_and_order_left_to_right():
    p = page(line("금액", 300, 100), line("구분", 72, 100), line("다음 줄", 72, 116))
    assert [f.text for f in fragments(p)] == ["구분", "금액", "다음 줄"]


def test_same_line_tolerates_half_size_baseline_shift():
    p = page(line("가나", 72, 100), line("다", 94, 105.4), line("라", 72, 106))
    assert [f.text for f in fragments(p)] == ["가나다", "라"]


def test_space_inserted_where_gap_exceeds_tracking():
    """자간을 -3pt로 좁히고 공백 글자 없이 낱말 사이만 1.4pt 벌린 줄(한글 프로그램 PDF 실측 모양)."""
    chars = line("상장사", 72, 100, size=12, gap=-3) + line("임직원", 72 + 2 * 9 + 12 + 1.4, 100, size=12, gap=-3)
    assert [f.text for f in fragments(page(chars))] == ["상장사 임직원"]
    assert [f.text for f in fragments(page(line("가 나", 72, 100)))] == ["가 나"]  # 공백 글자는 그대로


def test_space_rule_threshold_and_symbol_gaps():
    """공백 = 간격 − 보통 자간 > 크기 × 0.2. 보통 자간은 글자·숫자끼리 간격의 중앙값(음수일 때만)."""
    near = line("가나", 72, 100, size=10) + line("다라", 72 + 20 + 1.9, 100, size=10)  # 0.19 × 10pt
    far = line("가나", 72, 100, size=10) + line("다라", 72 + 20 + 2.1, 100, size=10)  # 0.21 × 10pt
    assert [f.text for f in fragments(page(near))] == ["가나다라"]
    assert [f.text for f in fragments(page(far))] == ["가나 다라"]
    # 목차: 글자는 붙어 있고 점선 기호끼리는 겹친다(음수 간격). 기호 간격이 자간 추정을 끌어내리면 안 된다
    toc = line("국채시장", 72, 100, size=10) + line("······", 112, 100, size=10, gap=-6)
    assert [f.text for f in fragments(page(toc))] == ["국채시장······"]
    # 넓은 양수 자간(글자 사이를 띄운 제목)은 보통 자간으로 보지 않는다: 띄운 그대로 공백
    assert [f.text for f in fragments(page(line("보도자료", 72, 100, size=10, gap=4)))] == ["보 도 자 료"]


def test_invisible_and_whitespace_only_chars_make_no_fragment():
    p = page(line("숨은 글자", 72, 100, invisible=True), line("   ", 72, 120))
    assert fragments(p) == [] and specs(p) == []


def test_body_size_is_most_common_char_size_ties_to_smaller():
    p = page(line("가나다라", 72, 100, size=11), line("마바사", 72, 130, size=16), line("아자차카", 72, 160, size=13))
    assert body_size([fragments(p)]) == 11.0
    assert body_size([fragments(page(line("가.", 72, 100, size=11.2)))]) == 11.0  # 0.5pt 단위
    assert body_size([[]]) is None


def test_headings_levels_and_section_path():
    p = page(line("사업 계획", 72, 60, 20), line("1. 추진 배경", 72, 100, 16), line("본문 첫째", 72, 130),
             line("가. 세부 목표", 72, 170, 13), line("본문 둘째", 72, 200), line("2. 예산", 72, 240, 16),
             line("본문 셋째", 72, 270))
    result = specs(p)
    assert [(s["kind"], s.get("level"), s["section_path"]) for s in result] == [
        ("heading", 1, ()), ("heading", 2, ("사업 계획",)), ("paragraph", None, ("사업 계획", "1. 추진 배경")),
        ("heading", 3, ("사업 계획", "1. 추진 배경")),
        ("paragraph", None, ("사업 계획", "1. 추진 배경", "가. 세부 목표")),
        ("heading", 2, ("사업 계획",)), ("paragraph", None, ("사업 계획", "2. 예산"))]
    assert {s["confidence"] for s in result if s["kind"] == "heading"} == {0.6}


def test_heading_ratio_and_bold_ratio():
    plain = page(line("본문 글자 크기", 72, 100), line("작은 제목", 72, 130, 12.5), line("본문 글자 크기", 72, 160))
    assert kinds_texts(specs(plain))[1] == ("paragraph", "작은 제목")  # 12.5 < 11 × 1.15
    bold = page(line("본문 글자 크기", 72, 100), line("굵은 제목", 72, 130, 12, bold=True), line("본문 글자 크기", 72, 160))
    assert kinds_texts(specs(bold))[1] == ("heading", "굵은 제목")  # 12 ≥ 11 × 1.05


def test_two_heading_lines_join_three_become_paragraph():
    two = page(line("본문 글자가 가장 많다", 72, 60), line("길게 이어지는", 72, 100, 20), line("제목", 72, 124, 20),
               line("본문 글자가 가장 많다", 72, 160))
    assert kinds_texts(specs(two))[1] == ("heading", "길게 이어지는 제목")
    three = page(line("본문 글자가 가장 많은 줄이다", 72, 60), line("큰 글자", 72, 100, 16), line("세 줄이면", 72, 120, 16),
                 line("문단", 72, 140, 16))
    assert kinds_texts(specs(three))[1] == ("paragraph", "큰 글자\n세 줄이면\n문단")


def test_paragraph_lines_join_with_newline_and_split_on_each_rule():
    joined = page(line("첫째 줄", 72, 100), line("둘째 줄", 72, 116))
    assert kinds_texts(specs(joined)) == [("paragraph", "첫째 줄\n둘째 줄")]
    far = page(line("첫째 줄", 72, 100), line("멀리 떨어진 줄", 72, 130))  # 빈 간격 > 줄 높이 × 0.8
    sized = page(line("첫째 줄", 72, 100), line("조금 큰 줄", 72, 116, 12))  # 크기 차 > 0.5pt
    shifted = page(line("첫째 줄", 72, 100), line("들여 쓴 줄", 84, 116))  # 왼쪽 시작 차 > 본문 크기
    for p in (far, sized, shifted):
        assert len(specs(p)) == 2


@pytest.mark.parametrize("text", ["1. 항목", "12) 항목", "(3) 항목", "가. 항목", "하) 항목", "① 항목", "⑳ 항목",
                                  "□ 항목", "■ 항목", "○ 항목", "● 항목", "◦ 항목", "◆ 항목", "◇ 항목", "▶ 항목",
                                  "▷ 항목", "- 항목", "– 항목", "• 항목", "※ 항목", "  □ 항목",
                                  "ㅇ 항목", "ㆍ 항목", "· 항목", "∙ 항목", "‣ 항목", "▸ 항목", "▪ 항목", "⇨ 항목", "→ 항목"])
def test_list_marker_matches(text):
    assert LIST_MARKER.match(text)


@pytest.mark.parametrize("text", ["1.5배 증가", "□항목", "ㅇ항목", "가나다", "(가) 항목", "2026년 계획", "* 주석", "→결과"])
def test_list_marker_rejects(text):
    assert not LIST_MARKER.match(text)


def test_list_items_split_and_keep_marker_with_hanging_indent_continuation():
    p = page(line("□ 첫째 항목은", 72, 100), line("이어지는 줄이다", 88.5, 116), line("○ 둘째 항목", 72, 132),
             line("- 셋째 항목", 72, 148))
    assert kinds_texts(specs(p)) == [("list_item", "□ 첫째 항목은\n이어지는 줄이다"), ("list_item", "○ 둘째 항목"),
                                     ("list_item", "- 셋째 항목")]
    assert {s["confidence"] for s in specs(p)} == {0.7}


def test_hanging_indent_after_any_leading_marker():
    """첫 줄 앞머리(목록 표지 또는 글자·숫자가 아닌 한 글자) 다음 글자에 맞춘 줄은 이어진다."""
    for marker in ("ㅇ", "✅", "*"):
        p = page(line(f"{marker} 첫째 줄은", 72, 100), line("이어지는 줄", 88.5, 116))
        assert kinds_texts(specs(p))[0][1] == f"{marker} 첫째 줄은\n이어지는 줄"
    p = page(line("가나 첫째 줄", 72, 100), line("둘째 줄", 88.5, 116))  # 두 글자 낱말은 앞머리가 아니다
    assert len(specs(p)) == 2


def footer_pages(n: int, header_y: float = 30.0) -> list[PageText]:
    return [page(line("2026년 사업 계획 보고", 72, header_y, 9), line(f"{i}쪽 본문은 머리말보다 글자가 많다", 72, 300),
                 line(f"- {i} -", 282, 812, 9), number=i) for i in range(1, n + 1)]


def test_header_footer_repeat_on_half_of_pages():
    result = specs(*footer_pages(3))
    assert [s["kind"] for s in result] == ["page_header", "paragraph", "page_footer"] * 3
    assert [s["text"] for s in result if s["kind"] == "page_footer"] == ["- 1 -", "- 2 -", "- 3 -"]
    assert {(s["confidence"], s["section_path"]) for s in result if s["kind"].startswith("page_")} == {(0.8, ())}


def test_header_footer_needs_three_pages():
    assert "page_footer" not in [s["kind"] for s in specs(*footer_pages(2))]


def test_header_footer_needs_same_position_on_half_the_pages():
    pages = footer_pages(4)
    moved = [page(line("2026년 사업 계획 보고", 72, 12 + 18 * i, 9), line("본문은 머리말보다 글자가 많다", 72, 300),
                  number=i + 1)
             for i in range(4)]  # 위 여백 안이지만 쪽마다 2% 넘게 움직인다
    assert "page_header" not in [s["kind"] for s in specs(*moved)]
    states = ["digital", "unreliable", "unreliable", "digital"]  # 반복 2/4쪽 = 절반
    assert [s["kind"] for s in specs(*pages, states=states)].count("page_footer") == 2


def test_scanned_pages_keep_visible_text_unreliable_pages_keep_their_text_layer():
    """scanned 쪽도 보이는 글자는 블록이 된다(숨은 글자만 버린다). unreliable 쪽은 깨진 글자층으로 블록을 만들고 그 쪽
    블록 신뢰도는 0.2 이하다(숨은 글자는 그 쪽에서도 버린다)."""
    p1 = page(line("보이는 쪽", 72, 100), line("숨은 글자", 72, 130, invisible=True))
    p2 = page(line("스캔 쪽 숨은 글자", 72, 100, invisible=True), line("스캔 쪽 쪽 번호", 72, 130), number=2)
    result = specs(p1, p2, states=["digital", "scanned"])
    assert [(s["text"], s["locator"]["page"]) for s in result] == [("보이는 쪽", 1), ("스캔 쪽 쪽 번호", 2)]
    kept = specs(p1, p2, states=["unreliable", "scanned"])
    assert [(s["text"], s["locator"]["page"], s["confidence"]) for s in kept] == [
        ("보이는 쪽", 1, 0.2), ("스캔 쪽 쪽 번호", 2, 0.7)]
    assert [(s["text"], s["confidence"]) for s in specs(p1, p2, states=["unreliable", "unreliable"])] == [
        ("보이는 쪽", 0.2), ("스캔 쪽 쪽 번호", 0.2)]


def linked(result: list[dict]) -> list[dict]:
    """그림 명세의 figure.caption_ref(명세 목록의 절대 순번)를 가리키는 캡션 글자로 바꾼다. 앞에 블록이 끼면 순번은
    계약대로 밀리지만 같은 캡션을 가리켜야 한다."""
    return [{**s, "figure": {**s["figure"], "caption_ref": result[s["figure"]["caption_ref"]]["text"]}}
            if "caption_ref" in s.get("figure", {}) else s for s in result]


def test_unreliable_page_does_not_change_digital_blocks():
    """unreliable 쪽 조각은 본문 크기·머리말 반복·제목 단계에 쓰지 않는다: 섞인 문서의 digital 쪽 블록은 그 쪽이 빈
    쪽일 때와 같다. 다만 그림의 caption_ref는 명세 목록의 절대 순번이라 앞 쪽 블록 수만큼 밀린다(가리키는 캡션은
    같다). unreliable 쪽의 큰 글자는 제목이 아니라 문단이고(section_path를 바꾸지 않는다), 머리말 자리 줄도 문단이다."""
    head, text, under = line("머리말 줄", 72, 30, 9), line("셋째 쪽 본문이다.", 72, 130), line("그림 1. 분기별 실적", 200, 360)
    at = len(head) + len(text)
    caption = Caption(box=(200.0, 350.0, 420.0, 363.0), text="그림 1. 분기별 실적",
                      char_ids=frozenset(range(at, at + len(under))), line_ids=frozenset(), above=False)
    pictures = [[], [], [FigureBlock(Figure(box=(90.0, 200.0, 510.0, 330.0), category="chart", caption=caption),
                                     "text_layer", IMAGE)]]

    def doc(middle: PageText) -> list[PageText]:
        return [page(line("머리말 줄", 72, 30, 9), line("1. 첫째 제목", 72, 100, 16), line("본문 문단이다.", 72, 130)),
                middle, page(head, text, under, number=3)]

    noisy = page(line("머리말 줄", 72, 30, 9), line("깨진 큰 글자", 72, 100, 24),
                 *[line("작은 글자가 아주 많이 적힌 깨진 줄이다", 72, 200 + 12 * i, 8) for i in range(30)], number=2)
    before = build_specs(doc(page(number=2)), ["digital"] * 3, None, None, pictures)
    after = build_specs(doc(noisy), ["digital", "unreliable", "digital"], None, None, pictures)
    assert [s["kind"] for s in before] == ["page_header", "heading", "paragraph", "page_header", "paragraph", "figure",
                                           "caption"]
    middle = [s for s in after if s["locator"]["page"] == 2]
    assert (before[5]["figure"]["caption_ref"], after[5 + len(middle)]["figure"]["caption_ref"]) == (6, 6 + len(middle))
    assert [s for s in linked(after) if s["locator"]["page"] != 2] == linked(before)
    assert [(s["kind"], s["text"].split("\n")[0], s["confidence"]) for s in middle] == [
        ("paragraph", "머리말 줄", 0.2), ("paragraph", "깨진 큰 글자", 0.2),
        ("paragraph", "작은 글자가 아주 많이 적힌 깨진 줄이다", 0.2)]
    assert {s["section_path"] for s in middle} == {("1. 첫째 제목",)}


def test_document_of_only_unreliable_pages_sizes_its_body_from_them():
    """digital·scanned 글자가 없으면 본문 크기는 unreliable 쪽 글자로 정한다(줄을 문단으로 잇는다). 제목은 없다."""
    p = page(line("깨진 큰 글자", 72, 80, 20), line("첫 줄이다", 72, 120), line("둘째 줄이다", 72, 134))
    assert [(s["kind"], s["text"], s["confidence"]) for s in specs(p, states=["unreliable"])] == [
        ("paragraph", "깨진 큰 글자", 0.2), ("paragraph", "첫 줄이다\n둘째 줄이다", 0.2)]


def test_blank_ocr_paragraph_makes_no_block():
    """글자가 공백뿐인 OCR 문단은 텍스트 레이어 조각처럼 블록이 되지 않는다(읽개가 빈 줄을 버리지만 한 겹 더)."""
    p = page(line("스캔 쪽 쪽 번호", 72, 130))
    paras = [OcrParagraph(text=" \n ", bbox=(0.1, 0.05, 0.5, 0.08), confidence=0.45),
             OcrParagraph(text="그림 속 글자", bbox=(0.1, 0.2, 0.5, 0.25), confidence=0.45)]
    result = build_specs([p], ["scanned"], None, [paras])
    assert [(s["text_source"], s["text"]) for s in result] == [("text_layer", "스캔 쪽 쪽 번호"), ("ocr", "그림 속 글자")]


def test_text_is_nfc_and_block_fields():
    nfd = unicodedata.normalize("NFD", "한글 문단")
    result = specs(page(line(nfd, 72, 100)))
    assert result[0]["text"] == "한글 문단" and unicodedata.is_normalized("NFC", result[0]["text"])
    assert {k: result[0][k] for k in ("state", "text_source", "confidence")} == {
        "state": "det", "text_source": "text_layer", "confidence": 0.7}


def test_bbox_rounded_to_three_places_and_never_degenerate():
    tiny = Char(text=".", x0=0.50001, y0=0.40001, x1=0.50004, y1=0.40004, baseline=0.40004, size=11)
    result = specs(PageText(page=1, width_pt=W, height_pt=H, rotation=0, chars=(tiny,), image_coverage=()))
    assert result[0]["locator"]["bbox"] == {"x0": 0.5, "y0": 0.4, "x1": 0.501, "y1": 0.401}
    edge = Char(text="끝", x0=0.99996, y0=0.99996, x1=1.0, y1=1.0, baseline=1.0, size=11)
    box = specs(PageText(page=1, width_pt=W, height_pt=H, rotation=0, chars=(edge,), image_coverage=()))[0]
    assert box["locator"]["bbox"] == {"x0": 0.999, "y0": 0.999, "x1": 1.0, "y1": 1.0}
    p = page(line("가나다", 72, 100))
    assert specs(p)[0]["locator"]["bbox"] == {"x0": 0.121, "y0": 0.109, "x1": 0.176, "y1": 0.121}


def test_specs_are_valid_contract_blocks_in_reading_order():
    p1 = page(line("둘째", 72, 200), line("첫째", 72, 100), line("오른쪽", 300, 100))
    p2 = page(line("다음 쪽", 72, 100), number=2)
    blocks = build_blocks("doc", specs(p1, p2))
    assert [b.text for b in blocks] == ["첫째", "오른쪽", "둘째", "다음 쪽"]


@pytest.mark.parametrize("opener", ["(", "〈", "《", "「", "『", "[", "［", "（", '"', "“", "'", "‘"])
def test_opening_bracket_is_not_a_leading_marker(opener):
    """여는 괄호·따옴표는 앞머리가 아니다: 둘째 글자에 맞춘 다음 줄도 내어쓰기로 잇지 않는다."""
    p = page(line(opener, 72, 100) + line("단위: 백만원 )", 88.5, 100), line("이어지는 줄", 88.5, 116))
    assert kinds_texts(specs(p)) == [("paragraph", f"{opener} 단위: 백만원 )"), ("paragraph", "이어지는 줄")]


@pytest.mark.parametrize("text", ["나. 항목", "하. 항목", "마) 항목", "3) 항목", "(3) 항목", "1. 2026년 계획",
                                  "2. 1.5배 증가", "1. 3.5% 증가", "2) 1.5배"])
def test_list_marker_ordinals_still_match(text):
    assert LIST_MARKER.match(text)


@pytest.mark.parametrize("text", ["요. 그러나", "것) 그러나", "각. 항목", "2026. 10. 3. 발표", "10. 3. 발표", "2026. 10.",
                                  "2026. 10.03. 발표", "2026. 10.3. 발표", "2026. 10.3."])
def test_list_marker_rejects_other_syllables_and_dates(text):
    """가~하 열네 글자만 차례 표지다(유니코드 범위 가-하가 아니다). 날짜 앞 숫자도 표지가 아니다."""
    assert not LIST_MARKER.match(text)


def test_wrapped_sentence_does_not_become_list_items():
    for first, second in (("이 사업은 2026년부터 시행했어", "요. 그러나 예산은 부족하"),
                          ("예산이 부족하다는", "것) 그러나 조정한다.")):
        p = page(line(first, 72, 100), line(second, 72, 116))
        assert kinds_texts(specs(p)) == [("paragraph", f"{first}\n{second}")]
    for when in ("2026. 10. 3. 발표", "2026. 10.03. 발표", "2026. 10.3. 발표"):
        date = page(line("발표 일자는 다음과 같다", 72, 100), line(when, 72, 116))
        assert kinds_texts(specs(date)) == [("paragraph", f"발표 일자는 다음과 같다\n{when}")]


def test_wrapped_line_starting_with_da_ordinal_is_still_a_list_item():
    """남은 한계: '다.'는 차례 표지(가. 나. 다.)와 구별할 수 없어 줄 첫머리에 오면 목록 항목이 된다."""
    p = page(line("이 사업은 2026년부터 시행된", 72, 100), line("다. 그러나 예산은 부족하", 72, 116),
             line("다. 이에 따라 조정한다.", 72, 132))
    assert [s["kind"] for s in specs(p)] == ["paragraph", "list_item", "list_item"]


def test_short_tight_fragment_loses_word_space_known_limit():
    """알려진 한계: 자간 -3pt·낱말 사이 1.4pt(12pt)의 짧은 조각은 중앙값이 낱말 간격이라 공백을 잃는다.
    작은 쪽 중앙값은 이를 고치지만 실제 문서에서 남는 공백을 늘려서 받아들인다."""
    chars = line("총", 72, 100, size=12, gap=-3) + line("매출", 72 + 12 + 1.4, 100, size=12, gap=-3)
    assert [f.text for f in fragments(page(chars))] == ["총매출"]


def test_number_cells_in_margin_are_not_footer_but_lone_page_number_is():
    cells = [page(line(f"{i}쪽 본문은 머리말보다 글자가 많다", 72, 300), line("1,234", 72, 812, 9),
                  line("5,678", 300, 812, 9), number=i) for i in range(1, 4)]
    assert "page_footer" not in [s["kind"] for s in specs(*cells)]
    numbers = [page(line(f"{i}쪽 본문은 머리말보다 글자가 많다", 72, 300), line(f"{i}", 290, 812, 9), number=i)
               for i in range(1, 4)]
    assert [s["text"] for s in specs(*numbers) if s["kind"] == "page_footer"] == ["1", "2", "3"]


def test_letterless_margin_line_counts_only_when_alone_in_its_zone():
    """글자 없이 숫자·기호뿐인 줄은 그 쪽 그 영역(위·아래)에 그런 줄이 하나뿐일 때만 머리말·꼬리말 후보다."""
    def body(i):
        return line(f"{i}쪽 본문은 머리말보다 글자가 많다", 72, 300)

    mixed = [page(body(i), line("1,234", 72, 812, 9), line("12.5", 300, 812, 9), number=i) for i in range(1, 4)]
    assert "page_footer" not in [s["kind"] for s in specs(*mixed)]
    # 위 영역의 숫자 칸은 아래 영역의 쪽 번호를 막지 않는다
    split = [page(line("1,234", 72, 30, 9), line("12.5", 300, 30, 9), body(i), line(f"- {i} -", 282, 812, 9),
                  number=i) for i in range(1, 4)]
    result = specs(*split)
    assert [s["text"] for s in result if s["kind"] == "page_footer"] == ["- 1 -", "- 2 -", "- 3 -"]
    assert "page_header" not in [s["kind"] for s in result]


def test_reading_frame_orders_and_spaces_upside_down_chars():
    """줄 안 순서·공백은 글자의 진행 방향을 따른다: 180° 뒤집힌 글자(axes (2, 3))는 오른쪽에서 왼쪽으로 읽는다."""
    out = []
    x = 400.0
    for ch in "가나다라":
        if ch == "다":
            x -= 4  # 낱말 사이 4pt
        out.append(Char(text=ch, x0=(x - 11) / W, y0=(100 - 1.6) / H, x1=x / W, y1=(100 + 8.3) / H,
                        baseline=1 - 100 / H, size=11, axes=(2, 3)))
        x -= 11
    assert [f.text for f in fragments(page(out))] == ["가나 다라"]


IMAGE = {"asset": "sha256:" + "a" * 64, "mime": "image/png", "width_px": 10, "height_px": 5, "dpi": 200,
         "category": "chart"}


def test_figure_and_caption_below_take_their_place_and_link():
    """그림 블록은 윗변 자리에 끼고 아래 캡션은 그림 바로 뒤. 그림·캡션이 가져간 글자는 문단에서 빠지고,
    figure.caption_ref는 캡션 명세의 순번이다(엔진이 caption_block_id로 바꾼다)."""
    top, inside = line("위 문단", 72, 100), line("1분기", 120, 300)
    under, bottom = line("그림 1. 분기별 실적", 200, 360), line("아래 문단", 72, 500)
    p = page(top, inside, under, bottom)
    a, b = len(top), len(top) + len(inside)
    fig_ids, cap_ids = frozenset(range(a, b)), frozenset(range(b, b + len(under)))
    caption = Caption(box=(200.0, 350.0, 420.0, 363.0), text="그림 1. 분기별 실적", char_ids=cap_ids,
                      line_ids=frozenset(), above=False)
    fig = Figure(box=(90.0, 140.0, 510.0, 330.0), category="chart", text="1분기", char_ids=fig_ids, caption=caption)
    result = build_specs([p], ["digital"], None, None, [[FigureBlock(fig, "text_layer", IMAGE)]])
    assert kinds_texts(result) == [("paragraph", "위 문단"), ("figure", "1분기"), ("caption", "그림 1. 분기별 실적"),
                                   ("paragraph", "아래 문단")]
    assert result[1]["figure"] == {**IMAGE, "caption_ref": 2} and "figure" not in result[2]
    assert {k: result[1][k] for k in ("state", "text_source", "confidence")} == {
        "state": "det", "text_source": "text_layer", "confidence": 0.7}
    assert result[1]["locator"]["bbox"] == unit_box(90 / W, 140 / H, 510 / W, 330 / H)


def test_caption_above_goes_first_and_a_figure_without_image_has_no_figure_field():
    """위 캡션은 그림 앞. 이미지를 담지 못한 그림(빈 자르기·바이트 상한)은 figure 필드 없이 블록만 남고, 짝 캡션은
    가리킬 곳(caption_block_id는 figure 안)이 없어 캡션 블록으로 따로 남는다(리뷰 I7)."""
    above = line("그림 2. 위 캡션", 72, 120)
    caption = Caption(box=(72.0, 110.0, 200.0, 123.0), text="그림 2. 위 캡션", char_ids=frozenset(range(len(above))),
                      line_ids=frozenset(), above=True)
    fig = Figure(box=(72.0, 130.0, 400.0, 300.0), category="image", caption=caption)
    result = build_specs([page(above)], ["digital"], None, None, [[FigureBlock(fig, "text_layer", None)]])
    assert kinds_texts(result) == [("caption", "그림 2. 위 캡션"), ("figure", "")]
    assert "figure" not in result[1]


def test_ocr_paragraphs_and_figures_merge_by_top():
    """scanned 쪽: OCR 문단(XY 분할 순서)과 그림 블록을 윗변으로 합친다. 윗변이 같으면 문단이 먼저다."""
    p = page(line("스캔 쪽 쪽 번호", 72, 800))
    paras = [OcrParagraph(text="첫 문단", bbox=(0.1, 0.05, 0.5, 0.08), confidence=0.45),
             OcrParagraph(text="같은 높이 문단", bbox=(0.6, 0.25, 0.9, 0.28), confidence=0.45),
             OcrParagraph(text="끝 문단", bbox=(0.1, 0.75, 0.5, 0.78), confidence=0.45)]
    fig = Figure(box=(0.1 * W, 0.25 * H, 0.5 * W, 0.5 * H), category="image", text="그림 속 글자")
    result = build_specs([p], ["scanned"], None, [paras], [[FigureBlock(fig, "ocr", None)]])
    assert [(s["kind"], s["text_source"], s["text"]) for s in result] == [
        ("paragraph", "ocr", "첫 문단"), ("paragraph", "ocr", "같은 높이 문단"), ("figure", "ocr", "그림 속 글자"),
        ("paragraph", "ocr", "끝 문단"), ("paragraph", "text_layer", "스캔 쪽 쪽 번호")]


def test_unreliable_page_keeps_its_figure_block_with_low_confidence():
    fig = Figure(box=(72.0, 200.0, 300.0, 400.0), category="image")
    result = build_specs([page(line("가", 72, 100))], ["unreliable"], None, None, [[FigureBlock(fig, "text_layer", None)]])
    assert [(s["kind"], s["text"], s["confidence"]) for s in result] == [("paragraph", "가", 0.2), ("figure", "", 0.2)]


def test_figure_and_text_with_the_same_rounded_top_put_the_text_first():
    """리뷰 M4: 블록 상자는 소수 셋째 자리로 반올림한다. 그림 윗변이 반올림해 텍스트 레이어 블록 윗변과 같으면(같은
    높이) 문서화한 규칙대로 텍스트가 먼저다(반올림 전 값으로 견주면 그림이 먼저 끼어든다)."""
    text = line("오른쪽 단 문단", 320, 300)  # 윗변 (300 - 0.752 × 11) / 842 = 0.34647 → 0.346
    fig = Figure(box=(72.0, 0.3456 * H, 300.0, 500.0), category="image")  # 윗변 0.3456 → 0.346
    result = build_specs([page(text)], ["digital"], None, None, [[FigureBlock(fig, "text_layer", None)]])
    assert result[0]["locator"]["bbox"]["y0"] == result[1]["locator"]["bbox"]["y0"] == 0.346
    assert kinds_texts(result) == [("paragraph", "오른쪽 단 문단"), ("figure", "")]


def test_ocr_paragraph_and_figure_with_the_same_rounded_top_put_the_paragraph_first():
    """사전 리뷰 5: scanned 쪽도 같은 규칙. OCR 문단과 그림의 윗변이 반올림해 같으면(블록 상자 둘 다 0.250) 문단이
    먼저다(반올림 전 값으로 견주면 그림이 먼저 끼어든다)."""
    paras = [OcrParagraph(text="같은 높이 문단", bbox=(0.6, 0.2504, 0.9, 0.28), confidence=0.45)]
    fig = Figure(box=(0.1 * W, 0.2496 * H, 0.5 * W, 0.5 * H), category="image")
    result = build_specs([page()], ["scanned"], None, [paras], [[FigureBlock(fig, "ocr", None)]])
    assert result[0]["locator"]["bbox"]["y0"] == result[1]["locator"]["bbox"]["y0"] == 0.25
    assert [(s["kind"], s["text"]) for s in result] == [("paragraph", "같은 높이 문단"), ("figure", "")]


def test_build_specs_takes_page_modes_apart_from_states():
    """처리 모드는 쪽 상태와 따로 받는다(파서가 넘긴다. 없으면 쪽 상태에서 page_mode). 쪽마다 하나여야 한다."""
    p = page(line("보이는 쪽", 72, 100))
    assert build_specs([p], ["digital"], modes=["layer"]) == specs(p)
    assert build_specs([p], ["scanned"], modes=["scan"]) == specs(p, states=["scanned"])
    with pytest.raises(ValueError, match="one PageMode per page"):
        build_specs([p], ["digital"], modes=[])


def ledger_page(table_ids=range(7, 11), figure_ids=(15, 16), caption_ids=range(11, 15)):
    """글자 장부용 쪽: 문단(0~6), 표 칸 글자(7~10), 캡션(11~14), 그림 속 글자(15~16), 숨은 글자(17~18). 공백이 셋."""
    p = page(line("본문 문단이다", 72, 100), line("칸 글자", 80, 320), line("그림 1", 72, 500), line("눈금", 100, 600),
             line("숨은", 72, 760, invisible=True))
    cells = [Cell(row=r, col=k, text="칸 글자" if (r, k) == (0, 0) else "", text_source="text_layer")
             for r in range(2) for k in range(2)]
    table = TableSpec(bbox=(0.1, 0.35, 0.5, 0.42), table=Table(n_rows=2, n_cols=2, cells=cells),
                      char_ids=frozenset(table_ids) | {17})  # 숨은 글자 순번이 섞여도 세지 않는다
    caption = Caption(box=(72.0, 490.0, 200.0, 503.0), text="그림 1", char_ids=frozenset(caption_ids),
                      line_ids=frozenset(), above=True)
    fig = Figure(box=(72.0, 520.0, 400.0, 700.0), category="chart", text="눈금", char_ids=frozenset(figure_ids),
                 caption=caption)
    return p, [[table]], [[FigureBlock(fig, "text_layer", None)]]


def test_ledger_counts_each_visible_char_once_whichever_block_has_it():
    """문단 6 + 표 3 + 캡션 3 + 그림 2 = 14 = 보이는 공백 아닌 글자 수. 공백·숨은 글자는 세지 않는다."""
    p, tables, figures = ledger_page()
    result, ledgers = build_page_specs([p], ["digital"], tables, None, figures)
    assert [s["kind"] for s in result] == ["paragraph", "table", "caption", "figure"]
    assert ledgers == {1: Ledger(in_blocks=14, doubled=0)} and page_stats(p).chars == 14
    assert build_specs([p], ["digital"], tables, None, figures) == result


def test_ledger_counts_margin_lines_and_every_page():
    pages = footer_pages(3)
    _, ledgers = build_page_specs(pages, ["digital"] * 3)
    assert ledgers == {p.page: Ledger(in_blocks=page_stats(p).chars) for p in pages}


@pytest.mark.parametrize("ids", [{"table_ids": range(7, 12)}, {"figure_ids": (14, 15, 16)}])
def test_ledger_flags_a_char_given_to_two_blocks(ids):
    """표와 캡션, 그림과 짝 캡션이 같은 글자를 함께 가져가면(FigureBlock.char_ids 합집합은 겹침을 숨긴다) doubled."""
    p, tables, figures = ledger_page(**ids)
    _, ledgers = build_page_specs([p], ["digital"], tables, None, figures)
    assert ledgers == {1: Ledger(in_blocks=14, doubled=1)}


@pytest.mark.parametrize("bad", [19, 99, -1])
def test_ledger_flags_a_char_id_outside_the_page_without_raising(bad):
    """쪽 글자 수(19) 밖 순번(음수 포함)은 장부 오류: 예외 없이 doubled로 센다(파서가 coverage_mismatch로 남긴다)."""
    p, tables, figures = ledger_page(table_ids=(*range(7, 11), bad))
    assert len(p.chars) == 19
    _, ledgers = build_page_specs([p], ["digital"], tables, None, figures)
    assert ledgers == {1: Ledger(in_blocks=14, doubled=1)}


def test_ledger_counts_broken_text_on_an_unreliable_page():
    """깨진 글자층(U+FFFD·사용자 정의 영역 U+E000·폭 없는 공백 U+200B)도 보이는 공백 아닌 글자마다 한 번씩 센다."""
    p = page(line("가�나다", 72, 100), line("라​마 �", 72, 116))
    assert page_stats(p).chars == 10
    _, ledgers = build_page_specs([p], ["unreliable"])
    assert ledgers == {1: Ledger(in_blocks=page_stats(p).chars, doubled=0)}


def test_ocr_mode_page_makes_no_text_layer_blocks_and_keeps_ocr_confidence():
    """ocr 모드 쪽(글자층 대신 OCR로 읽는 unreliable)은 텍스트 레이어 글자로 블록을 만들지 않는다(장부 in_blocks 0: 파서가
    replaced로 센다). OCR 문단·그림은 깨진 글자층에서 나온 글자가 아니라 신뢰도 상한 0.2를 받지 않는다. 같은 문서의
    digital 쪽 블록은 그 쪽이 빈 쪽일 때와 같다(그 쪽 큰 글자·작은 글자가 본문 크기·제목 단계에 들지 않는다)."""
    first = page(line("1. 첫째 제목", 72, 100, 16), line("본문 문단이다.", 72, 130))
    broken = page(line("깨진 큰 글자", 72, 100, 24),
                  *[line("작은 글자가 아주 많이 적힌 깨진 줄이다", 72, 200 + 12 * i, 8) for i in range(30)], number=2)
    para = OcrParagraph(text="다시 읽은 문단이다.", bbox=(0.1, 0.1, 0.5, 0.12), confidence=0.45)
    chart = Figure(box=(90.0, 500.0, 510.0, 700.0), category="chart", text="가로축", line_ids=frozenset({1}))
    before = build_specs([first, page(number=2)], ["digital", "digital"])
    result, ledgers = build_page_specs([first, broken], ["digital", "unreliable"], None, [[], [para]],
                                       [[], [FigureBlock(chart, "ocr", None)]], modes=["layer", "ocr"])
    assert [s for s in result if s["locator"]["page"] == 1] == before
    assert [(s["kind"], s["text"], s["text_source"], s["confidence"]) for s in result if s["locator"]["page"] == 2] == [
        ("paragraph", "다시 읽은 문단이다.", "ocr", 0.45), ("figure", "가로축", "ocr", 0.7)]
    assert ledgers[2] == Ledger(in_blocks=0, doubled=0)


def test_fragments_keep_each_char_id_through_the_reading_frame():
    """줄 조각 글자는 쪽 글자의 id를 그대로 든다: 읽기 좌표로 옮긴 글자(180° 뒤집힌 글자)도 같다. 공백·숨은 글자는
    조각에 없다."""
    upside = [Char(text=ch, x0=(400 - 11 * (k + 1)) / W, y0=(100 - 1.6) / H, x1=(400 - 11 * k) / W, y1=(100 + 8.3) / H,
                   baseline=1 - 100 / H, size=11, axes=(2, 3)) for k, ch in enumerate("가나")]
    chars = [*line("다 라", 72, 300), *upside, *line("숨은", 72, 400, invisible=True)]
    frags = fragments(page([replace(c, id=i) for i, c in enumerate(chars)]))
    assert [(f.text, [c.id for c in f.chars]) for f in frags] == [("다 라", [0, 2]), ("가나", [3, 4])]


def test_ledger_counts_two_equal_glyphs_at_one_place_twice():
    """같은 자리에 같은 글자를 두 번 찍은 쪽(PDFium이 지우지 않은 겹친 글자)은 값이 같은 Char 둘이다(id는 값 비교에 들지
    않는다). 장부는 값이 아니라 id로 세므로 둘 다 센다."""
    twice = line("가나", 72, 100)
    p = page(twice, twice)
    assert replace(p.chars[0], id=0) == replace(p.chars[2], id=2) and page_stats(p).chars == 4
    _, ledgers = build_page_specs([p], ["digital"])
    assert ledgers == {1: Ledger(in_blocks=4, doubled=0)}
