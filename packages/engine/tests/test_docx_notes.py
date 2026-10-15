"""DOCX 머리말·꼬리말·각주·미주(스펙 §7.2, §7.3, §9). 합성 문서는 build_ooxml_fixtures.py로 만든다."""

import importlib.util
from pathlib import Path

from hanji.formats.ooxml.docx.parser import DocxParser

HERE = Path(__file__).resolve().parent
CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
STYLES = B.styles(B.style("Normal", "Normal", default=True), B.style("H1", "heading 1"))
PARTS = {"rIdH1": ("header", "header1.xml"), "rIdH2": ("header", "header2.xml"), "rIdF1": ("footer", "footer1.xml"),
         "rIdF2": ("footer", "footer2.xml"), "rIdFN": ("footnotes", "footnotes.xml"),
         "rIdEN": ("endnotes", "endnotes.xml")}


def parse(*items: str, parts: dict[str, str], final: str | None = None, extra: tuple = (), rels_more: tuple = ()):
    """(ParsedSource, 구역별 장부 숫자). parts는 word/ 아래 멤버 → XML(관계 id는 PARTS에서 찾는다)."""
    rels = [(rid, B.REL[kind], member) for rid, (kind, member) in PARTS.items() if member in parts] + [*rels_more]
    data = B.docx(B.document(*items, final=final), styles_xml=STYLES, doc_rels=rels,
                  extra=[*((f"word/{m}", x) for m, x in parts.items()), *extra])
    return DocxParser()._parse(data, "a.docx")


def shown(parsed) -> list[tuple[str, str, int]]:
    return [(b["kind"], b["text"], b["locator"].paragraph_index) for b in parsed.blocks]


def reasons(parsed) -> list[tuple[str, str]]:
    return [(r.fallback_reason, r.region_id) for r in parsed.regions]


def test_headers_and_footers_come_first_in_the_section_that_first_references_them():
    """구역 첫 항목 자리에 참조 차례로, 파트 문단을 \\n으로 이은 블록 하나와 그 뒤 안의 표·그림(머리말 관계에서)."""
    header = B.margin("hdr", B.p("머리말 첫 줄", style="H1"), B.p(B.r("둘째 줄 "), B.drawing(B.pic("rIdP"))),
                      B.tbl(B.tr(B.tc("머리 가"), B.tc("머리 나")), B.tr(B.tc("머리 다"), B.tc("머리 라"))))
    footer = B.margin("ftr", B.p(B.r("쪽 "), B.fld("begin"), B.instr(" PAGE "), B.fld("separate"), B.r("3"),
                                 B.fld("end")))
    first = B.sect_refs(("header", "default", "rIdH1"), ("footer", "default", "rIdF1"), ("header", "first", "rIdH1"))
    second = B.sect_refs(("header", "default", "rIdH1"), ("footer", "default", "rIdF2"))
    pictures = [("word/_rels/header1.xml.rels", B.rels(("rIdP", f"{B.R}/image", "media/gray.png"))),
                ("word/media/gray.png", B.GRAY_PNG)]
    parsed, counts = parse(B.p("첫 구역 제목", style="H1"), B.p("첫 구역 끝", extra=first), B.p("둘째 구역"),
                           parts={"header1.xml": header, "footer1.xml": footer,
                                  "footer2.xml": B.margin("ftr", "둘째 꼬리말")}, final=second, extra=pictures)
    assert shown(parsed) == [
        ("page_header", "머리말 첫 줄\n둘째 줄", 0), ("figure", "", 0), ("table", "머리 가\t머리 나\n머리 다\t머리 라", 0),
        ("page_footer", "쪽 3", 0), ("heading", "첫 구역 제목", 0), ("paragraph", "첫 구역 끝", 1),
        ("page_footer", "둘째 꼬리말", 2), ("paragraph", "둘째 구역", 2)]
    assert "figure" in parsed.blocks[1] and len(parsed.assets) == 1
    assert parsed.regions == () and all(c.ok and c.rescued == 0 for c in counts)


def test_footnotes_then_endnotes_at_the_section_end_in_reference_order():
    foot = B.notes("footnotes", ("2", None, B.p('<w:r><w:footnoteRef/></w:r>', B.r(" 둘째 각주"))),
                   ("3", None, B.p("칸 속 각주") + B.p("각주 둘째 문단")), ("4", None, B.p("다음 구역 각주")))
    end = B.notes("endnotes", ("1", None, B.p('<w:r><w:endnoteRef/></w:r>', B.r(" 첫 미주"))))
    cell = B.tbl(B.tr(B.tc(B.p(B.r("칸"), B.note_ref("footnote", "3"))), B.tc("옆")), B.tr(B.tc("아래"), B.tc("끝")))
    parsed, counts = parse(B.p(B.r("참조 앞"), B.note_ref("footnote", "2"), B.note_ref("endnote", "1")), cell,
                           B.p(B.r("다시 참조"), B.note_ref("footnote", "2"), extra=B.sect()),
                           B.p(B.r("둘째 구역"), B.note_ref("footnote", "4")),
                           parts={"footnotes.xml": foot, "endnotes.xml": end})
    assert shown(parsed) == [
        ("paragraph", "참조 앞", 0), ("table", "칸\t옆\n아래\t끝", 1), ("paragraph", "다시 참조", 2),
        ("paragraph", "둘째 각주", 0), ("paragraph", "칸 속 각주", 1), ("paragraph", "각주 둘째 문단", 1),
        ("paragraph", "첫 미주", 0), ("paragraph", "둘째 구역", 3), ("paragraph", "다음 구역 각주", 3)]
    assert parsed.regions == () and [c.ok and c.rescued == 0 for c in counts] == [True, True]


def test_special_notes_are_out_of_range_and_counted():
    foot = B.notes("footnotes", ("-1", "separator", B.p("<w:r><w:separator/></w:r>")),
                   ("0", "continuationNotice", B.p("다음 쪽에 계속")), ("1", None, B.p("보통 각주")))
    parsed, counts = parse(B.p(B.r("본문"), B.note_ref("footnote", "1")), parts={"footnotes.xml": foot})
    assert shown(parsed) == [("paragraph", "본문", 0), ("paragraph", "보통 각주", 0)]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("special_note_skipped", "doc-special-note", 6)]
    assert counts[0].ok and counts[0].hidden == 6 and counts[0].rescued == 0


def test_unreferenced_parts_and_notes_reached_only_by_hidden_runs_are_counted_not_read():
    """참조되지 않은 머리말 파트와 주석, 숨긴 런·필드 명령 안 참조만 있는 주석은 글자 수만 남는다."""
    foot = B.notes("footnotes", ("1", None, B.p("숨긴 참조 각주")), ("2", None, B.p("참조 없는 각주")),
                   ("3", None, B.p("명령 안 각주")))
    command = B.p(B.fld("begin"), B.instr(" IF "), B.note_ref("footnote", "3"), B.fld("separate"), B.r("결과"),
                  B.fld("end"))
    parsed, counts = parse(B.p(B.r("본문"), B.note_ref("footnote", "1", vanish=True)), command,
                           parts={"footnotes.xml": foot, "header2.xml": B.margin("hdr", "안 쓰는 머리말")})
    assert shown(parsed) == [("paragraph", "본문", 0), ("paragraph", "결과", 1)]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("unreferenced_part_skipped", "doc-unreferenced-part", 23)]
    assert counts[0].ok and counts[0].rescued == 0


def test_a_trailing_empty_section_puts_its_header_at_the_last_item():
    last = B.sect_refs(("header", "default", "rIdH2"))
    parsed, counts = parse(B.p("첫 문단"), B.p("구역 끝 문단", extra=B.sect()), final=last,
                           parts={"header2.xml": B.margin("hdr", "빈 구역 머리말")})
    assert shown(parsed) == [("paragraph", "첫 문단", 0), ("paragraph", "구역 끝 문단", 1),
                             ("page_header", "빈 구역 머리말", 1)]
    assert len(counts) == 2 and all(c.ok and c.rescued == 0 for c in counts)


def test_text_boxes_in_headers_follow_the_header_part_prefixes_and_fields_restart_per_part():
    """머리말 파트는 이야기 하나: 본문에서 끝나지 않은 필드가 머리말 글자를 가리지 않는다. 글상자 mc 짝은 한쪽만."""
    alternate = ('<w:r><mc:AlternateContent><mc:Choice Requires="hs">' + B.drawing(B.wsp("머리 글상자"))[5:-6]
                 + "</mc:Choice><mc:Fallback>" + B.vpict(B.vshape(None, "옛 머리 글상자"))[5:-6]
                 + "</mc:Fallback></mc:AlternateContent></w:r>")
    header = B.margin("hdr", B.p(B.r("머리말"), alternate)).replace("wps:", "hs:").replace("xmlns:wps=", "xmlns:hs=")
    assert 'xmlns:hs="' in header and "xmlns:wps" not in header  # 이 파트만 wps 이름공간을 hs 접두어로 선언한다
    parsed, counts = parse(B.p(B.r("본문 "), B.fld("begin"), B.instr(" HYPERLINK x ")),
                           final=B.sect_refs(("header", "default", "rIdH1")), parts={"header1.xml": header})
    assert shown(parsed) == [("page_header", "머리말", 0), ("paragraph", "머리 글상자", 0), ("paragraph", "본문", 0)]
    assert [r for r, _ in reasons(parsed)] == ["shape_text", "field_unterminated", "alternate_content_skipped"]
    assert counts[0].ok


def test_a_later_note_with_a_duplicate_id_is_counted_as_unreferenced_text():
    """같은 w:id의 뒤 주석은 블록도 처리 이력도 없이 버려지지 않고, 글자 수가 unreferenced_part_skipped에 든다."""
    foot = B.notes("footnotes", ("1", None, B.p("첫 각주")), ("1", None, B.p("겹친 각주")))
    parsed, counts = parse(B.p(B.r("본문"), B.note_ref("footnote", "1")), parts={"footnotes.xml": foot})
    assert shown(parsed) == [("paragraph", "본문", 0), ("paragraph", "첫 각주", 0)]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("unreferenced_part_skipped", "doc-unreferenced-part", 4)]
    assert counts[0].ok and counts[0].rescued == 0


def test_an_unreferenced_part_reached_through_two_relationship_ids_is_counted_once():
    parsed, counts = parse(B.p("본문"), parts={"header2.xml": B.margin("hdr", "안 쓰는 머리말")},
                           rels_more=(("rIdH9", B.REL["header"], "header2.xml"),))
    assert shown(parsed) == [("paragraph", "본문", 0)]
    assert [(r.fallback_reason, r.gate.checks[0].value) for r in parsed.regions] == [
        ("unreferenced_part_skipped", 6)]
    assert counts[0].ok and counts[0].rescued == 0


def test_a_chart_in_a_header_resolves_its_relationship_in_the_header_part():
    """머리말 파트의 차트: 관계는 header1.xml.rels에만 있다. 머리말 자리에 머리말 글, 그림, 차트 글 문단."""
    header = B.margin("hdr", B.p("머리말 글"), B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI)))
    chart = ("word/charts/chart1.xml", B.chart_part("머리 차트 제목"))
    link = ("word/_rels/header1.xml.rels", B.rels(("rIdC", f"{B.R}/chart", "charts/chart1.xml")))
    parsed, counts = parse(B.p("본문"), parts={"header1.xml": header}, extra=(chart, link),
                           final=B.sect_refs(("header", "default", "rIdH1")))
    assert shown(parsed) == [("page_header", "머리말 글", 0), ("figure", "", 0), ("paragraph", "머리 차트 제목", 0),
                             ("paragraph", "본문", 0)]
    assert [(r.fallback_reason, r.gate.checks[0].value if r.gate else None) for r in parsed.regions] == [
        ("embedded_object", None), ("chart_text", 0)]
    assert counts[0].ok and counts[0].rescued == 0
