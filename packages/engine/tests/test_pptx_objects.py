"""PPTX 그림·동영상·차트·SmartArt·OLE·그 밖 개체(스펙 §8.4, §9). 합성 문서는 build_ooxml_fixtures.py로 만든다."""

import importlib.util
from pathlib import Path

from hanji.formats.ooxml.pptx.parser import PptxParser

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
MEDIA = {"rIdP": ("media/gray.png", B.GRAY_PNG), "rIdJ": ("media/cmyk.jpg", B.CMYK_JPEG),
         "rIdE": ("media/fake.emf", B.FAKE_EMF), "rIdS": ("media/vector.svg", b"<svg/>")}
DRAWN = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"


def parse(*slides: str, rels: tuple = (), extra: tuple = ()):
    """슬라이드마다 같은 관계(그림 MEDIA + rels)를 단 PPTX. (ParsedSource, 장부 숫자)."""
    links = [(rid, B.PREL["image"], f"../{member}") for rid, (member, _) in MEDIA.items()] + [*rels]
    data = B.pptx(*slides, slide_rels=[links] * len(slides),
                  extra=[*((f"ppt/{member}", raw) for member, raw in MEDIA.values()), *extra])
    return PptxParser()._parse(data, "a.pptx")


def shown(parsed) -> list[tuple]:
    """(kind, 글자, 그림 화소 너비·dpi 또는 None, 슬라이드, 도형 순번)."""
    return [(b["kind"], b["text"], (b["figure"]["width_px"], b["figure"]["dpi"]) if "figure" in b else None,
             b["locator"].slide, b["locator"].shape_index) for b in parsed.blocks]


def reasons(parsed) -> list[tuple]:
    return [(r.fallback_reason, r.region_id, r.gate.checks[0].value if r.gate else None) for r in parsed.regions]


def test_pictures_media_and_reasons():
    parsed, counts = parse(B.slide(
        B.ppic("rIdP", cx=B.EMU // 12), B.ppic("rIdJ", cx=B.EMU // 4), B.ppic("rIdE"), B.ppic(link="rIdP"),
        B.ppic("rIdNo"), B.ppic("rIdP", svg="rIdS", cx=0), B.ppic("rIdP", video=True), B.grp(B.ppic("rIdJ")),
        B.ppic("rIdP", hidden=True)))
    assert shown(parsed) == [
        ("figure", "", (6, 72), 1, 0), ("figure", "", (8, 32), 1, 1), ("figure", "", None, 1, 2),
        ("figure", "", None, 1, 3), ("figure", "", None, 1, 4), ("figure", "", (6, 96), 1, 5),
        ("figure", "", (6, 6), 1, 6), ("figure", "", (8, 8), 1, 8)]
    assert reasons(parsed) == [
        ("figure_image_unsupported", "slide1-sh2-figure-0", None), ("figure_image_linked", "slide1-sh3-figure-0", None),
        ("figure_image_missing", "slide1-sh4-figure-0", None), ("embedded_object", "slide1-sh6-object-0", None)]
    assert len(parsed.assets) == 2 and counts[0].ok


def test_charts_give_a_figure_and_rich_text_per_host_and_count_values():
    chart = B.chart_part("지어낸 발표 차트", "단위: 건", cache=("상반기", "12"))
    rels = (("rIdC", B.PREL["chart"], "../charts/chart1.xml"), ("rIdN", B.PREL["chart"], "../charts/none.xml"))
    frame = B.gframe(B.chart_ref("rIdC"), B.CHART_URI)
    parsed, counts = parse(B.slide(frame, B.gframe(B.chart_ref("rIdC"), B.CHART_URI, hidden=True)),
                           B.slide(B.sp("둘째 슬라이드"), frame, B.gframe(B.chart_ref("rIdN"), B.CHART_URI)),
                           rels=rels, extra=(("ppt/charts/chart1.xml", chart),))
    assert [(k, t, s, i) for k, t, _, s, i in shown(parsed)] == [
        ("figure", "", 1, 0), ("paragraph", "지어낸 발표 차트\n단위: 건", 1, 0), ("paragraph", "둘째 슬라이드", 2, 0),
        ("figure", "", 2, 1), ("paragraph", "지어낸 발표 차트\n단위: 건", 2, 1), ("figure", "", 2, 2)]
    assert reasons(parsed) == [
        ("embedded_object", "slide1-sh0-object-0", None), ("chart_text", "slide1-sh0-chart-0", 5),
        ("embedded_object", "slide2-sh1-object-0", None), ("chart_text", "slide2-sh1-chart-0", 5),
        ("embedded_object", "slide2-sh2-object-0", None), ("hidden_text", "slide1-sh1-hidden-text-0", 7),
        ("hidden_text", "slide1-sh1-hidden-text-1", 4)]
    assert all(c.ok and c.rescued == 0 for c in counts) and counts[0].hidden == 11


def test_smartart_drawn_part_through_the_slide_relationships_data_only_and_unresolved():
    rels = (("rIdDm", B.PREL["diagramData"], "../diagrams/data1.xml"), ("rIdDr", DRAWN, "../diagrams/drawing1.xml"),
            ("rIdD2", B.PREL["diagramData"], "../diagrams/data2.xml"))
    extra = (("ppt/diagrams/data1.xml", B.smartart_data("데이터 쪽 글", drawn="rIdDr")),
             ("ppt/diagrams/drawing1.xml", B.smartart_drawn(["그린 첫 도형"], ["그린 둘째", "둘째 문단"])),
             ("ppt/diagrams/data2.xml", B.smartart_data("데이터만 있는 글")))
    parsed, counts = parse(B.slide(B.gframe(B.smartart_ref("rIdDm"), B.DIAGRAM_URI),
                                   B.gframe(B.smartart_ref("rIdD2"), B.DIAGRAM_URI),
                                   B.gframe(B.smartart_ref("rIdNo"), B.DIAGRAM_URI)), rels=rels, extra=extra)
    assert [(k, t, i) for k, t, _, _, i in shown(parsed)] == [
        ("paragraph", "그린 첫 도형", 0), ("paragraph", "그린 둘째", 0), ("paragraph", "둘째 문단", 0),
        ("paragraph", "데이터만 있는 글", 1), ("figure", "", 2)]
    assert reasons(parsed) == [
        ("smartart_text", "slide1-sh0-smartart-0", None), ("smartart_text", "slide1-sh0-smartart-1", None),
        ("smartart_text", "slide1-sh1-smartart-0", None), ("embedded_object", "slide1-sh2-object-0", None),
        ("alternate_content_skipped", "doc-alternate-content", 5)]
    assert counts[0].ok and counts[0].rescued == 0


def test_ole_previews_and_unknown_graphic_data():
    parsed, counts = parse(B.slide(B.ole(B.ppic("rIdP")), B.ole(B.ppic("rIdP"), alternate=True), B.ole(None),
                                   B.gframe("<x:thing/>", "urn:hanji:test:thing")))
    assert [(k, f, i) for k, _, f, _, i in shown(parsed)] == [
        ("figure", (6, 6), 0), ("figure", None, 1), ("figure", None, 2), ("figure", None, 3)]
    assert reasons(parsed) == [
        ("embedded_object", "slide1-sh0-object-0", None), ("embedded_object", "slide1-sh1-object-0", None),
        ("embedded_object", "slide1-sh2-object-0", None), ("embedded_object", "slide1-sh3-object-0", None)]
    assert counts[0].ok
