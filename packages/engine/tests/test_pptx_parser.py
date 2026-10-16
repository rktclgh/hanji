"""PPTX 슬라이드·도형 글자·자리 표시자·목록·꼬리말·숨김·제목과 글자 장부(스펙 §8.1~§8.3, §9, §10). 합성 문서는
build_ooxml_fixtures.py로 만든다."""

import importlib.util
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from hanji import LocalEngine, MemoryStore, batch
from hanji.cli import main
from hanji.errors import ParseError
from hanji.formats.detect import default_parsers, detect_parser
from hanji.formats.ooxml.pptx import parser as pptx
from hanji.formats.ooxml.pptx.parser import PptxParser
from hanji.viewer import render_html
from hanji_contracts import DocumentTree, SlideLocator

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


def parse(*slides: str, **kw):
    """(ParsedSource, 슬라이드별 장부 숫자)."""
    return PptxParser()._parse(B.pptx(*slides, **kw), "a.pptx")


def shown(parsed) -> list[tuple]:
    """(kind, 글자, 슬라이드, 도형 순번)."""
    return [(b["kind"], b["text"], b["locator"].slide, b["locator"].shape_index) for b in parsed.blocks]


def reasons(parsed) -> list[tuple]:
    return [(r.fallback_reason, r.region_id, r.gate.checks[0].value if r.gate else None) for r in parsed.regions]


def test_slides_in_list_order_titles_first_then_tree_order_with_placeholder_kinds():
    first = B.slide(B.sp("일반 글상자 글"), B.sp("본문 첫 항목", B.para("글머리 없앤 문단", bullet="none"), ph="body"),
                    B.sp("지어낸 첫 제목", ph="title"), B.sp("부제 문단", ph="subTitle"),
                    B.sp("종류 없는 자리 글", ph=""), B.sp(B.para("기호 글머리", bullet="char"), B.para("번호 글머리",
                                                                                         bullet="auto")))
    second = B.slide(B.sp(B.para("둘째", "", "줄 바꾼 제목"), ph="ctrTitle"), B.sp("둘째 슬라이드 본문"))
    parsed, counts = parse(first, second)
    assert shown(parsed) == [
        ("heading", "지어낸 첫 제목", 1, 2), ("paragraph", "일반 글상자 글", 1, 0), ("list_item", "본문 첫 항목", 1, 1),
        ("paragraph", "글머리 없앤 문단", 1, 1), ("paragraph", "부제 문단", 1, 3), ("list_item", "종류 없는 자리 글", 1, 4),
        ("list_item", "기호 글머리", 1, 5), ("list_item", "번호 글머리", 1, 5), ("heading", "둘째 줄 바꾼 제목", 2, 0),
        ("paragraph", "둘째 슬라이드 본문", 2, 1)]
    assert [b["level"] for b in parsed.blocks if b["kind"] == "heading"] == [1, 1]
    assert [b["section_path"] for b in parsed.blocks][1] == ("지어낸 첫 제목",)
    assert isinstance(parsed.blocks[0]["locator"], SlideLocator) and parsed.regions == ()
    assert [c.ok for c in counts] == [True, True] and sum(c.rescued for c in counts) == 0


def test_footer_and_header_placeholders_give_one_block_each_with_stored_field_results():
    parsed, _ = parse(B.slide(B.sp("슬라이드 글"), B.sp(B.para(B.a_r("2026-10-16", fld="datetime1")), ph="dt"),
                              B.sp("지어낸 꼬리말", "둘째 줄", ph="ftr"), B.sp(B.para(B.a_r("7", fld="slidenum")), ph="sldNum"),
                              B.sp("지어낸 머리말", ph="hdr")))
    assert shown(parsed) == [("paragraph", "슬라이드 글", 1, 0), ("page_footer", "2026-10-16", 1, 1),
                             ("page_footer", "지어낸 꼬리말\n둘째 줄", 1, 2), ("page_footer", "7", 1, 3),
                             ("page_header", "지어낸 머리말", 1, 4)]


def test_hidden_shapes_and_groups_give_no_blocks_and_hidden_slides_keep_their_text():
    shapes = (B.sp("보이는 글"), B.sp("숨긴 도형 글", hidden=True),
              B.grp(B.sp("숨긴 그룹 안 글"), B.grp(B.sp("더 안쪽 글")), hidden=True), B.sp("뒤 글"))
    parsed, counts = parse(B.slide(*shapes), B.slide(B.sp("감춘 슬라이드 글"), show="0"))
    assert shown(parsed) == [("paragraph", "보이는 글", 1, 0), ("paragraph", "뒤 글", 1, 6),
                             ("paragraph", "감춘 슬라이드 글", 2, 0)]
    assert reasons(parsed) == [("hidden_slide", "slide2-sh0-hidden-slide-0", None),
                               ("hidden_text", "slide1-sh1-hidden-text-0", 5),
                               ("hidden_text", "slide1-sh3-hidden-text-0", 6),
                               ("hidden_text", "slide1-sh5-hidden-text-0", 4)]
    assert counts[0].ok and counts[0].hidden == 15 and counts[0].rescued == 0


def test_groups_flatten_in_pre_order_and_connectors_with_text_are_shapes():
    deep = B.sp("가장 안쪽 글")
    for _ in range(30):
        deep = B.grp(deep)
    parsed, counts = parse(B.slide(B.grp(B.sp("그룹 첫 글"), B.grp(B.sp("안 그룹 글")), B.sp("그룹 끝 글")),
                                   B.sp("연결선 글", tag="cxnSp"), deep))
    assert shown(parsed) == [("paragraph", "그룹 첫 글", 1, 1), ("paragraph", "안 그룹 글", 1, 3),
                             ("paragraph", "그룹 끝 글", 1, 4), ("paragraph", "연결선 글", 1, 5),
                             ("paragraph", "가장 안쪽 글", 1, 36)]
    assert counts[0].ok and counts[0].rescued == 0


def test_alternate_content_shapes_ignorable_elements_and_equations():
    alt = ('<mc:AlternateContent><mc:Choice Requires="x">' + B.sp("모르는 쪽 글") + "</mc:Choice><mc:Fallback>"
           + B.sp("대체 쪽 글") + "</mc:Fallback></mc:AlternateContent>")
    math = ('<a:p><a:r><a:t>수식 </a:t></a:r><mc:AlternateContent><mc:Choice Requires="a14"><a14:m '
            'xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main"><m:oMathPara><m:oMath><m:r><m:t>x=1'
            "</m:t></m:r></m:oMath></m:oMathPara></a14:m></mc:Choice><mc:Fallback><a:r><a:t>그림 대체</a:t></a:r>"
            "</mc:Fallback></mc:AlternateContent></a:p>")
    parsed, counts = parse(B.slide(alt, f"<x:future>{B.sp('무시한 도형 글')}</x:future>", B.sp(math)))
    assert shown(parsed) == [("paragraph", "대체 쪽 글", 1, 0), ("paragraph", "수식 x=1", 1, 1)]
    assert reasons(parsed) == [("equation_text", "slide1-sh1-equation-0", None),
                               ("alternate_content_skipped", "doc-alternate-content", 15)]
    assert counts[0].ok and counts[0].rescued == 0


SAMPLE = B.slide(B.sp("첫째 도형"), B.sp(B.para("둘째 앞", "둘째 뒤")))


def test_a_dropped_source_is_rescued_once_with_unassigned_text(monkeypatch):
    original = pptx._Slide.runs

    def drop_first(self, p, title=False):
        bundles, weights, lines = original(self, p, title)
        return (bundles[1:] if len(bundles) == 2 else bundles), weights, lines

    monkeypatch.setattr(pptx._Slide, "runs", drop_first)
    parsed, counts = parse(SAMPLE)
    assert shown(parsed) == [("paragraph", "첫째 도형", 1, 0), ("paragraph", "둘째 뒤", 1, 1),
                             ("paragraph", "둘째 앞", 1, 1)]
    assert parsed.blocks[-1]["confidence"] == 0.3 and counts[0].ok and counts[0].rescued == 3
    assert reasons(parsed) == [("unassigned_text", "slide1-sh1-unassigned-text-0", 3)]


def test_a_source_put_twice_is_a_coverage_mismatch(monkeypatch):
    original = pptx._Slide.runs
    monkeypatch.setattr(pptx._Slide, "runs", lambda self, p, title=False: (
        lambda out: (out[0] * 2, out[1], out[2]))(original(self, p, title)))
    parsed, counts = parse(SAMPLE)
    assert shown(parsed)[0] == ("paragraph", "첫째 도형첫째 도형", 1, 0)
    assert (counts[0].source, counts[0].in_blocks, counts[0].doubled) == (10, 20, 3)
    assert reasons(parsed)[-1][:2] == ("coverage_mismatch", "slide1-coverage")


def test_package_errors_name_the_part():
    with pytest.raises(ParseError, match="not a PPTX"):
        PptxParser().parse(b"PK\x03\x04 not a zip", "a.pptx")
    with pytest.raises(ParseError, match="legacy Word/PowerPoint"):
        PptxParser().parse(bytes.fromhex("d0cf11e0a1b11ae1") + bytes(100), "a.pptx")
    with pytest.raises(ParseError, match="unsupported OOXML document type"):
        PptxParser().parse(B.pptx(B.slide(), main_type=B.PPTX_MAIN.replace("presentation.main", "slideshow.main")),
                           "a.pptx")
    data = B.pptx(B.slide(B.sp("글")))
    broken = B.zip_bytes([(n, d.replace(b'Target="slides/slide1.xml"', b'Target="slides/none.xml"')
                           if n == "ppt/_rels/presentation.xml.rels" else d) for n, d in _members(data)])
    with pytest.raises(ParseError, match="missing zip member") as info:
        PptxParser().parse(broken, "a.pptx")
    assert info.value.location == "a.pptx:ppt/slides/none.xml"
    kinds = B.zip_bytes([(n, d.replace(b"relationships/slide\"", b"relationships/image\"")
                          if n == "ppt/_rels/presentation.xml.rels" else d) for n, d in _members(data)])
    with pytest.raises(ParseError, match="slide relationship not found"):
        PptxParser().parse(kinds, "a.pptx")
    wrong = B.zip_bytes([(n, d.replace(b"<p:sld ", b"<p:notes ").replace(b"</p:sld>", b"</p:notes>")
                          if n == "ppt/slides/slide1.xml" else d) for n, d in _members(data)])
    with pytest.raises(ParseError, match="unexpected root element") as info:
        PptxParser().parse(wrong, "a.pptx")
    assert info.value.location == "a.pptx:ppt/slides/slide1.xml"


def _members(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(BytesIO(data)) as z:
        return [(n, z.read(n)) for n in z.namelist()]


def test_detection_cli_folder_ingest_viewer_and_same_bytes(tmp_path, capsys, monkeypatch):
    assert isinstance(detect_parser("발표.PPTX", default_parsers(ocr=False, layout=False)), PptxParser)
    for name in ("a.pptx", "b.PPTX", "c.ppt"):
        (tmp_path / name).write_bytes(b"x")
    extensions = frozenset(e for p in default_parsers(ocr=False, layout=False) for e in p.extensions)
    entries, skipped = batch.expand([tmp_path], False, extensions)
    assert [Path(e.path).name for e in entries] == ["a.pptx", "b.PPTX"] and skipped == 1
    good = tmp_path / "좋은.pptx"
    good.write_bytes(B.pptx(B.slide(B.sp("지어낸 발표 제목", ph="title"), B.sp("본문 글"))))
    monkeypatch.delenv("HANJI_DB", raising=False)
    db = tmp_path / "state.db"
    assert main(["parse", str(good), "--db", str(db)]) == 0
    tree = DocumentTree.model_validate(json.loads(capsys.readouterr().out))
    assert tree.blocks[0].text == "지어낸 발표 제목" and tree.source.page_count is None
    assert tree.blocks[1].locator == SlideLocator(slide=1, shape_index=1)
    broken = tmp_path / "깨진.pptx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    assert main(["parse", str(broken), "--db", str(db)]) == 4
    assert "not a PPTX" in capsys.readouterr().err
    (raw,) = re.findall(r'<script type="application/json" id="ko-data">(.*?)</script>', render_html(tree), re.DOTALL)
    assert json.loads(raw)["blocks"][1]["where"] == "슬라이드 1"
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(good))
    assert engine.ingest(str(good)) == ref  # 같은 원본은 그대로


def test_slide_order_follows_sld_id_list_not_file_numbering():
    data = B.pptx(B.slide(B.sp("파일 첫째 슬라이드 글")), B.slide(B.sp("파일 둘째 슬라이드 글")))
    one, two = b'<p:sldId id="256" r:id="rIdS1"/>', b'<p:sldId id="257" r:id="rIdS2"/>'
    swapped = B.zip_bytes([(n, d.replace(one + two, two + one) if n == "ppt/presentation.xml" else d)
                           for n, d in _members(data)])
    assert swapped != data
    plain = PptxParser().parse(data, "a.pptx")
    parsed = PptxParser().parse(swapped, "a.pptx")
    assert shown(plain) == [("paragraph", "파일 첫째 슬라이드 글", 1, 0), ("paragraph", "파일 둘째 슬라이드 글", 2, 0)]
    assert shown(parsed) == [("paragraph", "파일 둘째 슬라이드 글", 1, 0), ("paragraph", "파일 첫째 슬라이드 글", 2, 0)]
    assert [b["locator"] for b in parsed.blocks] == [SlideLocator(slide=1, shape_index=0),
                                                     SlideLocator(slide=2, shape_index=0)]


def test_notes_layout_and_master_text_and_comments_are_counted_not_read():
    notes = ("ppt/notesSlides/notesSlide1.xml", B.notes_part("지어낸 발표자 노트", "둘째 줄"))
    old = ("ppt/comments/comment1.xml", f'{B.DECL}<p:cmLst{B.xmlns("p")}><p:cm><p:text>옛 메모 글</p:text></p:cm>'
                                        "</p:cmLst>")
    new = ("ppt/comments/modernComment1.xml", f'{B.DECL}<p188:cmLst xmlns:p188="urn:p188" {B.xmlns("a")[1:]}>'
                                              f"<p188:cm><p188:txBody>{B.para('새 메모')}</p188:txBody></p188:cm>"
                                              "</p188:cmLst>")
    links = [("rIdN", B.PREL["notesSlide"], "../notesSlides/notesSlide1.xml"),
             ("rIdC", B.PREL["comments"], "../comments/comment1.xml"),
             ("rIdM", "http://schemas.microsoft.com/office/2018/10/relationships/comments",
              "../comments/modernComment1.xml")]
    master = B.sp("마스터 안내 문구", ph="title") + B.sp("마스터 로고 글") + B.sp("숨긴 장식", hidden=True)
    layout = B.sp("레이아웃 본문 안내", ph="body") + B.grp(B.sp("레이아웃 장식 글"))
    parsed, counts = parse(B.slide(B.sp("슬라이드 글")), B.slide(B.sp("노트 없는 슬라이드")), slide_rels=[links],
                           extra=[notes, old, new], master_shapes=master, layout_shapes=layout)
    assert shown(parsed) == [("paragraph", "슬라이드 글", 1, 0), ("paragraph", "노트 없는 슬라이드", 2, 0)]
    assert reasons(parsed) == [("speaker_notes_skipped", "slide1-sh0-speaker-notes-0", 11),
                               ("master_text_skipped", "doc-master-text", 13), ("comments_skipped", "doc-comments", 7)]
    assert all(c.ok for c in counts)


def test_broken_notes_comments_and_master_parts_do_not_fail_the_document():
    links = [("rIdN", B.PREL["notesSlide"], "../notesSlides/notesSlide1.xml"),
             ("rIdC", B.PREL["comments"], "../comments/comment1.xml")]
    data = B.pptx(B.slide(B.sp("지어낸 본문 문단은 크기 규칙의 본문 크기를 정할 만큼 길다"),
                          B.sp(B.para(B.a_r("크기 사슬 글", sz=2400)), y=0)), slide_rels=[links],
                  extra=[("ppt/notesSlides/notesSlide1.xml", B.DECL + "<p:notes><broken"),
                         ("ppt/comments/comment1.xml", B.DECL + "<p:cmLst><broken")])
    data = B.zip_bytes([(n, B.DECL + "<p:sldMaster><broken" if n == "ppt/slideMasters/slideMaster1.xml" else d)
                        for n, d in _members(data)])
    parsed, counts = PptxParser()._parse(data, "a.pptx")
    assert shown(parsed)[1] == ("heading", "크기 사슬 글", 1, 1) and parsed.regions == () and counts[0].ok


def test_a_broken_master_still_lets_the_layout_text_be_counted():
    layout = B.sp("레이아웃 안내", ph="body") + B.sp("레이아웃 장식 글")
    data = B.pptx(B.slide(B.sp("슬라이드 글")), master_shapes=B.sp("마스터 로고 글"), layout_shapes=layout)
    data = B.zip_bytes([(n, B.DECL + "<p:sldMaster><broken" if n == "ppt/slideMasters/slideMaster1.xml" else d)
                        for n, d in _members(data)])
    parsed, counts = PptxParser()._parse(data, "a.pptx")
    assert shown(parsed) == [("paragraph", "슬라이드 글", 1, 0)]
    assert reasons(parsed) == [("master_text_skipped", "doc-master-text", 7)] and counts[0].ok


def test_a_broken_layout_relationship_part_does_not_fail_the_document():
    shapes = dict(master_shapes=B.sp("마스터 로고 글"), layout_shapes=B.sp("레이아웃 장식 글"))
    data = B.pptx(B.slide(B.sp("슬라이드 글")), **shapes)
    plain, _ = PptxParser()._parse(data, "a.pptx")
    twice = B.rels(("rIdM", B.PREL["slideMaster"], "../slideMasters/slideMaster1.xml"),
                   ("rIdM", B.PREL["slideMaster"], "../slideMasters/slideMaster1.xml"))
    broken = B.zip_bytes([(n, twice if n == "ppt/slideLayouts/_rels/slideLayout1.xml.rels" else d)
                          for n, d in _members(data)])
    parsed, counts = PptxParser()._parse(broken, "a.pptx")
    assert shown(parsed) == shown(plain) == [("paragraph", "슬라이드 글", 1, 0)] and counts[0].ok
    assert reasons(plain) == [("master_text_skipped", "doc-master-text", 13)]
    assert reasons(parsed) == [("master_text_skipped", "doc-master-text", 7)]  # 마스터 쪽은 닿지 못해 레이아웃 글자만


def test_a_slide_relationship_part_with_a_duplicate_id_still_fails_the_document():
    data = B.pptx(B.slide(B.sp("슬라이드 글")))
    twice = B.rels(("rIdL", B.PREL["slideLayout"], "../slideLayouts/slideLayout1.xml"),
                   ("rIdL", B.PREL["slideLayout"], "../slideLayouts/slideLayout1.xml"))
    broken = B.zip_bytes([(n, twice if n == "ppt/slides/_rels/slide1.xml.rels" else d) for n, d in _members(data)])
    with pytest.raises(ParseError, match="duplicate relationship id"):
        PptxParser()._parse(broken, "a.pptx")


def _notes_chars(notes: str) -> list[tuple]:
    links = [("rIdN", B.PREL["notesSlide"], "../notesSlides/notesSlide1.xml")]
    parsed, counts = parse(B.slide(B.sp("슬라이드 글")), slide_rels=[links],
                           extra=[("ppt/notesSlides/notesSlide1.xml", notes)])
    assert shown(parsed) == [("paragraph", "슬라이드 글", 1, 0)] and all(c.ok for c in counts)
    return reasons(parsed)


def test_speaker_notes_count_skips_the_slide_image_number_date_footer_and_header_places():
    furniture = (B.sp(ph="sldImg") + B.sp(B.para(B.a_r("1", fld="slidenum")), ph="sldNum")
                 + B.sp(B.para(B.a_r("2026-10-16", fld="datetime1")), ph="dt") + B.sp("노트 꼬리말", ph="ftr")
                 + B.sp("노트 머리말", ph="hdr"))
    assert _notes_chars(B.notes_part(shapes=furniture)) == []
    assert _notes_chars(B.notes_part("지어낸 노트", shapes=furniture + B.sp("노트 글상자"))) == [
        ("speaker_notes_skipped", "slide1-sh0-speaker-notes-0", 10)]
