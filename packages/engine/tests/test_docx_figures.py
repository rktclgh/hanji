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
