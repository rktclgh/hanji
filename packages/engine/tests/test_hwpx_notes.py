"""HWPX 머리말·꼬리말·각주·미주(스펙 §8)와 글상자·도형·수식(§9): 컨트롤 안에서도 보통 개체 순회를 한다."""

import importlib.util
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwpx.parser import HwpxParser
from hanji_contracts import DocumentTree

HERE = Path(__file__).resolve().parent
ROOT = HERE / "fixtures" / "hwpx"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


def p(text: str, *objects: str) -> str:
    return B.para(B.run(B.t(text), *objects))


def test_notes_golden_margins_notes_shapes_and_equation():
    tree = DocumentTree.model_validate_json((ROOT / "expected" / "notes.json").read_bytes())
    assert [(b.kind, b.text, b.locator.paragraph_index) for b in tree.blocks] == [
        ("paragraph", "머리말을 둔 첫 문단이다.", 0), ("page_header", "지어낸 머리말", 0),
        ("page_footer", "지어낸 꼬리말", 0), ("figure", "", 0),
        ("paragraph", "각주가 달린 문단 이어지는 글", 1),
        ("table", "각주 든 칸\t둘째 칸\n셋째 칸\t칸 속 글상자", 2),
        ("paragraph", "본문 글상자 첫째", 3), ("paragraph", "본문 글상자 둘째", 3), ("paragraph", "묶음 속 글상자", 4),
        ("paragraph", "x = {-b +- sqrt {b^2 - 4ac}} over {2a}", 5), ("paragraph", "미주가 달린 문단", 6),
        ("paragraph", "숨은 설명이 있는 문단", 7),
        ("paragraph", "첫째 각주 글", 1), ("paragraph", "칸 속 각주", 2), ("paragraph", "첫째 미주 글", 6),
        ("paragraph", "둘째 구역 문단", 8), ("paragraph", "둘째 구역 끝 문단이다.", 9),
        ("paragraph", "둘째 구역 각주", 8)]
    assert tree.blocks[3].figure is not None  # 꼬리말 안 그림
    history = (ROOT / "expected" / "notes.history.json").read_text(encoding="utf-8")
    for reason in ("hidden_comment", "shape_text", "equation_script"):
        assert reason in history
    assert "coverage_mismatch" not in history and "unassigned_text" not in history


def test_the_notes_sample_ledger_counts_the_hidden_comment_and_rescues_nothing():
    _, counts = HwpxParser()._parse(B.SAMPLES["notes.hwpx"], "notes.hwpx")
    assert [c.ok for c in counts] == [True, True] and counts[0].hidden == 5 and counts[0].rescued == 0


def test_headers_for_different_pages_are_separate_blocks_and_page_numbers_are_not_text():
    page_number = '<hp:pageNum pos="BOTTOM_CENTER" formatType="DIGIT" sideChar="-"/>'
    s = B.section(B.para(B.run(B.ctrl(B.margin("header", p("홀수 쪽 머리말"), apply="ODD"),
                                      B.margin("header", p("짝수 쪽 머리말"), apply="EVEN"), page_number))))
    parsed, (count,) = HwpxParser()._parse(B.package(s), "a.hwpx")
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [("page_header", "홀수 쪽 머리말"),
                                                                ("page_header", "짝수 쪽 머리말")]
    assert count.ok


def test_shapes_without_text_and_empty_equations_make_nothing():
    s = B.section(B.para(B.run(B.line(), '<hp:rect id="9"/>', B.rect(B.para()), B.equation(" "))))
    parsed, _ = HwpxParser()._parse(B.package(s), "a.hwpx")
    assert parsed.blocks == () and parsed.regions == ()


def test_boxes_inside_notes_headers_and_text_boxes_do_not_change_body_headings():
    """각주·머리말·글상자 안 1×1 상자의 개요 문단·큰 글자 문단은 본문 제목 판정(개요 수준 사용 여부·본문 크기)에 들지
    않는다: 상자를 더해도 본문 제목과 경로가 같다."""
    head = B.header(headings={1: ("OUTLINE", 0)}, chars={1: (1600, False)})
    box = B.tbl(1, 1, [B.tc(0, 0, B.para(B.run(B.t("상자 속 개요")), pr=1),
                             B.para(B.run(B.t("상자 속 큰 글자"), char=1), lines=1))])
    title = B.para(B.run(B.t("큰 글자 제목"), char=1), lines=1)
    body = B.para(B.run(B.t("본문 크기 문단이 가장 많은 글자를 가진다. 지어낸 문장이다.")), lines=1)

    def headings(*extra: str):
        parsed, (count,) = HwpxParser()._parse(B.package(B.section(title, body, *extra), head=head), "a.hwpx")
        assert count.ok
        return ([(b["kind"], b.get("level"), b["text"]) for b in parsed.blocks if b["kind"] == "heading"],
                parsed.blocks[1]["section_path"], [b["text"] for b in parsed.blocks[2:]])

    plain = headings()
    assert plain == ([("heading", 1, "큰 글자 제목")], ("큰 글자 제목",), [])
    boxed = headings(B.para(B.run(B.t("닻"), B.ctrl(B.note("footNote", B.para(B.run(box)))),
                                  B.ctrl(B.margin("header", B.para(B.run(box)))), B.rect(B.para(B.run(box))))))
    assert boxed[:2] == plain[:2]
    assert boxed[2] == ["닻", "상자 속 개요", "상자 속 큰 글자", "상자 속 개요", "상자 속 큰 글자", "상자 속 개요",
                        "상자 속 큰 글자"]


def test_text_boxes_nested_up_to_the_depth_cap_do_not_hit_the_recursion_limit():
    """글상자 조립도 중첩만큼 재귀한다: XML 깊이 상한(256) 안의 48겹은 돌고, 넘으면 ParseError."""
    def nest(levels: int) -> str:
        para = B.para(B.run(B.t("가장 안 글상자")))
        for _ in range(levels):
            para = B.para(B.run(B.rect(para)))
        return para

    parsed, (count,) = HwpxParser()._parse(B.package(B.section(nest(48))), "a.hwpx")
    assert [b["text"] for b in parsed.blocks] == ["가장 안 글상자"] and count.ok
    assert [r.fallback_reason for r in parsed.regions] == ["shape_text"] * 48
    with pytest.raises(ParseError, match="deeper than 256"):
        HwpxParser().parse(B.package(B.section(nest(52))), "a.hwpx")


def test_objects_inside_notes_and_text_boxes_become_blocks_too():
    note = B.ctrl(B.note("footNote", B.para(B.run(B.t("표가 든 각주"),
                                                    B.tbl(2, 1, [B.tc(0, 0, p("가"))], [B.tc(1, 0, p("나"))])))))
    box = B.rect(B.para(B.run(B.t("글상자 안"), B.equation("a over b"))))
    parsed, (count,) = HwpxParser()._parse(B.package(B.section(B.para(B.run(B.t("본문"), note, box)))), "a.hwpx")
    assert [(b["kind"], b["text"]) for b in parsed.blocks] == [
        ("paragraph", "본문"), ("paragraph", "글상자 안"), ("paragraph", "a over b"), ("paragraph", "표가 든 각주"),
        ("table", "가\n나")]
    assert count.ok and [r.fallback_reason for r in parsed.regions] == ["shape_text", "equation_script"]
