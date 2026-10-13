"""HWPX 문단·제목·목록과 글자 장부(스펙 §5, §10). 합성 문서는 build_hwpx_fixtures.py의 쓰기 도구로 만든다."""

import importlib.util
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwpx import parser as hwpx
from hanji.formats.hwpx.parser import HwpxParser
from hanji_contracts import FlowLocator

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
HEAD = B.header(headings={1: ("OUTLINE", 0), 2: ("OUTLINE", 1), 3: ("OUTLINE", 7), 4: ("NUMBER", 0),
                          5: ("BULLET", 0)})


def parse(*sections: str, head: str | None = None, **kw):
    """(ParsedSource, 구역별 장부 숫자)."""
    return HwpxParser()._parse(B.package(*sections, head=HEAD if head is None else head, **kw), "a.hwpx")


def texts(parsed) -> list[tuple[str, str]]:
    return [(b["kind"], b["text"]) for b in parsed.blocks]


def reasons(parsed) -> list[str]:
    return [r.fallback_reason for r in parsed.regions]


def test_paragraph_text_is_the_runs_t_text_with_inline_replacements():
    s = B.section(B.para(B.run(B.t("가", B.tab(), "나", B.line_break(), "다", B.nb_space(), "라", B.fw_space(), "마",
                                   B.title_mark())), B.run(B.t(" 바 "), char=0)))
    parsed, counts = parse(s)
    assert texts(parsed) == [("paragraph", "가\t나\n다 라 마 바")]
    assert parsed.mime == "application/hwp+zip" and parsed.pages == () and parsed.assets == {}
    assert [c.ok for c in counts] == [True] and counts[0].source == counts[0].in_blocks == 6
    assert reasons(parsed) == []


def test_outline_levels_lists_and_section_paths_over_two_sections():
    """개요 0·1·7단계 → 제목 1·2·6, 번호·글머리 → 목록 항목, 모르는 paraPrIDRef → 문단. 빈 문단도 순번을 센다."""
    first = B.section(B.para(B.run(B.t("지어낸 큰 제목")), pr=1), B.para(), B.para(B.run(B.t("본문 문단")), pr=99),
                      B.para(B.run(B.t("둘째 수준")), pr=2))
    second = B.section(B.para(B.run(B.t("번호 항목")), pr=4), B.para(B.run(B.t("글머리 항목")), pr=5),
                       B.para(B.run(B.t("깊은 수준")), pr=3), B.para(B.run(B.t("다시 큰 제목")), pr=1),
                       B.para(B.run(B.t("끝 문단"))))
    parsed, counts = parse(first, second)
    assert [(b["kind"], b.get("level"), b["text"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("heading", 1, "지어낸 큰 제목", 0), ("paragraph", None, "본문 문단", 2), ("heading", 2, "둘째 수준", 3),
        ("list_item", None, "번호 항목", 4), ("list_item", None, "글머리 항목", 5), ("heading", 6, "깊은 수준", 6),
        ("heading", 1, "다시 큰 제목", 7), ("paragraph", None, "끝 문단", 8)]
    assert [b["section_path"] for b in parsed.blocks] == [
        (), ("지어낸 큰 제목",), ("지어낸 큰 제목",), ("지어낸 큰 제목", "둘째 수준"), ("지어낸 큰 제목", "둘째 수준"),
        ("지어낸 큰 제목", "둘째 수준"), (), ("다시 큰 제목",)]
    assert all(b["locator"] == FlowLocator(section_path=b["section_path"], paragraph_index=b["locator"].paragraph_index)
               for b in parsed.blocks)
    assert {b["confidence"] for b in parsed.blocks} == {1.0} and [c.ok for c in counts] == [True, True]


def test_empty_and_whitespace_paragraphs_make_no_blocks_and_count_nothing():
    parsed, counts = parse(B.section(B.para(), B.para(B.run(B.t("  "), B.t("", B.tab())))))
    assert parsed.blocks == () and counts[0].source == 0 and counts[0].ok


def test_unknown_inline_child_text_is_kept_in_place_once_with_a_record():
    s = B.section(B.para(B.run(B.t("앞", "<hp:mark>가운데</hp:mark>", "뒤"), B.t("", "<hp:mark>또</hp:mark>"))))
    parsed, counts = parse(s)
    assert texts(parsed) == [("paragraph", "앞가운데뒤또")]
    assert reasons(parsed) == ["unknown_inline_element"] and parsed.regions[0].region_id == "s0-p0-unknown-inline-0"
    assert counts[0].ok and counts[0].source == 6


def test_hidden_comment_text_is_counted_as_hidden_not_put_in_blocks():
    hidden = ("<hp:ctrl><hp:hiddenComment><hp:subList>" + B.para(B.run(B.t("숨은 설명 글자"))) +
              "</hp:subList></hp:hiddenComment></hp:ctrl>")
    parsed, counts = parse(B.section(B.para(B.run(B.t("보이는 글"), hidden))))
    assert texts(parsed) == [("paragraph", "보이는 글")]
    assert counts[0].hidden == 6 and counts[0].source == 10 and counts[0].in_blocks == 4 and counts[0].ok
    (region,) = parsed.regions
    assert (region.fallback_reason, region.kind, region.gate.checks[0].name, region.gate.checks[0].value) == (
        "hidden_comment", "paragraph", "hidden_chars", 6)


def test_text_inside_objects_survives_as_a_rescued_paragraph_at_the_section_end():
    """개체(HW-A가 아직 읽지 않는 모르는 개체) 안 글자는 닻 문단별 구조 문단(신뢰도 0.3)으로 구역 끝에 한 번 나온다."""
    box = "<hp:futureObject><hp:subList>{}</hp:subList></hp:futureObject>"
    s = B.section(B.para(B.run(B.t("닻 문단"), box.format(B.para(B.run(B.t("상자 첫"))) + B.para(B.run(B.t("상자 둘")))))),
                  B.para(B.run(B.t("다음 문단"))), B.para(B.run(box.format(B.para(B.run(B.t("둘째 상자")))))))
    parsed, counts = parse(s)
    assert [(b["kind"], b["text"], b["confidence"], b["locator"].paragraph_index) for b in parsed.blocks] == [
        ("paragraph", "닻 문단", 1.0, 0), ("paragraph", "다음 문단", 1.0, 1),
        ("paragraph", "상자 첫\n상자 둘", 0.3, 0), ("paragraph", "둘째 상자", 0.3, 2)]
    assert counts[0].ok and counts[0].rescued == 10
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id, region.gate.checks[0].value) == (
        "unassigned_text", "s0-p0-unassigned-text-0", 10)


def test_masterpage_text_is_skipped_with_a_record():
    master = (f'<masterPage xmlns:hp="{B.HP}"><hp:subList>' + B.para(B.run(B.t("마스터 쪽 글")))
              + "</hp:subList></masterPage>")
    parsed, _ = parse(B.section(B.para(B.run(B.t("본문")))), extra={"Contents/masterpage0.xml": master.encode()})
    assert texts(parsed) == [("paragraph", "본문")]
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id, region.gate.checks[0].value) == (
        "masterpage_text_skipped", "masterpage-0", 5)


def test_missing_header_and_wrong_section_root_are_parse_errors():
    data = B.package(B.section(B.para()))
    with zipfile.ZipFile(BytesIO(data)) as z:
        kept = [(i, z.read(i)) for i in z.infolist() if i.filename != "Contents/header.xml"]
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for info, raw in kept:
            z.writestr(info, raw)
    with pytest.raises(ParseError, match="missing") as info:
        HwpxParser().parse(buf.getvalue(), "a.hwpx")
    assert info.value.location == "a.hwpx:Contents/header.xml"
    with pytest.raises(ParseError, match="unexpected root element") as info:
        HwpxParser().parse(B.package(B.header()), "a.hwpx")
    assert info.value.location == "a.hwpx:Contents/section0.xml"


TWO = B.section(B.para(B.run(B.t("첫째 문단"))), B.para(B.run(B.t("가", B.tab(), "나")), B.run(B.t("둘째"))))


def test_a_dropped_source_is_rescued_once_with_unassigned_text(monkeypatch):
    original = hwpx._Section.bundles

    def drop_first(self, p, anchor):
        out = original(self, p, anchor)
        return out[1:] if anchor == 1 else out

    monkeypatch.setattr(hwpx._Section, "bundles", drop_first)
    parsed, counts = parse(TWO)
    assert texts(parsed) == [("paragraph", "첫째 문단"), ("paragraph", "둘째"), ("paragraph", "가\t나")]
    assert counts[0].ok and counts[0].rescued == 2 and reasons(parsed) == ["unassigned_text"]


def test_a_source_put_into_two_blocks_is_a_coverage_mismatch(monkeypatch):
    original = hwpx._Section.emit

    def twice(self, kind, bundles, anchor, **extra):
        original(self, kind, bundles, anchor, **dict(extra))
        return original(self, kind, bundles, anchor, **extra)

    monkeypatch.setattr(hwpx._Section, "emit", twice)
    parsed, counts = parse(TWO)
    assert counts[0].doubled == 3 and not counts[0].ok
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id) == ("coverage_mismatch", "s0-coverage")
    assert {c.name: (c.value, c.passed) for c in region.gate.checks} == {
        "in_blocks": (16, False), "doubled": (3, False), "partial": (3, False), "hidden": (0, True)}


def test_a_source_put_twice_into_the_same_block_is_a_coverage_mismatch(monkeypatch):
    original = hwpx._Section.bundles
    monkeypatch.setattr(hwpx._Section, "bundles", lambda self, p, anchor: original(self, p, anchor) * 2)
    parsed, counts = parse(TWO)
    assert texts(parsed)[0] == ("paragraph", "첫째 문단첫째 문단")
    assert (counts[0].source, counts[0].in_blocks, counts[0].doubled, counts[0].partial) == (8, 16, 3, 3)
    assert reasons(parsed) == ["coverage_mismatch"]


def test_a_source_put_with_some_of_its_text_missing_is_partial(monkeypatch):
    """<t>A<x>B</x>C</t>에서 B를 버리면 블록 글자를 따로 만드는 방식은 통과하지만, 장부는 출처 n과 넣은 글자 수를 견준다."""
    monkeypatch.setattr(hwpx, "_t_text", lambda t: ((t.text or "") + "".join(c.tail or "" for c in t), False))
    parsed, counts = parse(B.section(B.para(B.run(B.t("가", "<hp:x>나</hp:x>", "다")))))
    assert texts(parsed) == [("paragraph", "가다")]
    assert (counts[0].source, counts[0].in_blocks, counts[0].doubled, counts[0].partial) == (3, 2, 0, 1)
    assert reasons(parsed) == ["coverage_mismatch"]


def test_hidden_source_put_into_a_block_is_a_mismatch(monkeypatch):
    hidden = ("<hp:ctrl><hp:hiddenComment><hp:subList>" + B.para(B.run(B.t("숨은"))) +
              "</hp:subList></hp:hiddenComment></hp:ctrl>")
    original = hwpx._Section.bundles

    def with_hidden(self, p, anchor):
        extra = [self.source_bundle(t, anchor, p) for t in p.iter(hwpx.T) if self.ledger.sources[
            self.ledger.source_of(t)].hidden]
        return original(self, p, anchor) + extra

    monkeypatch.setattr(hwpx._Section, "bundles", with_hidden)
    parsed, counts = parse(B.section(B.para(B.run(B.t("보임"), hidden))))
    assert texts(parsed) == [("paragraph", "보임숨은")]
    count = counts[0]
    assert (count.source, count.in_blocks, count.hidden, count.doubled, count.partial) == (4, 4, 2, 1, 0)
    assert not count.ok and "coverage_mismatch" in reasons(parsed)  # 4 != 4 + 2


def test_negative_outline_level_is_clamped_to_heading_level_one():
    parsed, _ = parse(B.section(B.para(B.run(B.t("음수 수준")), pr=1)), head=B.header(headings={1: ("OUTLINE", -1)}))
    assert [(b["kind"], b["level"]) for b in parsed.blocks] == [("heading", 1)]


def test_text_inside_a_known_inline_element_is_kept_once_with_a_record():
    parsed, counts = parse(B.section(B.para(B.run(B.t("가", "<hp:tab>엑스</hp:tab>", "나")))))
    assert texts(parsed) == [("paragraph", "가\t엑스나")]
    assert reasons(parsed) == ["unknown_inline_element"] and counts[0].ok and counts[0].source == 4
