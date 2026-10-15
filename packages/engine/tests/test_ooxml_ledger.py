"""OOXML 출처 열거와 표시(스펙 §5.2, §5.4~§5.6, §9). 블록 조립 없이 Walker만 본다."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

from hanji.formats.flowledger import Ledger
from hanji.formats.ooxml.docx.styles import read_styles
from hanji.formats.ooxml.ledger import HIDDEN, NORMAL, OUT, Story, Walker, part_chars, source_text
from hanji.formats.zipxml import scan

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
W = f"{{{B.W}}}"


def walk(*items: str, styles: str | None = None, ignorable: str = "w14"):
    """본문 항목들을 순번 닻으로 훑는다. (Walker, Ledger, [(출처 글자, 표시)])."""
    xml = B.document(*items, ignorable=ignorable)
    root, prefixes = ET.fromstring(xml), scan(xml.encode("utf-8"), "t", 256)
    walker = Walker(read_styles(ET.fromstring(styles) if styles else None, None))
    ledger = Ledger()
    story = Story()
    body = root.find(f"{W}body")
    for anchor, item in enumerate(x for x in body if x.tag != f"{W}sectPr"):
        walker.walk(item, prefixes, ledger, 0, anchor, story, (root, body))
    walker.close(story)
    walker.finish()
    return walker, ledger, [(source_text(s.element), walker.marks[id(s.element)]) for s in ledger.sources]


def notes(walker) -> list[tuple]:
    return [(n.anchor, n.reason, n.value) for n in walker.notes]


def alt(*children: str) -> str:
    return f"<mc:AlternateContent>{''.join(children)}</mc:AlternateContent>"


def test_alternate_content_reads_one_branch_and_marks_the_other_out_of_range():
    walker, ledger, got = walk(
        B.p(alt(f'<mc:Choice Requires="wps">{B.r("새 형식")}</mc:Choice>', f"<mc:Fallback>{B.r('옛 형식')}</mc:Fallback>")),
        B.p(alt(f'<mc:Choice Requires="x">{B.r("모름")}</mc:Choice>', f"<mc:Fallback>{B.r('대체')}</mc:Fallback>")),
        B.p(alt('<mc:Choice Requires="wps"/>', f"<mc:Fallback>{B.r('빈 쪽 대신')}</mc:Fallback>")))
    assert got == [("새 형식", NORMAL), ("옛 형식", OUT), ("모름", OUT), ("대체", NORMAL), ("빈 쪽 대신", OUT)]
    assert [s.hidden for s in ledger.sources] == [False, True, True, False, True]
    assert notes(walker) == [(None, "alternate_content_skipped", 9)]


def test_ignorable_elements_are_out_of_range_unless_process_content():
    walker, _, got = walk(B.p(B.r("보임"), f"<x:future>{B.r('무시')}</x:future>"),
                          f'<w:p mc:ProcessContent="x:box"><x:box>{B.r("벗김")}</x:box></w:p>', ignorable="w14 x")
    assert got == [("보임", NORMAL), ("무시", OUT), ("벗김", NORMAL)]
    _, _, plain = walk(B.p(f"<x:future>{B.r('모르는 요소')}</x:future>"))
    assert plain == [("모르는 요소", NORMAL)]  # 무시 표시가 없으면 모르는 요소의 글자는 보통 출처다
    assert notes(walker) == [(None, "alternate_content_skipped", 2)]


def test_fields_span_paragraphs_and_hide_every_instruction_segment():
    walker, _, got = walk(
        B.p(B.instr("밖 명령"), B.r("앞"), B.fld("begin"), B.instr(" TOC \\o "), B.fld("separate")),
        B.p(B.r("목차 줄 1"), B.fld("begin"), B.instr("PAGEREF a"), B.fld("separate"), B.r("3"), B.fld("end")),
        B.p(B.r("목차 줄 2"), B.fld("end"), B.fld("separate"), B.fld("end"), B.r("뒤")),
        B.p(B.fld("begin"), B.instr("IF "), B.fld("begin"), B.instr("REF b"), B.fld("separate"), B.r("안 결과"),
            B.fld("end"), B.fld("separate"), B.r("겉 결과"), B.fld("end")),
        B.p('<w:fldSimple w:instr="PAGE">' + B.r("7") + "</w:fldSimple>"))
    assert got == [("밖 명령", NORMAL), ("앞", NORMAL), (" TOC \\o ", OUT), ("목차 줄 1", NORMAL), ("PAGEREF a", OUT),
                   ("3", NORMAL), ("목차 줄 2", NORMAL), ("뒤", NORMAL), ("IF ", OUT), ("REF b", OUT),
                   ("안 결과", OUT), ("겉 결과", NORMAL), ("7", NORMAL)]
    assert notes(walker) == []  # 정상 필드의 명령은 처리 이력이 없다


def test_an_unterminated_field_is_recorded_once_per_story_with_the_hidden_chars():
    walker, _, got = walk(B.p(B.r("앞"), B.fld("begin"), B.instr("HYPERLINK x")),
                          B.p(B.r("끝나지 않은 명령"), B.fld("begin"), B.instr("PAGE"), B.fld("separate"), B.r("2")))
    assert [m for _, m in got] == [NORMAL, OUT, OUT, OUT, OUT]
    assert notes(walker) == [(0, "field_unterminated", 22)]


def test_an_unterminated_field_past_separate_hides_nothing_because_of_itself():
    walker, _, got = walk(B.p(B.fld("begin"), B.instr("PAGE"), B.fld("separate"), B.r("5")))
    assert got == [("PAGE", OUT), ("5", NORMAL)]  # 명령은 정상 필드도 숨기고, 결과는 보인다
    assert notes(walker) == [(0, "field_unterminated", 0)]


def test_tracked_deletions_are_out_of_range_and_insertions_are_text():
    walker, _, got = walk(B.p(f"<w:ins>{B.r('넣음')}</w:ins>", f"<w:moveFrom>{B.r('옮겨 간 글')}</w:moveFrom>",
                              f'<w:del><w:r><w:delText>지움</w:delText></w:r>{B.fld("begin")}</w:del>', B.r("끝")))
    assert got == [("넣음", NORMAL), ("옮겨 간 글", OUT), ("끝", NORMAL)]  # 지운 필드 시작은 상태를 바꾸지 않는다
    assert notes(walker) == [(None, "tracked_deletion_skipped", 6)]


def test_vanish_from_the_run_or_its_styles_is_hidden_and_spec_vanish_is_not():
    styles = B.styles(B.style("HP", "hidden para", rpr_xml=B.rpr(vanish="")),
                      B.style("HC", "hidden char", kind="character", rpr_xml=B.rpr(vanish="1")))
    walker, ledger, got = walk(
        B.p(B.r("보임"), B.r("숨김", vanish=""), B.r("글자 스타일", style="HC"), B.r("다시 보임", vanish="false"),
            f'<w:r><w:rPr><w:specVanish/></w:rPr>{B.t("표시 기호")}</w:r>'),
        B.p(B.r("문단 스타일"), style="HP"), styles=styles)
    assert got == [("보임", NORMAL), ("숨김", HIDDEN), ("글자 스타일", HIDDEN), ("다시 보임", NORMAL),
                   ("표시 기호", NORMAL), ("문단 스타일", HIDDEN)]
    assert notes(walker) == [(0, "hidden_text", 7), (1, "hidden_text", 5)]
    assert ledger.count().hidden == 12


def test_a_text_box_is_its_own_story_for_fields():
    box = ('<w:r><w:drawing><wps:txbx><w:txbxContent>' + B.p(B.r("글상자"), B.fld("begin"), B.instr("SEQ"))
           + "</w:txbxContent></wps:txbx></w:drawing></w:r>")
    walker, _, got = walk(B.p(B.fld("begin"), B.instr("REF a"), box, B.fld("separate"), B.r("결과"), B.fld("end")))
    assert got == [("REF a", OUT), ("글상자", NORMAL), ("SEQ", OUT), ("결과", NORMAL)]
    assert notes(walker) == [(0, "field_unterminated", 3)]


def test_symbols_and_equation_text_are_sources_with_paragraph_records():
    math = ('<m:oMath><m:r><m:t>x</m:t></m:r><m:f><m:num><m:r><m:t>1</m:t></m:r></m:num>'
            '<m:den><m:r><m:t>2</m:t></m:r></m:den></m:f></m:oMath>')
    walker, ledger, got = walk(B.p(B.r("값 "), '<w:r><w:sym w:font="Symbol" w:char="F0B7"/></w:r>', math),
                               B.p('<w:r><w:sym w:char="zz"/><w:sym w:char="0020"/></w:r>'))
    assert got == [("값 ", NORMAL), ("", NORMAL), ("x", NORMAL), ("1", NORMAL), ("2", NORMAL), ("", NORMAL),
                   (" ", NORMAL)]
    assert [s.n for s in ledger.sources] == [1, 1, 1, 1, 1, 0, 0]
    assert notes(walker) == [(0, "symbol_char", 0), (0, "equation_text", 0), (1, "symbol_char", 0)]


def test_part_chars_counts_sources_of_parts_outside_the_ledger():
    root = ET.fromstring(B.comments(B.p("지어낸 메모 하나"), B.p(B.r("둘째"), '<w:r><w:delText>지움</w:delText></w:r>')))
    assert part_chars(root) == 9
