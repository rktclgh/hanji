"""HWPX 문단·제목·목록과 글자 장부(스펙 §5, §10). 합성 문서는 build_hwpx_fixtures.py의 쓰기 도구로 만든다."""

import importlib.util
import unicodedata
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwpx import ledger, owpml
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


def test_a_hyphen_element_is_a_hyphen_like_hwp_and_the_ledger_counts_it(monkeypatch):
    """hp:hypen(OWPML 철자)은 HWP 하이픈(WCHAR 24)처럼 -다. 장부 출처의 n에도 들어 빠지면 partial이다."""
    s = B.section(B.para(B.run(B.t("하이", B.hypen(), "픈"))))
    parsed, (count,) = parse(s)
    assert texts(parsed) == [("paragraph", "하이-픈")] and count.ok and count.source == 4 and parsed.regions == ()
    monkeypatch.setattr(owpml, "INLINE", {k: v for k, v in owpml.INLINE.items() if v != "-"})  # 처리부만 잃는다
    parsed, (count,) = parse(s)
    assert texts(parsed) == [("paragraph", "하이픈")] and count.partial == 1 and reasons(parsed) == ["coverage_mismatch"]


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


def test_ruby_text_follows_its_main_text_in_parentheses_and_overlaps_keep_their_characters_in_place(monkeypatch):
    """덧말은 그 자리에 본말(덧말)(덧말이 비면 본말만), 글자 겹침은 겹친 글자 문단·칸 글자에 든다. 본말·덧말·겹친
    글자는 장부 출처라 빠지면 살리고(unassigned_text) 두 번 들면 coverage_mismatch다."""
    s = B.section(B.para(B.run(B.t("앞 "), B.dutmal("본말", "덧말"), B.t(" 뒤 "), B.compose("가나"), B.dutmal("홀로", ""))),
                  B.para(B.run(B.tbl(1, 2, [B.tc(0, 0, B.para(B.run(B.dutmal("칸", "덧")))),
                                            B.tc(0, 1, B.para(B.run(B.compose("다"))))]))))
    parsed, (count,) = parse(s)
    assert texts(parsed) == [("paragraph", "앞 본말(덧말) 뒤 가나홀로"), ("table", "칸(덧)\t다")]
    assert count.ok and count.source == 13 and parsed.regions == ()
    monkeypatch.setattr(owpml._Section, "ruby", lambda self, element: [])
    parsed, (count,) = parse(s)
    assert texts(parsed)[0] == ("paragraph", "앞  뒤")
    assert texts(parsed)[2:] == [("paragraph", "본말덧말가나홀로"), ("paragraph", "칸덧\n다")]
    assert count.ok and count.rescued == 11 and reasons(parsed) == ["unassigned_text"]
    real = owpml._Section.bundles
    monkeypatch.undo()
    monkeypatch.setattr(owpml._Section, "bundles", lambda self, p, anchor: real(self, p, anchor) + [
        b for b in real(self, p, anchor) if b[0] is not None and self.ledger.sources[b[0]].element.tag != owpml.T])
    parsed, (count,) = parse(s)
    assert count.doubled == 4 and reasons(parsed) == ["coverage_mismatch"]  # 본말·덧말·겹친 글자·홀로


def _ruby_with(child: str) -> str:
    """스키마 밖: 덧말 본말 안에 child 요소가 든 덧말."""
    return ('<hp:dutmal posType="TOP" szRatio="0" option="0" styleIDRef="0" align="CENTER">'
            f"<hp:mainText>본말{child}</hp:mainText><hp:subText>덧</hp:subText></hp:dutmal>")


def test_ruby_text_is_only_its_direct_text_and_a_schema_foreign_t_inside_is_rescued_once():
    """덧말 본말·덧말의 글자는 직접 글자(text와 자식 뒤 글자)뿐이고 자식 요소는 제 규칙의 출처다. 본말 안 hp:t는 따로
    출처라 덧말 자리에는 안 들고, 문단 어디에도 놓이지 않아 구역 끝 구조 문단(unassigned_text)으로 한 번 살아난다.
    장부는 ok이고 두 번 든 글자가 없다(전에는 속글이 두 번 나거나 자식 글자가 소리 없이 빠졌다)."""
    parsed, (count,) = parse(B.section(B.para(B.run(_ruby_with("<hp:t>속글</hp:t>")))))
    assert texts(parsed) == [("paragraph", "본말(덧)"), ("paragraph", "속글")]
    assert parsed.blocks[1]["confidence"] == 0.3 and reasons(parsed) == ["unassigned_text"]
    assert count.ok and count.rescued == 2 and count.source == count.in_blocks == 5 and count.doubled == 0


def test_a_compose_or_textart_inside_ruby_text_is_its_own_source_and_is_rescued():
    """본말 안 글자 겹침의 composeText·글맵시의 text 속성도 따로 센 출처다: 덧말 자리에는 안 들고 장부 ok인 채로
    구역 끝 구조 문단으로 살아난다(전에는 장부가 둘 다 세지 않아 소리 없이 빠졌다)."""
    parsed, (count,) = parse(B.section(B.para(B.run(_ruby_with(B.compose("겹침"))))))
    assert texts(parsed) == [("paragraph", "본말(덧)"), ("paragraph", "겹침")]
    assert reasons(parsed) == ["unassigned_text"] and count.ok and count.rescued == 2 and count.source == 5
    parsed, (count,) = parse(B.section(B.para(B.run(_ruby_with(B.textart("글맵시"))))))
    assert texts(parsed) == [("paragraph", "본말(덧)"), ("paragraph", "글맵시")]
    assert reasons(parsed) == ["unassigned_text"] and count.ok and count.rescued == 3 and count.source == 6


def test_a_hidden_comment_inside_ruby_text_stays_hidden_and_out_of_the_visible_ruby():
    """본말 안 숨은 설명은 숨은 출처다: hidden_comment 처리 이력을 남기고 글자는 덧말 자리에 들지 않으며 블록에도 없다."""
    hidden = ("<hp:hiddenComment><hp:subList>" + B.para(B.run(B.t("숨은 글"))) + "</hp:subList></hp:hiddenComment>")
    parsed, (count,) = parse(B.section(B.para(B.run(_ruby_with(hidden)))))
    assert texts(parsed) == [("paragraph", "본말(덧)")]
    assert reasons(parsed) == ["hidden_comment"] and count.ok and count.hidden == 3 and count.rescued == 0


def test_the_circled_table_is_exactly_the_unicode_circle_decompositions_of_the_five_ranges_and_zero():
    """원문자 표는 파서와 무관한 유니코드 데이터로 확인한다: 모든 항목의 분해가 '<circle> 그 글자'이고 BMP 글자다. 범위는
    ⓪ 0, ①–⑳, ㉑–㉟, ㊱–㊿, ⓐ–ⓩ, Ⓐ–Ⓩ뿐이다(원문자 한글·한자 등은 넣지 않는다)."""
    for text, glyph in ledger.CIRCLED.items():
        assert unicodedata.decomposition(glyph) == "<circle> " + " ".join(f"{ord(ch):04X}" for ch in text), text
        assert ord(glyph) <= 0xFFFF
    keys = {str(n) for n in range(51)} | {chr(c) for c in range(0x61, 0x7B)} | {chr(c) for c in range(0x41, 0x5B)}
    assert set(ledger.CIRCLED) == keys
    assert ledger.circled("21", "SHAPE_CIRCLE") == "㉑" and ledger.circled("51", "SHAPE_CIRCLE") == "51"
    assert ledger.circled("가", "SHAPE_CIRCLE") == "가" and ledger.circled("12", "SHAPE_RECTANGLE") == "12"
    assert ledger.circled("1", "CHAR") == "1" and ledger.circled("1", None) == "1"


def test_circled_overlaps_become_circled_characters_and_the_ledger_counts_them_on_its_own(monkeypatch):
    """원 모양(SHAPE_CIRCLE) 글자 겹침은 글자 전체가 표에 있으면 원문자 하나, 아니면 그대로(사각형 등 다른 모양도
    그대로). 장부 n은 처리부와 따로 센다: 처리부만 표를 잃으면 두 자("21")가 partial로 잡힌다."""
    s = B.section(B.para(B.run(B.compose("1", circle="SHAPE_CIRCLE"), B.compose("21", circle="SHAPE_CIRCLE"),
                               B.compose("가", circle="SHAPE_CIRCLE"), B.compose("21", circle="SHAPE_RECTANGLE"),
                               B.compose("①"))))
    parsed, (count,) = parse(s)
    assert texts(parsed) == [("paragraph", "①㉑가21①")] and count.ok and count.source == 6
    monkeypatch.setattr(owpml, "circled", lambda text, circle: text)  # 처리부만 표를 잃는다
    parsed, (count,) = parse(s)
    assert texts(parsed) == [("paragraph", "121가21①")] and count.partial == 1
    assert reasons(parsed) == ["coverage_mismatch"]


def test_a_text_box_holding_only_ruby_or_an_overlap_still_records_shape_text():
    for inner in (B.dutmal("본말", "덧"), B.compose("가")):
        parsed, (count,) = parse(B.section(B.para(B.run(B.rect(B.para(B.run(inner)))))))
        assert reasons(parsed) == ["shape_text"] and count.ok


def test_masterpage_text_is_skipped_with_a_record():
    master = (f'<masterPage xmlns:hp="{B.HP}"><hp:subList>' + B.para(B.run(B.t("마스터 쪽 글")))
              + "</hp:subList></masterPage>")
    parsed, _ = parse(B.section(B.para(B.run(B.t("본문")))), extra={"Contents/masterpage0.xml": master.encode()})
    assert texts(parsed) == [("paragraph", "본문")]
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id, region.gate.checks[0].value) == (
        "masterpage_text_skipped", "masterpage-0", 5)


def test_spine_items_outside_contents_leave_one_record():
    items = [("js", "Scripts/headerScripts.js", "application/x-javascript", True)]
    parsed, _ = parse(B.section(B.para(B.run(B.t("본문")))), items=items, spine=["header", "section0", "js"],
                      extra={"Scripts/headerScripts.js": b"var x;"})
    assert texts(parsed) == [("paragraph", "본문")]
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id, region.kind, region.gate.checks[0].value) == (
        "spine_item_skipped", "spine-skipped", "paragraph", 1)


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
    monkeypatch.setattr(owpml, "_t_text", lambda t: ((t.text or "") + "".join(c.tail or "" for c in t), False))
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


SIZES = B.header(chars={1: (1600, False), 2: (1100, True), 3: (1300, False), 4: (2000, False), 5: (1050, True)},
                 headings={1: ("OUTLINE", 0)})
BODY = "본문 크기로 쓴 지어낸 문단이다. 글자 수가 가장 많다."


def test_without_outline_levels_large_short_paragraphs_are_headings_by_size():
    """본문 10pt. 20pt·16pt 짧은 문단 → 제목 1·2(크기 순위), 16pt 3줄 → 문단, 굵지 않은 13pt(1.3배) → 제목 3,
    굵은 11pt(1.1배 ≥ 1.05) → 제목 4, 굵은 10.5pt(1.05배) → 제목 5, 첫 런만 크고 대부분 본문인 문단 → 문단."""
    s = B.section(B.para(B.run(B.t("가장 큰 제목"), char=4), lines=1),
                  B.para(B.run(B.t(BODY)), lines=1),
                  B.para(B.run(B.t("큰 제목"), char=1), lines=2),
                  B.para(B.run(B.t("큰 글자로 쓴 세 줄 강조 문단"), char=1), lines=3),
                  B.para(B.run(B.t("굵은 작은 제목"), char=2), lines=1),
                  B.para(B.run(B.t("조금 큰 제목"), char=3)),
                  B.para(B.run(B.t("굵은 본문급 제목"), char=5), lines=1),
                  B.para(B.run(B.t("큰"), char=1), B.run(B.t("대부분 본문 크기 글자")), lines=1),
                  B.para(B.run(B.t(BODY * 3)), lines=2))
    parsed, counts = parse(s, head=SIZES)
    assert [(b["kind"], b.get("level"), b["confidence"]) for b in parsed.blocks] == [
        ("heading", 1, 0.6), ("paragraph", None, 1.0), ("heading", 2, 0.6), ("paragraph", None, 1.0),
        ("heading", 4, 0.6), ("heading", 3, 0.6), ("heading", 5, 0.6), ("paragraph", None, 1.0),
        ("paragraph", None, 1.0)]
    assert parsed.blocks[1]["section_path"] == ("가장 큰 제목",)
    assert parsed.blocks[-1]["section_path"] == ("가장 큰 제목", "큰 제목", "조금 큰 제목", "굵은 본문급 제목")
    assert counts[0].ok


def test_ruby_and_composed_text_count_toward_the_heading_size_weights():
    """덧말·글자 겹침만 든 문단도 그 런의 글자 크기로 센다(본말·겹친 글자만, 작게 찍히는 덧말 글자는 세지 않는다)."""
    s = B.section(B.para(B.run(B.t(BODY * 2))),
                  B.para(B.run(B.dutmal("덧말뿐인 큰 제목", "작은 덧말 글자를 길게 붙인다"), char=4), lines=1),
                  B.para(B.run(B.compose("겹침큰제목"), char=1), lines=1))
    parsed, counts = parse(s, head=SIZES)
    assert [(b["kind"], b.get("level")) for b in parsed.blocks] == [("paragraph", None), ("heading", 1), ("heading", 2)]
    assert counts[0].ok


def test_the_representative_size_is_weighted_by_characters_and_bold_needs_a_majority():
    """첫 런만 크고 굵어도 글자 수 과반이 본문 크기·보통이면 문단이다(첫 런만 보지 않는다)."""
    s = B.section(B.para(B.run(B.t(BODY * 2))),
                  B.para(B.run(B.t("큰굵은"), char=2), B.run(B.t("보통 굵기의 긴 본문 크기 글자들")), lines=1),
                  B.para(B.run(B.t("굵은 글자가 과반"), char=2), B.run(B.t("보통")), lines=1))
    parsed, _ = parse(s, head=SIZES)
    assert [b["kind"] for b in parsed.blocks] == ["paragraph", "paragraph", "heading"]


def test_line_count_falls_back_to_line_breaks_and_outline_documents_skip_the_size_rule():
    s = B.section(B.para(B.run(B.t(BODY * 2))),
                  B.para(B.run(B.t("큰", B.line_break(), "두 줄", B.line_break(), "세 줄"), char=1)),
                  B.para(B.run(B.t("큰", B.line_break(), "두 줄"), char=1)),
                  B.para(B.run(B.t("큰", B.line_break(), "두", B.line_break(), "세 줄"), char=1), lines=0),
                  B.para(B.run(B.t("뒤따르는 본문"))))
    parsed, counts = parse(s, head=SIZES)
    assert [b["kind"] for b in parsed.blocks] == ["paragraph", "paragraph", "heading", "paragraph", "paragraph"]
    assert parsed.blocks[2]["text"] == "큰 두 줄"  # 줄바꿈은 공백 하나로(마크다운 제목이 깨지지 않는다)
    assert parsed.blocks[-1]["section_path"] == ("큰 두 줄",)
    assert counts[0].ok  # 글자 장부는 원문 글자로 센다
    parsed, _ = parse(B.section(B.para(B.run(B.t("개요 제목")), pr=1), B.para(B.run(B.t(BODY * 2))),
                                B.para(B.run(B.t("큰 글자"), char=1), lines=1)), head=SIZES)
    assert [(b["kind"], b.get("level"), b["confidence"]) for b in parsed.blocks] == [
        ("heading", 1, 1.0), ("paragraph", None, 1.0), ("paragraph", None, 1.0)]


SHAPES = B.header(chars={2: (1100, True), 5: (1050, True), 6: (1100, False)})


def test_bold_is_judged_by_character_majority_not_by_any_or_first_run():
    """다수 크기 11pt인데 굵은 글자가 소수 → 굵지 않은 문단(11 < 11.5) → 문단. 굵은 글자가 과반이면 첫 런이 굵지
    않아도 굵은 문단(10.5 ≥ 10.5) → 제목."""
    s = B.section(B.para(B.run(B.t(BODY * 2))),
                  B.para(B.run(B.t("열한 포인트 보통 굵기 문장"), char=6), B.run(B.t("굵게"), char=2), lines=1),
                  B.para(B.run(B.t("보통"), char=0), B.run(B.t("굵은 글자가 과반"), char=5), lines=1))
    parsed, _ = parse(s, head=SHAPES)
    assert [b["kind"] for b in parsed.blocks] == ["paragraph", "paragraph", "heading"]


def test_heading_levels_fold_at_six():
    sizes = {k: (1100 + 100 * k, False) for k in range(1, 8)}  # 12pt ~ 18pt, 일곱 가지
    s = B.section(B.para(B.run(B.t(BODY * 3))),
                  *(B.para(B.run(B.t(f"제목 {k}"), char=k), lines=1) for k in range(7, 0, -1)))
    parsed, _ = parse(s, head=B.header(chars=sizes))
    assert [b.get("level") for b in parsed.blocks] == [None, 1, 2, 3, 4, 5, 6, 6]


def test_huge_header_integers_are_clamped_and_never_overflow():
    huge = int("1" + "0" * 400)
    s = B.section(B.para(B.run(B.t(BODY * 3))), B.para(B.run(B.t("제목"), char=1), lines=1))
    for head in (B.header(chars={1: (huge, False)}), B.header(headings={1: ("OUTLINE", huge)}),
                 B.header(chars={1: (1600, False)}).replace('hangul="100"', 'hangul="' + "9" * 400 + '"')):
        parsed, counts = parse(s, head=head)
        assert [b["kind"] for b in parsed.blocks] and all(c.ok for c in counts)


def test_a_body_size_of_zero_or_less_skips_the_size_rule():
    s = B.section(B.para(B.run(B.t(BODY * 3))), B.para(B.run(B.t("짧은 문단")), lines=1))
    for height in (0, -1000):
        parsed, _ = parse(s, head=B.header(chars={0: (height, False)}))
        assert [b["kind"] for b in parsed.blocks] == ["paragraph", "paragraph"]
