"""DOCX 스타일 사슬·개요 수준·제목 이름·목록·런 속성(스펙 §5.3, §5.6). 합성 XML은 build_ooxml_fixtures.py로 만든다."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

from hanji.formats.ooxml.docx.styles import read_styles

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


def para(xml: str) -> ET.Element:
    """문단 XML 하나 → 요소(w 접두어 선언을 붙인다)."""
    return ET.fromstring(xml.replace("<w:p>", f'<w:p xmlns:w="{B.W}">', 1))


def load(*items: str, defaults: str = "", nums: tuple[str, ...] = ()):
    return read_styles(ET.fromstring(B.styles(*items, defaults=defaults)),
                       ET.fromstring(B.numbering(*nums)) if nums else None)


def outline(level: str) -> str:
    return f'<w:pPr><w:outlineLvl w:val="{level}"/></w:pPr>'


def test_based_on_chains_stop_at_64_and_cut_cycles():
    chain = [B.style(f"s{k}", f"s{k}", based=f"s{k + 1}") for k in range(70)]
    styles = load(*chain, B.style("a", "a", based="b"), B.style("b", "b", based="a"))
    assert len(styles.chain("s0")) == 64 and [s.name for s in styles.chain("a")] == ["a", "b"]
    assert styles.chain("missing") == [] and styles.chain(None) == []


def test_outline_level_direct_then_style_chain_and_level_nine_is_body_text():
    styles = load(B.style("H", "custom", ppr_xml=outline("1")), B.style("Child", "child", based="H"),
                  B.style("Body9", "heading 2", ppr_xml=outline("9")), B.style("Bad", "bad", ppr_xml=outline("x")))
    assert styles.heading(para(B.p("가", style="Child"))) == 2
    assert styles.heading(para(B.p("가", style="Child", outline=0))) == 1
    assert styles.heading(para(B.p("가", outline=8))) == 6
    assert styles.heading(para(B.p("가", style="Body9"))) is None  # 9가 있으면 이름 규칙으로 가지 않는다
    assert styles.heading(para(B.p("가", style="Bad"))) is None and styles.heading(para(B.p("가", outline=-1))) is None


def test_heading_and_title_style_names_without_outline_levels():
    styles = load(B.style("h3", "Heading 3"), B.style("h9", "heading 9"), B.style("t", "Title"),
                  B.style("toc", "toc 1"), B.style("mine", "mine", based="h3"))
    got = [styles.heading(para(B.p("가", style=k))) for k in ("h3", "h9", "t", "toc", "mine")]
    assert got == [3, 6, 1, None, 3]


def test_the_default_paragraph_style_applies_without_a_pstyle():
    styles = load(B.style("Normal", "Normal", default=True, rpr_xml=B.rpr(sz=22)), B.style("Other", "Other"))
    assert styles.default_paragraph == "Normal"
    assert styles.run(para(B.p("가")), None) == (11.0, False, False)


def test_lists_need_a_nonzero_num_id_known_to_numbering():
    styles = load(B.style("L", "list", ppr_xml='<w:pPr><w:numPr><w:numId w:val="2"/></w:numPr></w:pPr>'),
                  nums=("1", "2"))
    assert [styles.listed(para(x)) for x in (B.p("가", num="1"), B.p("가", style="L"), B.p("가", num="0"),
                                             B.p("가", num="7"), B.p("가", style="L", num="0"), B.p("가"))] == [
        True, True, False, False, False, False]


def test_run_size_bold_and_vanish_follow_run_then_styles_then_defaults():
    styles = load(B.style("P", "p", rpr_xml=B.rpr(sz=28, vanish="")),
                  B.style("C", "c", kind="character", rpr_xml=B.rpr(sz=40, b="")),
                  B.style("C2", "c2", kind="character", based="C", rpr_xml=B.rpr(b="false")),
                  defaults=B.rpr(sz=18))
    p = para(B.p(B.r("가", style="C2", sz=50), B.r("나", style="C2"), B.r("다", b="off", vanish="0"), B.r("라"),
                 style="P"))
    runs = p.findall(f"{{{B.W}}}r")
    assert [styles.run(p, r) for r in runs] == [(25.0, False, True), (20.0, False, True), (14.0, False, False),
                                                (14.0, False, True)]
    plain = para(B.p(B.r("마"), B.r("바", sz=0)))
    assert [styles.run(plain, r) for r in plain.findall(f"{{{B.W}}}r")] == [(9.0, False, False), (9.0, False, False)]


def test_spec_vanish_is_not_hidden_and_missing_parts_give_defaults():
    p = para(B.p(f'<w:r><w:rPr><w:specVanish/></w:rPr>{B.t("가")}</w:r>'))
    empty = read_styles(None, None)
    assert empty.run(p, p.find(f"{{{B.W}}}r")) == (10.0, False, False)
    assert empty.heading(p) is None and not empty.listed(para(B.p("가", num="1")))


def test_an_unknown_pstyle_falls_back_to_the_default_paragraph_style():
    styles = load(B.style("Normal", "Normal", default=True, rpr_xml=B.rpr(sz=24),
                          ppr_xml='<w:pPr><w:numPr><w:numId w:val="2"/></w:numPr></w:pPr>'), nums=("2",))
    p = para(B.p("가", style="NoSuchStyle"))
    assert [s.name for s in styles.paragraph_chain(p)] == ["Normal"]
    assert styles.run(p, None)[0] == 12.0 and styles.listed(p)
