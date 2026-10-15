"""DOCX 문단·제목·목록·구역·처리 이력과 글자 장부(스펙 §5, §9, §10). 합성 문서는 build_ooxml_fixtures.py로 만든다."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from hanji import LocalEngine, MemoryStore, batch
from hanji.cli import main
from hanji.errors import ParseError
from hanji.formats.detect import default_parsers, detect_parser
from hanji.formats.ooxml.docx import parser as docx
from hanji.formats.ooxml.docx.parser import DocxParser
from hanji.viewer import render_html
from hanji_contracts import DocumentTree, FlowLocator

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
STYLES = B.styles(B.style("Normal", "Normal", default=True), B.style("H1", "heading 1"),
                  B.style("H2", "heading 2", based="H1"), B.style("Lst", "list", ppr_xml=(
                      '<w:pPr><w:numPr><w:numId w:val="3"/></w:numPr></w:pPr>')),
                  B.style("Hid", "hidden", kind="character", rpr_xml=B.rpr(vanish="")))


def parse(*items: str, styles: str | None = STYLES, **kw):
    """(ParsedSource, 구역별 장부 숫자)."""
    data = B.docx(B.document(*items), styles_xml=styles, numbering_xml=B.numbering("3", "4"), **kw)
    return DocxParser()._parse(data, "a.docx")


def texts(parsed) -> list[tuple[str, str]]:
    return [(b["kind"], b["text"]) for b in parsed.blocks]


def reasons(parsed) -> list[str]:
    return [r.fallback_reason for r in parsed.regions]


def test_paragraph_text_is_the_run_text_with_replacements_and_unwrapped_containers():
    run = B.r("가", "<w:tab/>", "나", "<w:br/>", "다", "<w:cr/>", "라", "<w:noBreakHyphen/>", "마", "<w:softHyphen/>",
              "바", '<w:ptab w:alignment="right"/>', "사")
    wrapped = ('<w:hyperlink r:id="rId9">' + B.r("링크") + "</w:hyperlink>"
               + "<w:smartTag>" + B.r("태그") + "</w:smartTag>"
               + "<w:sdt><w:sdtPr/><w:sdtContent>" + B.r("내용 칸") + "</w:sdtContent></w:sdt>"
               + "<w:customXml>" + B.r("사용자") + "</w:customXml>"
               + "<w:ins>" + B.r("넣음") + "</w:ins><w:moveTo>" + B.r("옮겨 옴") + "</w:moveTo>"
               + "<w:dir>" + B.r("방향") + "</w:dir><w:bdo>" + B.r("양방향") + "</w:bdo>")
    parsed, counts = parse(B.p(B.r("  "), run, B.r(" ")), B.p(wrapped), B.p(B.r("   ")), B.p())
    assert texts(parsed) == [("paragraph", "가\t나\n다\n라‑마바\t사"),
                             ("paragraph", "링크태그내용 칸사용자넣음옮겨 옴방향양방향")]
    assert parsed.mime == docx.MIME and parsed.pages == () and parsed.assets == {}
    assert [c.ok for c in counts] == [True] and counts[0].source == counts[0].in_blocks == 27
    assert reasons(parsed) == []


def test_headings_lists_section_paths_and_item_indices_over_two_sections():
    """개요 수준·제목 이름·Title, numPr 직접·스타일 목록, 빈 문단과 표도 순번을 센다. 구역 둘."""
    first = [B.p("지어낸 큰 제목", style="H1"), B.p(), B.p("본문 문단"), B.p("둘째 수준", style="H2"),
             B.p("구역 끝 문단", extra=B.sect())]
    second = [B.p("번호 항목", num="3"), B.p("스타일 목록", style="Lst"), B.p("모르는 번호", num="9"),
              B.p("개요 깊이", outline=7), B.p("다시 큰 제목", style="H1"), B.p("끝 문단")]
    parsed, counts = parse(*first, *second)
    got = [(b["kind"], b.get("level"), b["text"], b["locator"].paragraph_index) for b in parsed.blocks]
    assert got == [("heading", 1, "지어낸 큰 제목", 0), ("paragraph", None, "본문 문단", 2),
                   ("heading", 2, "둘째 수준", 3), ("paragraph", None, "구역 끝 문단", 4),
                   ("list_item", None, "번호 항목", 5), ("list_item", None, "스타일 목록", 6),
                   ("paragraph", None, "모르는 번호", 7), ("heading", 6, "개요 깊이", 8),
                   ("heading", 1, "다시 큰 제목", 9), ("paragraph", None, "끝 문단", 10)]
    assert parsed.blocks[7]["section_path"] == ("지어낸 큰 제목", "둘째 수준")
    assert parsed.blocks[-1]["locator"] == FlowLocator(section_path=("다시 큰 제목",), paragraph_index=10)
    assert len(counts) == 2 and all(c.ok for c in counts) and parsed.regions == ()


def test_hidden_text_is_not_in_blocks_and_is_recorded_per_paragraph():
    parsed, counts = parse(B.p(B.r("보임 "), B.r("숨긴 글", vanish=""), B.r("스타일 숨김", style="Hid"),
                               f'<w:r><w:rPr><w:specVanish/></w:rPr>{B.t(" 표시")}</w:r>', B.r("탭", "<w:tab/>",
                                                                                            vanish="")))
    assert texts(parsed) == [("paragraph", "보임  표시")]
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id, region.gate.checks[0].value) == (
        "hidden_text", "s0-p0-hidden-text-0", 9)
    assert counts[0].ok and counts[0].hidden == 9


def test_fields_keep_results_drop_instructions_and_record_unterminated_fields():
    toc = [B.p(B.fld("begin"), B.instr(" TOC \\o "), B.fld("separate"), B.r("1. 목차 줄"), B.r("\t3")),
           B.p(B.r("2. 둘째 줄 5"), B.fld("end")),
           B.p(B.instr("필드 밖 명령"), '<w:fldSimple w:instr=" PAGE ">' + B.r("7") + "</w:fldSimple>"),
           B.p(B.r("본문"), B.fld("begin"), B.instr("HYPERLINK x"))]
    parsed, counts = parse(*toc)
    assert texts(parsed) == [("paragraph", "1. 목차 줄\t3"), ("paragraph", "2. 둘째 줄 5"),
                             ("paragraph", "필드 밖 명령7"), ("paragraph", "본문")]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("field_unterminated", "s0-p3-field-unterminated-0", 10)]
    assert counts[0].ok


def test_alternate_content_reads_one_branch_and_records_the_skipped_chars():
    choice = (f'<mc:AlternateContent><mc:Choice Requires="x">{B.r("새 기능")}</mc:Choice>'
              f'<mc:Fallback>{B.r("대체 글")}</mc:Fallback></mc:AlternateContent>')
    ignored = f'<x:future>{B.r("무시한 글")}</x:future><x:box>{B.r(" 벗긴 글")}</x:box>'
    data = B.docx(B.document(B.p(B.r("앞 "), choice), B.p(ignored, process="x:box"), ignorable="w14 x"))
    parsed, counts = DocxParser()._parse(data, "a.docx")
    assert texts(parsed) == [("paragraph", "앞 대체 글"), ("paragraph", "벗긴 글")]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("alternate_content_skipped", "doc-alternate-content", 7)]
    assert counts[0].ok and counts[0].hidden == 7


def test_symbols_equations_unknown_run_children_and_tracked_changes():
    math = '<m:oMath><m:r><m:t>a</m:t></m:r><m:r><m:t>+b</m:t></m:r></m:oMath>'
    parsed, counts = parse(
        B.p(B.r("기호 "), '<w:r><w:sym w:font="Wingdings" w:char="F0E0"/></w:r>', B.r(" 수식 "), math),
        B.p(B.r("앞", "<x:future><w:t>모르는 자식</w:t></x:future>", "뒤"),
            f"<w:moveFrom>{B.r('옮겨 간 글')}</w:moveFrom>", '<w:del><w:r><w:delText>지운 글</w:delText></w:r></w:del>'))
    assert texts(parsed) == [("paragraph", "기호  수식 a+b"), ("paragraph", "앞모르는 자식뒤")]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("unknown_inline_element", "s0-p1-unknown-inline-0"), ("symbol_char", "s0-p0-symbol-0"),
        ("equation_text", "s0-p0-equation-0"), ("tracked_deletion_skipped", "doc-tracked-deletion")]
    assert parsed.regions[-1].gate.checks[0].value == 7 and counts[0].ok


def test_comments_are_counted_but_not_read():
    parsed, _ = parse(B.p("본문"), comments_xml=B.comments(B.p("지어낸 메모")))
    assert texts(parsed) == [("paragraph", "본문")]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("comments_skipped", "doc-comments", 5)]


def test_text_boxes_headers_and_footnotes_are_rescued_until_they_get_blocks():
    """OX-A: 글상자·머리말·각주 글자는 블록이 없어 구역 끝 구조 문단으로 살아남는다(OX-B가 블록으로 낸다)."""
    box = ("<w:r><w:drawing><wp:inline><a:graphic><a:graphicData><wps:wsp><wps:txbx><w:txbxContent>"
           + B.p("글상자 글") + "</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:inline>"
           "</w:drawing></w:r>")
    note = '<w:r><w:footnoteReference w:id="2"/></w:r>'
    sect = '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/></w:sectPr>'
    header = f'{B.DECL}<w:hdr{B.xmlns("w")}>{B.p("머리말 글")}</w:hdr>'
    notes = (f'{B.DECL}<w:footnotes{B.xmlns("w")}><w:footnote w:type="separator" w:id="0">{B.p("구분")}</w:footnote>'
             f'<w:footnote w:id="2">{B.p("각주 글")}</w:footnote></w:footnotes>')
    parsed, counts = parse(B.p(B.r("본문"), box), B.p(B.r("참조"), note, extra=sect), B.p("둘째 구역"),
                           extra=[("word/header1.xml", header), ("word/footnotes.xml", notes)],
                           doc_rels=[("rIdH", B.REL["header"], "header1.xml"),
                                     ("rIdF", B.REL["footnotes"], "footnotes.xml")])
    assert [(b["kind"], b["text"], b["confidence"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("paragraph", "본문", 1.0, 0), ("paragraph", "참조", 1.0, 1), ("paragraph", "글상자 글\n머리말 글", 0.3, 0),
        ("paragraph", "각주 글", 0.3, 1), ("paragraph", "둘째 구역", 1.0, 2)]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("unassigned_text", "s0-p0-unassigned-text-0")]
    assert [c.ok for c in counts] == [True, True] and counts[0].rescued == 11


SAMPLE = [B.p("첫째 문단"), B.p(B.r("가", "<w:tab/>", "나"), B.r("둘째"))]


def test_a_dropped_source_is_rescued_once_with_unassigned_text(monkeypatch):
    original = docx._Section.content

    def drop_first(self, p, anchor):
        bundles, sizes, lines = original(self, p, anchor)
        return (bundles[1:] if anchor == 1 else bundles), sizes, lines

    monkeypatch.setattr(docx._Section, "content", drop_first)
    parsed, counts = parse(*SAMPLE)
    assert texts(parsed) == [("paragraph", "첫째 문단"), ("paragraph", "나둘째"), ("paragraph", "가")]
    assert counts[0].ok and counts[0].rescued == 1 and reasons(parsed) == ["unassigned_text"]


def test_a_source_put_twice_is_a_coverage_mismatch(monkeypatch):
    original = docx._Section.content
    monkeypatch.setattr(docx._Section, "content", lambda self, p, anchor: (
        lambda out: (out[0] * 2, out[1], out[2]))(original(self, p, anchor)))
    parsed, counts = parse(*SAMPLE)
    assert texts(parsed)[0] == ("paragraph", "첫째 문단첫째 문단")
    assert (counts[0].source, counts[0].in_blocks, counts[0].doubled, counts[0].partial) == (8, 16, 4, 4)
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id) == ("coverage_mismatch", "s0-coverage")


def test_putting_an_out_of_range_source_is_a_coverage_mismatch_and_it_is_never_rescued(monkeypatch):
    choice = (f'<mc:AlternateContent><mc:Choice Requires="wps">{B.r("고른 쪽")}</mc:Choice>'
              f'<mc:Fallback>{B.r("버린 쪽")}</mc:Fallback></mc:AlternateContent>')
    original = docx._Section.piece

    def keep_all(self, source, p, run, out, sizes):
        if self.doc.walker.marks[id(source)] != docx.NORMAL:
            out.append((self.ledger.source_of(source), docx._text(source)))
        original(self, source, p, run, out, sizes)

    parsed, counts = parse(B.p(choice), B.p(B.fld("begin"), B.instr("PAGE"), B.fld("end")))
    assert texts(parsed) == [("paragraph", "고른 쪽")] and counts[0].ok  # 버린 쪽·명령은 살리지 않는다
    monkeypatch.setattr(docx._Section, "piece", keep_all)
    monkeypatch.setattr(docx._Section, "content", lambda self, p, anchor: _all_sources(self, p))
    parsed, counts = parse(B.p(choice), B.p(B.fld("begin"), B.instr("PAGE"), B.fld("end")))
    assert texts(parsed) == [("paragraph", "고른 쪽버린 쪽"), ("paragraph", "PAGE")]
    assert (counts[0].hidden, counts[0].doubled) == (7, 2) and "coverage_mismatch" in reasons(parsed)


def _all_sources(section, p):
    """표시와 상관없이 문단 안 출처를 모두 넣는다(잘못된 조립 흉내)."""
    out = [(section.ledger.source_of(e), docx._text(e)) for e in p.iter() if e.tag in docx.SOURCES]
    return out, docx.Counter(), 1


def test_without_heading_styles_large_short_paragraphs_are_headings_by_size():
    plain = B.styles(B.style("Normal", "Normal", default=True, rpr_xml=B.rpr(sz=20)))
    parsed, _ = parse(B.p(B.r("큰 제목", sz=32)), B.p("지어낸 본문 문단 하나는 이만큼 길다"), B.p("본문 문단 둘도 길게 쓴다"),
                      B.p(B.r("굵은 소제목", sz=22, b="")), B.p(B.r("큰 세 줄", "<w:br/>", "둘", "<w:br/>", "셋", sz=32)),
                      B.p(B.r("쪽 나눔", '<w:br w:type="page"/>', "뒤", sz=32)), styles=plain)
    got = [(b["kind"], b.get("level"), b["confidence"]) for b in parsed.blocks]
    assert got == [("heading", 1, 0.6), ("paragraph", None, 1.0), ("paragraph", None, 1.0), ("heading", 2, 0.6),
                   ("paragraph", None, 1.0), ("heading", 1, 0.6)]
    assert parsed.blocks[-1]["text"] == "쪽 나눔 뒤"
    parsed, _ = parse(B.p(B.r("큰 글", sz=32)), B.p("본문"), B.p("제목 스타일", style="H1"), styles=STYLES)
    assert [b["kind"] for b in parsed.blocks] == ["paragraph", "paragraph", "heading"]  # 제목 스타일이 있으면 쓰지 않는다


def test_parse_errors_name_the_part_and_line():
    with pytest.raises(ParseError, match="invalid XML") as info:
        DocxParser().parse(B.docx(B.DECL + "\n<w:document>\n<broken"), "a.docx")
    assert info.value.location.startswith("a.docx:word/document.xml:")
    with pytest.raises(ParseError, match="missing w:body"):
        DocxParser().parse(B.docx(f'<w:document{B.xmlns("w")}/>'), "a.docx")


def test_detection_cli_folder_ingest_viewer_and_same_bytes(tmp_path, capsys, monkeypatch):
    assert isinstance(detect_parser("보고서.DOCX", default_parsers(ocr=False, layout=False)), DocxParser)
    for name in ("a.docx", "b.DOCX", "c.doc"):
        (tmp_path / name).write_bytes(b"x")
    extensions = frozenset(e for p in default_parsers(ocr=False, layout=False) for e in p.extensions)
    entries, skipped = batch.expand([tmp_path], False, extensions)
    assert [Path(e.path).name for e in entries] == ["a.docx", "b.DOCX"] and skipped == 1
    good = tmp_path / "좋은.docx"
    good.write_bytes(B.docx(B.document(B.p("지어낸 제목", style="H1"), B.p("본문")), styles_xml=STYLES))
    monkeypatch.delenv("HANJI_DB", raising=False)
    db = tmp_path / "state.db"
    assert main(["parse", str(good), "--db", str(db)]) == 0
    tree = DocumentTree.model_validate(json.loads(capsys.readouterr().out))
    assert tree.blocks[0].text == "지어낸 제목" and tree.source.page_count is None
    broken = tmp_path / "깨진.docx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    assert main(["parse", str(broken), "--db", str(db)]) == 4
    assert "not a DOCX" in capsys.readouterr().err
    (raw,) = re.findall(r'<script type="application/json" id="ko-data">(.*?)</script>', render_html(tree), re.DOTALL)
    assert json.loads(raw)["blocks"][1]["where"] == "문단 1"
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(good))
    assert engine.ingest(str(good)) == ref  # 같은 원본은 그대로


def _margin_and_note_parts():
    """머리말 하나(구역 0·1이 함께 참조)와 각주 하나를 단 꾸러미 재료."""
    header = f'{B.DECL}<w:hdr{B.xmlns("w")}>{B.p("공유 머리말")}</w:hdr>'
    notes = f'{B.DECL}<w:footnotes{B.xmlns("w")}><w:footnote w:id="2">{B.p("각주 글")}</w:footnote></w:footnotes>'
    return dict(extra=[("word/header1.xml", header), ("word/footnotes.xml", notes)],
                doc_rels=[("rIdH", B.REL["header"], "header1.xml"), ("rIdF", B.REL["footnotes"], "footnotes.xml")])


SHARED = '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/></w:sectPr>'


def test_story_part_roots_are_held_until_parsing_ends(monkeypatch):
    """Walker·장부는 id(w:p)로 문단을 가리킨다: 머리말·각주 파트 뿌리를 파싱이 끝날 때까지 붙잡아 id가 다시 쓰이지 않게."""
    seen = []
    original = DocxParser._stories

    def spy(self, doc, *rest):
        original(self, doc, *rest)
        seen.append(doc)

    monkeypatch.setattr(DocxParser, "_stories", spy)
    parse(B.p(B.r("참조"), '<w:r><w:footnoteReference w:id="2"/></w:r>', extra=SHARED), **_margin_and_note_parts())
    (doc,) = seen
    assert [root.tag for root in doc.held] == [f"{docx.W}hdr", f"{docx.W}footnotes"]


def test_body_level_alternate_content_and_unknown_wrappers_give_body_items():
    """본문 층 mc:AlternateContent는 고른 쪽 문단이 항목이고 버린 쪽 글자는 범위 밖, 모르는 감싼 요소는 벗긴다(무시
    표시 요소는 통째로 항목이 아니고 글자는 범위 밖)."""
    alternate = (f'<mc:AlternateContent><mc:Choice Requires="w14">{B.p("고른 문단")}</mc:Choice>'
                 f'<mc:Fallback>{B.p("버린 문단")}</mc:Fallback></mc:AlternateContent>')
    data = B.docx(B.document(alternate, f'<w14:wrap>{B.p("감싼 문단")}</w14:wrap>', f'<x:box>{B.p("무시한 문단")}</x:box>',
                             B.p("끝 문단"), ignorable="w14 x"), styles_xml=STYLES)
    parsed, counts = DocxParser()._parse(data, "a.docx")
    assert [(b["text"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("고른 문단", 0), ("감싼 문단", 1), ("끝 문단", 2)]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("alternate_content_skipped", "doc-alternate-content", 9)]
    assert counts[0].ok and counts[0].hidden == 9


def test_a_symbol_with_code_zero_puts_no_nul_into_blocks():
    """w:sym 코드 범위는 장부(ledger.sym_char)와 같은 규칙: 0은 글자가 아니다."""
    parsed, counts = parse(B.p(B.r("앞"), '<w:r><w:sym w:font="Symbol" w:char="0000"/></w:r>', B.r("뒤")))
    assert texts(parsed) == [("paragraph", "앞뒤")]
    assert all("\x00" not in b["text"] for b in parsed.blocks) and counts[0].ok


def test_a_header_shared_by_two_sections_is_rescued_once():
    """구역 둘이 같은 머리말 파트를 참조해도 그 글자는 한 번(처음 참조한 구역에서)만 살린다(스펙 §7.2)."""
    parsed, counts = parse(B.p("첫 구역", extra=SHARED), B.p("둘째 구역", extra=SHARED), B.p("셋째 구역"),
                           **_margin_and_note_parts())
    assert [b["text"] for b in parsed.blocks].count("공유 머리말") == 1
    assert [c.rescued for c in counts] == [5, 0, 0] and all(c.ok for c in counts)


def test_a_toc_field_continues_across_sections():
    """본문은 이야기 하나: 구역 0에서 시작한 목차 필드의 결과 줄이 구역 1에서도 블록이다."""
    first = B.p(B.fld("begin"), B.instr(" TOC \\o "), B.fld("separate"), B.r("1. 첫 줄"), extra=B.sect())
    parsed, counts = parse(first, B.p(B.r("2. 둘째 줄"), B.fld("end")), B.p("본문"))
    assert texts(parsed) == [("paragraph", "1. 첫 줄"), ("paragraph", "2. 둘째 줄"), ("paragraph", "본문")]
    assert reasons(parsed) == [] and len(counts) == 2 and all(c.ok for c in counts)


def test_ignored_body_elements_are_not_items_and_do_not_split_sections():
    """무시 표시 요소(ProcessContent 아님)는 통째로 항목이 아니다: 순번 공백·빈 표 블록·안 w:sectPr의 구역 나눔이 없다."""
    ignored = (f'<x:box>{B.p("무시한 문단", extra=B.sect())}{B.tbl(B.tr(B.tc("가"), B.tc("나")), B.tr(B.tc("다"), B.tc("라")))}'
               "</x:box>")
    kept = f'<x:keep>{B.p("벗긴 문단")}</x:keep>'
    body = B.document(B.p("앞 문단"), ignored, kept, B.p("뒤 문단"), ignorable="w14 x").replace(
        "<w:body>", '<w:body mc:ProcessContent="x:keep">')
    parsed, counts = DocxParser()._parse(B.docx(body, styles_xml=STYLES), "a.docx")
    assert [(b["kind"], b["text"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("paragraph", "앞 문단", 0), ("paragraph", "벗긴 문단", 1), ("paragraph", "뒤 문단", 2)]
    assert [(r.fallback_reason, r.gate.checks[0].value) for r in parsed.regions] == [("alternate_content_skipped", 9)]
    assert len(counts) == 1 and counts[0].ok


def test_math_keeps_wrapped_runs_in_place():
    """m:oMath 안 w:ins·w:hyperlink·w:sdt로 감싼 런도 문서 순서 그 자리에 든다(살리기로 밀려나지 않는다)."""
    math = ("<m:oMath><m:r><m:t>a</m:t></m:r><w:ins><m:r><m:t>+</m:t></m:r></w:ins>"
            '<w:hyperlink r:id="rId9"><w:r><w:t>b</w:t></w:r></w:hyperlink>'
            "<w:sdt><w:sdtContent><m:r><m:t>=c</m:t></m:r></w:sdtContent></w:sdt>"
            "<w:del><m:r><m:t>지움</m:t></m:r></w:del></m:oMath>")
    parsed, counts = parse(B.p(B.r("식 "), math, B.r(" 끝")))
    assert texts(parsed) == [("paragraph", "식 a+b=c 끝")]
    assert reasons(parsed) == ["equation_text", "tracked_deletion_skipped"] and counts[0].ok
