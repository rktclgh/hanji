"""DOCX 그림·글상자·묶음·VML·OLE(스펙 §7.1, §7.4). 합성 문서는 build_ooxml_fixtures.py로 만든다."""

import importlib.util
from pathlib import Path

from hanji.formats.ooxml import package as ox
from hanji.formats.ooxml.docx.parser import DocxParser
from hanji.formats.pdf import figures

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
STYLES = B.styles(B.style("Normal", "Normal", default=True), B.style("H1", "heading 1"))
PICS = {"rIdP": ("media/gray.png", B.GRAY_PNG), "rIdJ": ("media/cmyk.jpg", B.CMYK_JPEG),
        "rIdE": ("media/fake.emf", B.FAKE_EMF), "rIdS": ("media/vector.svg", b"<svg/>"),
        "rIdA": ("media/alpha.png", B.RGBA_PNG)}


def parse(*items: str, pictures: dict | None = None, rels: tuple = (), extra: tuple = ()):
    """(ParsedSource, 구역별 장부 숫자). pictures는 관계 id → (멤버, 바이트)."""
    found = B.media(**(PICS if pictures is None else pictures))
    data = B.docx(B.document(*items), styles_xml=STYLES, doc_rels=[*found["doc_rels"], *rels],
                  extra=[*found["extra"], *extra])
    return DocxParser()._parse(data, "a.docx")


def shown(parsed) -> list[tuple]:
    """(kind, 글자, 그림 화소 너비·dpi 또는 None, 닻)."""
    return [(b["kind"], b["text"], (b["figure"]["width_px"], b["figure"]["dpi"]) if "figure" in b else None,
             b["locator"].paragraph_index) for b in parsed.blocks]


def reasons(parsed) -> list[tuple[str, str]]:
    return [(r.fallback_reason, r.region_id) for r in parsed.regions]


def test_inline_and_anchored_pictures_become_png_assets_after_the_paragraph_text():
    parsed, counts = parse(B.p(B.r("그림 앞 글"), B.drawing(B.pic("rIdP"), cx=B.EMU // 12), B.r(" 뒤 글")),
                           B.p(B.drawing(B.pic("rIdJ"), anchor=True, cx=B.EMU // 4)),
                           B.p(B.drawing(B.pic("rIdP"))), B.p(B.drawing(B.pic("rIdA"), cx=0)))
    assert shown(parsed) == [("paragraph", "그림 앞 글 뒤 글", None, 0), ("figure", "", (6, 72), 0),
                             ("figure", "", (8, 32), 1), ("figure", "", (6, 6), 2), ("figure", "", (4, 96), 3)]
    keys = [b["figure"]["asset"] for b in parsed.blocks if "figure" in b]
    assert keys[0] == keys[2] and len(parsed.assets) == 3 and all(k in parsed.assets for k in keys)
    assert all(parsed.assets[k].startswith(b"\x89PNG") for k in keys) and parsed.regions == () and counts[0].ok


def test_pictures_without_images_keep_their_place_and_reason():
    rels = (("rIdX", f"{B.R}/image", "http://example.invalid/a.png", "External"),
            ("rIdM", f"{B.R}/image", "media/none.png"))
    parsed, _ = parse(B.p(B.drawing(B.pic("rIdE"))), B.p(B.drawing(B.pic(link="rIdX"))), B.p(B.drawing(B.pic("rIdX"))),
                      B.p(B.drawing(B.pic("rIdQ"))), B.p(B.drawing(B.pic("rIdM"))), B.p(B.drawing(B.pic())),
                      B.p(B.drawing(B.pic("rIdP", svg="rIdS"))), rels=rels)
    assert [(b["kind"], "figure" in b) for b in parsed.blocks] == [("figure", False)] * 6 + [("figure", True)]
    assert reasons(parsed) == [
        ("figure_image_unsupported", "s0-p0-figure-0"), ("figure_image_linked", "s0-p1-figure-0"),
        ("figure_image_linked", "s0-p2-figure-0"), ("figure_image_missing", "s0-p3-figure-0"),
        ("figure_image_missing", "s0-p4-figure-0"), ("figure_image_missing", "s0-p5-figure-0")]


def test_size_limits_and_the_asset_budget_leave_figures_without_images(monkeypatch):
    monkeypatch.setattr(ox, "MAX_BINARY_BYTES", 100)
    parsed, _ = parse(B.p(B.drawing(B.pic("rIdJ"))), B.p(B.drawing(B.pic("rIdP"))))
    assert reasons(parsed) == [("figure_image_too_large", "s0-p0-figure-0")] and "figure" in parsed.blocks[1]
    monkeypatch.setattr(figures, "ASSET_LIMIT", 10)
    parsed, _ = parse(B.p(B.drawing(B.pic("rIdP"))), B.p(B.drawing(B.pic("rIdP"))))
    assert reasons(parsed) == [("figure_image_budget", "s0-p0-figure-0"), ("figure_image_budget", "s0-p1-figure-0")]
    assert parsed.assets == {}


def test_text_boxes_groups_and_one_branch_of_alternate_content():
    """글상자 문단은 판정 없는 문단(제목 스타일도), 안 표는 표 블록. 묶음은 자식 차례. mc 짝은 한쪽만."""
    box = B.wsp(B.p("글상자 제목", style="H1"), B.tbl(B.tr(B.tc("가"), B.tc("나")), B.tr(B.tc("다"), B.tc("라"))),
                B.p("글상자 끝"))
    group = B.drawing(B.wgp(B.pic("rIdP", cx=B.EMU // 12), B.wsp("묶음 속 글"), B.wgp(B.wsp("안 묶음 글"))))
    alternate = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps">' + B.drawing(B.wsp("새 형식 글"))[5:-6]
                 + "</mc:Choice><mc:Fallback>" + B.vpict(B.vshape(None, "옛 형식 글"))[5:-6]
                 + "</mc:Fallback></mc:AlternateContent></w:r>")
    parsed, counts = parse(B.p(B.r("본문"), B.drawing(box)), B.p(group), B.p(alternate), B.p(B.drawing(B.wsp(""))))
    assert [(k, t, a) for k, t, _, a in shown(parsed)] == [
        ("paragraph", "본문", 0), ("paragraph", "글상자 제목", 0), ("table", "가\t나\n다\t라", 0),
        ("paragraph", "글상자 끝", 0), ("figure", "", 1), ("paragraph", "묶음 속 글", 1), ("paragraph", "안 묶음 글", 1),
        ("paragraph", "새 형식 글", 2)]
    assert reasons(parsed) == [("shape_text", "s0-p0-shape-0"), ("shape_text", "s0-p1-shape-0"),
                               ("shape_text", "s0-p1-shape-1"), ("shape_text", "s0-p2-shape-0"),
                               ("alternate_content_skipped", "doc-alternate-content")]
    assert counts[0].ok and counts[0].rescued == 0


def test_vml_pictures_text_boxes_groups_and_ole_previews():
    parsed, counts = parse(
        B.p(B.vpict(B.vshape("rIdP"), '<v:group>' + B.vshape(None, "VML 글상자") + B.vshape("rIdE") + "</v:group>")),
        B.p(B.vpict('<v:shapetype id="t1"/>', B.vshape("rIdJ"), ole=True)), B.p(B.vpict(ole=True)),
        B.p(B.vpict('<v:rect style="width:1pt"/>')))
    assert [(k, t, f is not None, a) for k, t, f, a in shown(parsed)] == [
        ("figure", "", True, 0), ("paragraph", "VML 글상자", False, 0), ("figure", "", False, 0),
        ("figure", "", True, 1), ("figure", "", False, 2)]
    assert parsed.blocks[0]["figure"]["dpi"] == 96
    assert reasons(parsed) == [("shape_text", "s0-p0-shape-0"), ("figure_image_unsupported", "s0-p0-figure-0"),
                               ("embedded_object", "s0-p1-object-0"), ("embedded_object", "s0-p2-object-0")]
    assert counts[0].ok


def test_objects_in_table_cells_come_after_the_table_block():
    cell = B.p(B.r("칸 글"), B.drawing(B.pic("rIdP")), B.drawing(B.wsp("칸 속 글상자")))
    parsed, counts = parse(B.p("앞"), B.tbl(B.tr(B.tc(cell), B.tc("옆 칸")), B.tr(B.tc("아래"), B.tc("끝"))), B.p("뒤"))
    assert [(k, t) for k, t, _, _ in shown(parsed)] == [
        ("paragraph", "앞"), ("table", "칸 글\t옆 칸\n아래\t끝"), ("figure", ""), ("paragraph", "칸 속 글상자"),
        ("paragraph", "뒤")]
    assert counts[0].ok and reasons(parsed) == [("shape_text", "s0-p1-shape-0")]


def test_hidden_frames_give_no_blocks_and_their_text_is_hidden():
    parsed, counts = parse(B.p(B.r("보임"), B.drawing(B.wsp("숨긴 글상자"), hidden=True),
                               B.drawing(B.pic("rIdP"), hidden=True)))
    assert shown(parsed) == [("paragraph", "보임", None, 0)]
    assert [(r.fallback_reason, r.gate.checks[0].value) for r in parsed.regions] == [("hidden_text", 5)]
    assert counts[0].ok and counts[0].hidden == 5


def test_unknown_graphic_data_is_a_figure_without_image():
    parsed, counts = parse(B.p(B.drawing("<x:future/>", uri="urn:hanji:test:future")))
    assert shown(parsed) == [("figure", "", None, 0)]
    assert reasons(parsed) == [("embedded_object", "s0-p0-object-0")] and counts[0].ok


def test_a_picture_member_with_a_broken_zip_crc_loses_only_that_picture():
    found = B.media(**PICS)
    data = bytearray(B.docx(B.document(B.p("앞 글"), B.p(B.drawing(B.pic("rIdP"))), B.p(B.drawing(B.pic("rIdJ"))),
                                       B.p("뒤 글")),
                            styles_xml=STYLES, doc_rels=found["doc_rels"], extra=found["extra"]))
    assert bytes(data).count(B.GRAY_PNG) == 1  # ZIP_STORED: 멤버 바이트가 그대로 한 번 들어 있다
    data[bytes(data).index(B.GRAY_PNG) + len(B.GRAY_PNG) // 2] ^= 0xFF  # 저장된 데이터 한 바이트를 깨 CRC를 어긋나게
    parsed, counts = DocxParser()._parse(bytes(data), "a.docx")
    assert [(b["kind"], b["text"], "figure" in b) for b in parsed.blocks] == [
        ("paragraph", "앞 글", False), ("figure", "", False), ("figure", "", True), ("paragraph", "뒤 글", False)]
    assert reasons(parsed) == [("figure_image_unreadable", "s0-p1-figure-0")] and counts[0].ok


CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DIAGRAM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


def test_charts_give_a_figure_and_their_rich_text_once_per_host_and_count_cache_text():
    chart = B.chart_part("지어낸 매출 추이", "단위: 억원", cache=("1분기", "2분기", "10.5"))
    rels = (("rIdC", f"{B.R}/chart", "charts/chart1.xml"), ("rIdN", f"{B.R}/chart", "charts/none.xml"))
    parsed, counts = parse(B.p(B.r("차트 앞"), B.drawing(B.chart_ref("rIdC"), uri=CHART_URI)),
                           B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI)),
                           B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI, hidden=True)),
                           B.p(B.drawing(B.chart_ref("rIdN"), uri=CHART_URI)),
                           rels=rels, extra=(("word/charts/chart1.xml", chart),))
    assert [(k, t, a) for k, t, _, a in shown(parsed)] == [
        ("paragraph", "차트 앞", 0), ("figure", "", 0), ("paragraph", "지어낸 매출 추이\n단위: 억원", 0),
        ("figure", "", 1), ("paragraph", "지어낸 매출 추이\n단위: 억원", 1), ("figure", "", 3)]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value if r.gate else None) for r in parsed.regions] == [
        ("embedded_object", "s0-p0-object-0", None), ("chart_text", "s0-p0-chart-0", 10),
        ("embedded_object", "s0-p1-object-0", None), ("chart_text", "s0-p1-chart-0", 10),
        ("embedded_object", "s0-p3-object-0", None), ("hidden_text", "s0-p2-hidden-text-0", 7),
        ("hidden_text", "s0-p2-hidden-text-1", 5)]
    assert counts[0].ok and counts[0].hidden == 12 and counts[0].rescued == 0


def test_hidden_charts_in_different_paragraphs_each_get_their_own_hidden_text_record():
    chart = B.chart_part("숨은 차트 제목")
    rels = (("rIdC", f"{B.R}/chart", "charts/chart1.xml"),)
    parsed, counts = parse(B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI, hidden=True)),
                           B.p(B.r("보통 글")),
                           B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI, hidden=True)),
                           rels=rels, extra=(("word/charts/chart1.xml", chart),))
    assert [(k, t) for k, t, _, _ in shown(parsed)] == [("paragraph", "보통 글")]
    assert [(r.fallback_reason, r.region_id, r.gate.checks[0].value) for r in parsed.regions] == [
        ("hidden_text", "s0-p0-hidden-text-0", 6), ("hidden_text", "s0-p2-hidden-text-0", 6)]
    assert counts[0].ok and counts[0].hidden == 12 and counts[0].rescued == 0


def test_smartart_reads_the_drawn_part_found_through_the_host_and_leaves_the_data_part_out():
    data = B.smartart_data("데이터 글 하나", "데이터 글 둘", drawn="rIdDr")
    drawn = B.smartart_drawn(["그린 첫 도형"], ["그린 둘째 도형", "둘째 문단"])
    rels = (("rIdDm", f"{B.R}/diagramData", "diagrams/data1.xml"),
            ("rIdDr", "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing", "diagrams/drawing1.xml"),
            ("rIdD2", f"{B.R}/diagramData", "diagrams/data2.xml"))
    parsed, counts = parse(B.p(B.drawing(B.smartart_ref("rIdDm"), uri=DIAGRAM_URI)),
                           B.p(B.drawing(B.smartart_ref("rIdD2"), uri=DIAGRAM_URI)),
                           B.p(B.drawing(B.smartart_ref("rIdNo"), uri=DIAGRAM_URI)), rels=rels,
                           extra=(("word/diagrams/data1.xml", data), ("word/diagrams/drawing1.xml", drawn),
                                  ("word/diagrams/data2.xml", B.smartart_data("점 글", ""))))
    assert [(k, t, a) for k, t, _, a in shown(parsed)] == [
        ("paragraph", "그린 첫 도형", 0), ("paragraph", "그린 둘째 도형", 0), ("paragraph", "둘째 문단", 0),
        ("paragraph", "점 글", 1), ("figure", "", 2)]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("smartart_text", "s0-p0-smartart-0"), ("smartart_text", "s0-p0-smartart-1"),
        ("smartart_text", "s0-p1-smartart-0"), ("embedded_object", "s0-p2-object-0"),
        ("alternate_content_skipped", "doc-alternate-content")]
    assert parsed.regions[-1].gate.checks[0].value == 11 and counts[0].ok and counts[0].rescued == 0


def test_a_hidden_smartart_counts_only_the_drawn_part_as_hidden_and_the_data_part_as_out_of_range():
    data = B.smartart_data("데이터 글", drawn="rIdDr")
    drawn = B.smartart_drawn(["그린 글"])
    rels = (("rIdDm", f"{B.R}/diagramData", "diagrams/data1.xml"),
            ("rIdDr", "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing", "diagrams/drawing1.xml"))
    parsed, counts = parse(B.p(B.drawing(B.smartart_ref("rIdDm"), uri=DIAGRAM_URI, hidden=True)), rels=rels,
                           extra=(("word/diagrams/data1.xml", data), ("word/diagrams/drawing1.xml", drawn)))
    assert parsed.blocks == ()
    assert [(r.fallback_reason, r.gate.checks[0].value) for r in parsed.regions] == [
        ("hidden_text", 3), ("alternate_content_skipped", 4)]
    assert counts[0].ok and counts[0].source == 7 and counts[0].in_blocks == 0 and counts[0].rescued == 0


def test_a_relationship_of_the_wrong_kind_leaves_a_figure_without_text_and_does_not_fail_the_document():
    data = B.smartart_data("데이터 글", drawn="rIdDr")
    rels = (("rIdDm", f"{B.R}/diagramData", "diagrams/data1.xml"),
            ("rIdDr", f"{B.R}/chart", "diagrams/data1.xml"))  # 그려진 파트 자리에 차트 관계
    parsed, counts = parse(B.p(B.drawing(B.chart_ref("rIdDm"), uri=CHART_URI)),
                           B.p(B.drawing(B.smartart_ref("rIdDr"), uri=DIAGRAM_URI)),
                           B.p(B.drawing(B.smartart_ref("rIdDm"), uri=DIAGRAM_URI)), rels=rels,
                           extra=(("word/diagrams/data1.xml", data),))
    assert [(k, t, a) for k, t, _, a in shown(parsed)] == [("figure", "", 0), ("figure", "", 1),
                                                          ("paragraph", "데이터 글", 2)]
    assert [(r.fallback_reason, r.region_id) for r in parsed.regions] == [
        ("embedded_object", "s0-p0-object-0"), ("embedded_object", "s0-p1-object-0"),
        ("smartart_text", "s0-p2-smartart-0")]
    assert counts[0].ok and counts[0].rescued == 0


def test_only_reference_elements_are_recorded_as_graphics(monkeypatch):
    """차트 파트 안 c:chart(참조가 아닌 요소)는 기록하지 않는다: 차트 하나면 기록 하나."""
    seen = []
    original = DocxParser._graphics

    def spy(doc, ledgers):
        seen.append(len(doc.walker.graphics))
        original(doc, ledgers)
        seen.append(len(doc.walker.graphics))

    monkeypatch.setattr(DocxParser, "_graphics", staticmethod(spy))
    parse(B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI)), rels=(("rIdC", f"{B.R}/chart", "charts/chart1.xml"),),
          extra=(("word/charts/chart1.xml", B.chart_part("제목")),))
    assert seen == [1, 1]


def test_broken_chart_and_smartart_parts_leave_figures_and_do_not_fail_the_document():
    """깨진 차트 파트·뿌리가 다른 차트 파트 → 이미지 없는 그림만, 깨진 그려진 파트 → 데이터 파트를 읽는다(스펙 §16-1)."""
    rels = (("rIdC1", f"{B.R}/chart", "charts/chart1.xml"), ("rIdC2", f"{B.R}/chart", "charts/chart2.xml"),
            ("rIdDm", f"{B.R}/diagramData", "diagrams/data1.xml"),
            ("rIdDr", "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing", "diagrams/drawing1.xml"))
    parsed, counts = parse(B.p(B.drawing(B.chart_ref("rIdC1"), uri=CHART_URI)),
                           B.p(B.drawing(B.chart_ref("rIdC2"), uri=CHART_URI)),
                           B.p(B.drawing(B.smartart_ref("rIdDm"), uri=DIAGRAM_URI)), rels=rels,
                           extra=(("word/charts/chart1.xml", B.DECL + "<c:chartSpace><broken"),
                                  ("word/charts/chart2.xml", B.smartart_data("뿌리 다른 글")),
                                  ("word/diagrams/data1.xml", B.smartart_data("데이터 쪽 글", drawn="rIdDr")),
                                  ("word/diagrams/drawing1.xml", B.DECL + "<dsp:drawing><broken")))
    assert [(k, t, a) for k, t, _, a in shown(parsed)] == [("figure", "", 0), ("figure", "", 1),
                                                          ("paragraph", "데이터 쪽 글", 2)]
    assert [r.fallback_reason for r in parsed.regions] == ["embedded_object", "embedded_object", "smartart_text"]
    assert counts[0].ok and counts[0].rescued == 0


def test_chart_value_count_includes_literals_multi_level_caches_and_series_names():
    """cache_chars는 차트 파트의 c:v 전부다: 캐시·리터럴·다단 범주·계열 이름(스펙 §16-7)."""
    extra_values = ('<c:ser><c:tx><c:v>계열명</c:v></c:tx><c:val><c:numLit><c:pt idx="0"><c:v>7.5</c:v></c:pt>'
                    '</c:numLit></c:val><c:cat><c:multiLvlStrRef><c:multiLvlStrCache><c:lvl><c:pt idx="0"><c:v>상위</c:v>'
                    "</c:pt></c:lvl></c:multiLvlStrCache></c:multiLvlStrRef></c:cat></c:ser>")
    chart = B.chart_part("값 세는 차트", cache=("1분기",)).replace("</c:barChart>", f"{extra_values}</c:barChart>")
    parsed, _ = parse(B.p(B.drawing(B.chart_ref("rIdC"), uri=CHART_URI)),
                      rels=(("rIdC", f"{B.R}/chart", "charts/chart1.xml"),), extra=(("word/charts/chart1.xml", chart),))
    assert [(r.fallback_reason, r.gate.checks[0].value if r.gate else None) for r in parsed.regions] == [
        ("embedded_object", None), ("chart_text", 3 + 3 + 3 + 2)]
