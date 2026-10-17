"""HWP 레코드 → OWPML 요소 트리 옮겨 쓰기(HWP 스펙 §7.1·§7.2, HP-A의 §7.3)와 옮겨 쓰기 장부(§6)·예산(§5.4)."""

import importlib.util
import struct
import tracemalloc
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwp import transcode
from hanji.formats.hwp.cfb import Compound
from hanji.formats.hwp.stream import Reader
from hanji.formats.hwp.transcode import Nodes
from hanji.formats.hwpx import container

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwp_fixtures", HERE / "build_hwp_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


def section(data: bytes, nodes: Nodes | None = None) -> transcode.Section:
    cfb = Compound(B.compound({"BodyText/Section0": data}), "a.hwp")
    top = Reader(cfb, compressed=False).tree("BodyText/Section0")
    return transcode.section(top, 0, 0, "a.hwp:BodyText/Section0", nodes or Nodes())


def local(element: ET.Element) -> str:
    return element.tag.split("}")[1]


def shape(p: ET.Element) -> list:
    """문단 모양: 런마다 (charPrIDRef, [hp:t는 (text, [(자식, tail)]), 그 밖은 (이름, [자식 이름])])."""
    out = []
    for run in p.iterfind(f"{HP}run"):
        items = []
        for child in run:
            if local(child) == "t":
                items.append((child.text, [(local(c), c.tail) for c in child]))
            else:
                items.append((local(child), [local(c) for c in child]))
        out.append((run.get("charPrIDRef"), items))
    return out


def reasons(result: transcode.Section) -> list[tuple[str, str | None, int | None]]:
    return [(r.reason, r.gate.checks[0].name if r.gate else None, r.gate.checks[0].value if r.gate else None)
            for r in result.regions]


def test_text_and_control_characters_become_owpml_elements_and_runs_split_at_char_shape_bounds():
    result = section(B.paragraph("가나", B.TAB, "다", B.LINE_BREAK, "라", B.NB_SPACE, B.FW_SPACE, B.HYPHEN,
                                 B.TITLE_MARK, "마", pr=7, style=3, shapes=((0, 4), (3, 9), (30, 5)), lines=2)
                     + B.paragraph())
    first, empty = result.root
    assert (first.get("paraPrIDRef"), first.get("styleIDRef")) == ("7", "3")
    # 경계 3은 탭(2~9) 안이라 탭 시작으로 당긴다. 경계 30은 문단 끝 뒤라 빈 런
    assert shape(first) == [("4", [("가나", [])]),
                            ("9", [(None, [("tab", "다"), ("lineBreak", "라"), ("nbSpace", None), ("fwSpace", "-"),
                                           ("titleMark", "마")])]),
                            ("5", [])]
    assert len(first.findall(f"{HP}linesegarray/{HP}lineseg")) == 2
    assert shape(empty) == [("0", [])] and empty.find(f"{HP}linesegarray") is not None
    assert result.regions == [] and result.audit.ok


def test_surrogate_pairs_lone_surrogates_unknown_control_chars_and_trailing_chars_are_recorded():
    text = B.units("보충 평면 \U000f02ee 글", [0xD800], "끝", B.inline(5), [25], B.PARA_END, "뒤")
    result = section(B.paragraph(text=text))
    ((_, [(t, children)]),) = shape(result.root[0])
    assert (t, children) == ("보충 평면 \U000f02ee 글�끝뒤", [])
    assert reasons(result) == [("invalid_utf16", "chars", 1), ("unknown_control_char", "chars", 2),
                               ("record_inconsistent", "trailing_chars", 1)]
    assert [r.region_id for r in result.regions] == [None, None, None] and result.audit.ok
    with pytest.raises(ParseError, match="truncated control in PARA_TEXT") as info:
        section(B.paragraph(text=B.units("앞") + B.TAB[:5]))
    assert info.value.location == "a.hwp:BodyText/Section0#1"


def test_surrogates_pair_only_inside_one_span_of_ordinary_characters():
    text = B.units("가\U0001f600", B.TAB, "\U0001f600나", [0xD800], B.LINE_BREAK, [0xDC00], "다", B.PARA_END)
    result = section(B.paragraph(text=text))
    # 대리쌍은 구간 끝·컨트롤 옆에서도 묶이고, 제어 문자를 사이에 둔 대리 문자 둘은 짝이 아니다
    assert shape(result.root[0]) == [("0", [("가\U0001f600", [("tab", "\U0001f600나�"), ("lineBreak", "�다")])])]
    assert reasons(result) == [("invalid_utf16", "chars", 2)] and result.audit.ok


def test_char_shape_bounds_split_a_span_but_never_a_surrogate_pair():
    # 가0 나1 대리쌍2–3 다4 짝 없는 상위 대리5 라6 짝 없는 하위 대리7 문단 끝8
    text = B.units("가나\U0001f600다", [0xD800], "라", [0xDC00], B.PARA_END)
    result = section(B.paragraph(text=text, shapes=((0, 1), (1, 2), (3, 3), (5, 4), (6, 5), (7, 6))))
    # 경계 3은 대리쌍 안이라 2로 당긴다. 짝 없는 대리 문자 앞뒤 경계(5·6·7)는 그대로 끊는다
    assert shape(result.root[0]) == [("1", [("가", [])]), ("2", [("나", [])]), ("3", [("\U0001f600다", [])]),
                                     ("4", [("�", [])]), ("5", [("라", [])]), ("6", [("�", [])])]
    assert reasons(result) == [("invalid_utf16", "chars", 2)] and result.audit.ok


def odd_text(*items: str | list[int], tail: bytes = b"\x00") -> bytes:
    """PARA_TEXT 몸 끝에 바이트 하나가 더 붙은(홀수 길이) 문단. PARA_HEADER의 글자 수는 WCHAR 수와 같다."""
    body = b"".join(struct.pack("<H", w) for w in B.units(*items) + B.PARA_END)
    return B.paragraph(text=B.units(*items) + B.PARA_END).replace(
        B.record(B.PARA_TEXT, 1, body), B.record(B.PARA_TEXT, 1, body + tail))


def test_an_odd_byte_at_the_end_of_para_text_is_recorded_as_invalid_utf16_without_changing_the_text():
    result = section(odd_text("가나"))
    assert shape(result.root[0]) == [("0", [("가나", [])])]
    assert reasons(result) == [("invalid_utf16", "chars", 1)] and result.audit.ok
    # 짝 없는 대리 문자와 같은 문단 기록 하나에 센다(글자 수 2), 짝수 길이 몸은 기록이 없다
    both = section(odd_text("가", [0xD800], "나"))
    assert shape(both.root[0]) == [("0", [("가�나", [])])]
    assert reasons(both) == [("invalid_utf16", "chars", 2)] and both.audit.ok
    assert reasons(section(B.paragraph("가나"))) == []


def test_surrogates_in_control_info_words_neither_pair_nor_count():
    # 가0, 탭1–8(부가 정보에 대리쌍), 나9, 모르는 인라인 컨트롤10–17(부가 정보에 대리쌍), 문단 끝18
    text = B.units("가", [9, 0xD800, 0xDC00, 0, 0, 0, 0, 9], "나", [5, 0x41, 0xD800, 0xDC00, 0, 0, 0, 5], B.PARA_END)
    result = section(B.paragraph(text=text, shapes=((0, 1), (3, 2), (12, 3), (14, 4))))
    # 경계 3은 탭 안이라 1로 당긴다. 낱말이 아닌 모르는 컨트롤 안 경계(12·14)는 당기지 않고 글자 뒤 빈 런 하나로 접힌다
    assert shape(result.root[0]) == [("1", [("가", [])]), ("2", [(None, [("tab", "나")])]), ("3", [])]
    assert reasons(result) == [("unknown_control_char", "chars", 1)] and result.audit.ok


def test_a_long_paragraph_is_transcoded_without_a_python_object_per_character():
    n = 200_000
    cfb = Compound(B.compound({"BodyText/Section0": B.paragraph("가" * n, B.TAB, "끝")}), "a.hwp")
    top = Reader(cfb, compressed=False).tree("BodyText/Section0")
    tracemalloc.start()
    try:
        result = transcode.section(top, 0, 0, "a.hwp:BodyText/Section0", Nodes())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result.audit.ok and len(result.root[0].find(f"{HP}run/{HP}t").text) == n
    assert peak < 10 * 2 * n  # 입력(WCHAR 2B)의 10배 미만(지금 약 4배, 글자마다 객체를 만들면 90배 안팎)


def test_extended_controls_pair_with_ctrl_headers_in_order_and_unknown_ones_carry_their_paragraphs():
    cell = B.control("tbl ", 1, B.list_header(2, 2), B.paragraph("칸 첫째", level=2), B.paragraph("칸 둘째", level=2))
    data = B.paragraph(B.extended(2, "secd"), B.extended(2, "cold"), "앞", B.extended(3, "%hlk"), "링크", B.FIELD_END,
                       "뒤", B.extended(11, "tbl "), shapes=((0, 1), (20, 2)),
                       controls=[B.control("secd", 1), B.control("cold", 1), B.control("%hlk", 1), cell])
    result = section(data)
    # 경계 20은 필드 시작 컨트롤(17~24) 안이라 17로 당긴다
    assert shape(result.root[0]) == [("1", [("ctrl", []), ("ctrl", []), ("앞", [])]),
                                     ("2", [("ctrl", []), ("링크", []), ("ctrl", ["fieldEnd"]), ("뒤", []),
                                            ("ctrl", ["unknownObject"])])]
    sub = result.root[0].find(f"{HP}run/{HP}ctrl/{HP}unknownObject/{HP}subList")
    assert [shape(p)[0][1][0][0] for p in sub] == ["칸 첫째", "칸 둘째"]
    assert reasons(result) == [("unknown_control", None, None)] and result.audit.ok


def test_control_count_id_and_nchars_mismatches_keep_all_text_and_are_recorded():
    data = (B.paragraph(B.extended(11, "gso "), "글", controls=[B.control("tbl ", 1, B.paragraph("표 글", level=2)),
                                                                B.control("eqed", 1)], nchars=99)
            + B.paragraph("둘째", B.extended(11, "tbl ")))
    result = section(data)
    first, second = result.root
    # 짝지은 첫 컨트롤(ID가 다르다)은 제자리, 남은 수식 컨트롤은 문단 끝 자리 개체
    assert shape(first) == [("0", [("ctrl", ["unknownObject"]), ("글", []), ("ctrl", ["unknownObject"])])]
    assert shape(second) == [("0", [("둘째", [])])]
    assert reasons(result) == [("record_inconsistent", "nchars", 1), ("record_inconsistent", "control_mismatch", 2),
                               ("unknown_control", None, None), ("record_inconsistent", "control_mismatch", 1)]
    assert result.audit.ok


def test_text_in_overlap_and_ruby_controls_is_counted_and_skipped():
    tcps = B.control("tcps", 1, body=b"\x02\x00" + "겹침".encode("utf-16-le") + bytes(4))
    tdut = B.control("tdut", 1, body=b"\x02\x00" + "본말".encode("utf-16-le") + b"\x01\x00" + "덧".encode("utf-16-le"))
    result = section(B.paragraph("앞", B.extended(23, "tcps"), B.extended(23, "tdut"), controls=[tcps, tdut]))
    assert reasons(result) == [("control_text_skipped", "chars", 2), ("control_text_skipped", "chars", 3)]
    assert result.audit.ok
    with pytest.raises(ParseError, match="CTRL_HEADER tdut record too short") as info:
        section(B.paragraph(B.extended(23, "tdut"), controls=[B.control("tdut", 1, body=b"\x05\x00")]))
    assert info.value.location == "a.hwp:BodyText/Section0#4"


def test_text_controls_outside_paragraphs_are_audited_with_their_record_location():
    # 문단을 거치지 않는 덧말·글자 겹침(레벨 0, 다른 컨트롤 바로 아래)은 옮겨 쓰지 않고 장부만 읽는다
    with pytest.raises(ParseError, match="CTRL_HEADER tdut record too short") as info:
        section(B.paragraph("앞") + B.control("tdut", 0, body=b"\x05\x00"))
    assert info.value.location == "a.hwp:BodyText/Section0#4"
    nested = B.control("gso ", 1, B.control("tcps", 2, body=b"\x09\x00"))
    with pytest.raises(ParseError, match="CTRL_HEADER tcps record too short") as info:
        section(B.paragraph(B.extended(11, "gso "), controls=[nested]))
    assert info.value.location == "a.hwp:BodyText/Section0#5"


def test_unknown_record_tags_are_counted_per_section():
    result = section(B.paragraph("글") + B.record(0x3FF, 0, b"x") + B.record(0x200, 1))
    assert reasons(result) == [("unknown_record", "records", 2)]
    assert result.regions[0].region_id == "s0-unknown-record" and result.audit.ok


def cells(tbl: ET.Element) -> list[list[tuple]]:
    """행마다 칸 (header, borderFillIDRef, rowAddr, colAddr, rowSpan, colSpan, 문단 글자들)."""
    out = []
    for tr in tbl.iterfind(f"{HP}tr"):
        row = []
        for tc in tr.iterfind(f"{HP}tc"):
            addr, span = tc.find(f"{HP}cellAddr"), tc.find(f"{HP}cellSpan")
            texts = ["".join(t.text or "" for t in p.iter(f"{HP}t")) for p in tc.find(f"{HP}subList")]
            row.append((tc.get("header"), tc.get("borderFillIDRef"), addr.get("rowAddr"), addr.get("colAddr"),
                        span.get("rowSpan"), span.get("colSpan"), texts))
        out.append(row)
    return out


def test_tables_become_hp_tbl_with_caption_cells_spans_header_bits_and_fills():
    tbl = B.table(1, 2, 3, B.cell(2, 0, 0, B.paragraph("구분", level=2), header=True),
                  B.cell(2, 0, 1, B.paragraph("값", level=2), header=True, fill=2),
                  B.cell(2, 0, 2, B.paragraph("비고", level=2), span=(2, 1)),
                  B.cell(2, 1, 0, B.paragraph("인건비", level=2), B.paragraph("둘째 줄", level=2), span=(1, 2)),
                  caption=B.caption(2, B.paragraph("표 1. 지어낸 표", level=2), side="TOP"))
    result = section(B.paragraph("표 앞", B.extended(11, "tbl "), "뒤", controls=[tbl]))
    assert shape(result.root[0]) == [("0", [("표 앞", []), ("tbl", ["caption", "tr", "tr"]), ("뒤", [])])]
    found = result.root[0].find(f"{HP}run/{HP}tbl")
    assert (found.get("rowCnt"), found.get("colCnt"), found.find(f"{HP}caption").get("side")) == ("2", "3", "TOP")
    assert [shape(p)[0][1][0][0] for p in found.find(f"{HP}caption/{HP}subList")] == ["표 1. 지어낸 표"]
    assert cells(found) == [[("1", "1", "0", "0", "1", "1", ["구분"]), ("1", "2", "0", "1", "1", "1", ["값"]),
                             ("0", "1", "0", "2", "2", "1", ["비고"])],
                            [("0", "1", "1", "0", "1", "2", ["인건비", "둘째 줄"])]]
    assert result.regions == [] and result.audit.ok


def test_table_lists_out_of_count_and_paragraphs_outside_any_list_keep_their_text():
    tbl = B.control("tbl ", 1, B.record(B.TABLE, 2, struct.pack("<IHH", 0, 1, 1)),
                    B.cell(2, 0, 0, B.paragraph("칸 글", level=2)).replace(struct.pack("<hIH", 1, 0, 0),
                                                                          struct.pack("<hIH", 3, 0, 0)),
                    B.record(B.CTRL_HEADER, 2, b"xxxx"), B.paragraph("리스트 밖 문단", level=2))
    result = section(B.paragraph(B.extended(11, "tbl "), controls=[tbl]))
    assert shape(result.root[0]) == [("0", [("tbl", ["tr"]), ("ctrl", ["unknownObject"])])]
    assert reasons(result) == [("record_inconsistent", "list_paragraphs", 2), ("unknown_control", None, None)]
    assert result.audit.ok
    short_caption = B.record(B.LIST_HEADER, 2, bytes(11)) + B.record(B.TABLE, 2, bytes(8))
    for broken, what, at in ((B.record(B.TABLE, 2, bytes(7)), "TABLE", 5),
                             (short_caption, "LIST_HEADER caption", 5)):  # 문단 머리 0~3, 컨트롤 4
        with pytest.raises(ParseError, match=f"{what} record too short") as info:
            section(B.paragraph(B.extended(11, "tbl "), controls=[B.control("tbl ", 1, broken)]))
        assert info.value.location == f"a.hwp:BodyText/Section0#{at}"


def test_a_list_shorter_than_a_cell_after_table_takes_the_leftover_path_instead_of_failing():
    tbl = B.control("tbl ", 1, B.record(B.TABLE, 2, struct.pack("<IHH", 0, 1, 1)),
                    B.cell(2, 0, 0, B.paragraph("칸 글", level=2)), B.caption(2, B.paragraph("뒤에 온 캡션", level=2)))
    result = section(B.paragraph(B.extended(11, "tbl "), controls=[tbl]))
    assert shape(result.root[0]) == [("0", [("tbl", ["tr"]), ("ctrl", ["unknownObject"])])]
    assert reasons(result) == [("record_inconsistent", "list_shape", 1), ("unknown_control", None, None)]
    assert cells(result.root[0].find(f"{HP}run/{HP}tbl")) == [[("0", "1", "0", "0", "1", "1", ["칸 글"])]]
    rescued = result.root[0].find(f"{HP}run/{HP}ctrl/{HP}unknownObject")
    assert ["".join(t.text or "" for t in p.iter(f"{HP}t")) for p in rescued.iter(f"{HP}p")] == ["뒤에 온 캡션"]
    assert result.audit.ok


def tags(element: ET.Element) -> list:
    """요소 이름과 자식(재귀) — hp:p 안은 런 모양 대신 문단 글자."""
    if local(element) == "p":
        return ["p", "".join(t.text or "" for t in element.iter(f"{HP}t"))]
    return [local(element), {k: v for k, v in element.attrib.items()}, [tags(c) for c in element]]


def test_drawing_objects_become_pictures_shapes_containers_and_embedded_objects():
    shapes = [B.gso(1, B.pic(2, 3, width=14400, height=7200), caption=B.caption(2, B.paragraph("그림 1", level=2))),
              B.gso(1, B.component(2, "$pic", B.picture(3, 5), size=(500, 400), first=(500, 400))),
              B.gso(1, B.container(2, B.pic(3, 1, top=False), B.rect(3, B.paragraph("묶음 속 글상자", level=4), top=False),
                                   B.line(3, top=False))),
              B.gso(1, B.ole(2)), B.gso(1, B.ole(2, chart=True)), B.gso(1, B.textart(2, "지어낸 글맵시")),
              B.gso(1, B.component(2, "$unk", B.listed(3, B.paragraph("모르는 도형 글", level=3))))]
    data = B.paragraph("앞", *[B.extended(11, "gso ")] * len(shapes), controls=shapes)
    result = section(data)
    run = result.root[0].find(f"{HP}run")
    assert [tags(e) for e in run][1:] == [
        ["pic", {}, [["curSz", {"width": "14400", "height": "7200"}, []], ["img", {"binaryItemIDRef": "3"}, []],
                     ["caption", {"side": "BOTTOM"}, [["subList", {}, [["p", "그림 1"]]]]]]],
        ["pic", {}, [["curSz", {"width": "0", "height": "0"}, []], ["img", {"binaryItemIDRef": "5"}, []]]],
        ["container", {}, [["pic", {}, [["curSz", {"width": "7200", "height": "3600"}, []],
                                        ["img", {"binaryItemIDRef": "1"}, []]]],
                           ["rect", {}, [["drawText", {}, [["subList", {}, [["p", "묶음 속 글상자"]]]]]]],
                           ["line", {}, []]]],
        ["ole", {}, []], ["chart", {}, []], ["textart", {"text": "지어낸 글맵시"}, []],
        ["ctrl", {}, [["unknownObject", {}, [["subList", {}, [["p", "모르는 도형 글"]]]]]]]]
    assert reasons(result) == [("unknown_control", None, None)] and result.audit.ok


def test_equations_carry_their_script_and_caption_and_both_join_the_transcode_ledger(monkeypatch):
    script = "x = {-b +- sqrt {b^2 - 4ac}} over {2a}"
    data = B.paragraph(B.extended(11, "eqed"), B.extended(11, "gso "),
                       controls=[B.equation(1, script, caption=B.caption(2, B.paragraph("식 1", level=2), side="TOP")),
                                 B.gso(1, B.textart(2, "글맵시 글"))])
    result = section(data)
    eq = result.root[0].find(f"{HP}run/{HP}equation")
    assert eq.findtext(f"{HP}script") == script and eq.find(f"{HP}caption").get("side") == "TOP"
    assert result.audit.ok and result.regions == []

    def check(mutate) -> transcode.Audit:
        def wrapped(records, root, marks, skipped, location):
            mutate(root)
            return REAL_AUDIT(records, root, marks, skipped, location)

        monkeypatch.setattr(transcode, "audit", wrapped)
        return section(data).audit

    def cut_script(root):
        found = next(root.iter(f"{HP}script"))
        found.text = found.text[:-3]

    def script_tail(root):
        next(root.iter(f"{HP}script")).tail = "떠돌이"

    def drop_art(root):
        run = next(root.iter(f"{HP}run"))
        run.remove(run.find(f"{HP}textart"))

    def change_art(root):
        next(root.iter(f"{HP}textart")).set("text", "글맵시 굴")

    assert check(cut_script) == transcode.Audit(1, 0, 0)
    assert check(script_tail) == transcode.Audit(0, 0, 3)
    assert check(drop_art) == transcode.Audit(0, 1, 0)
    assert check(change_art) == transcode.Audit(1, 0, 0)


def test_objects_without_their_body_record_take_the_unknown_path_and_short_ones_are_refused():
    result = section(B.paragraph(B.extended(11, "eqed"), B.extended(11, "gso "),
                                 controls=[B.control("eqed", 1), B.gso(1, B.component(2, "$tat"))]))
    assert [tags(e)[0] for e in result.root[0].find(f"{HP}run")] == ["ctrl", "ctrl"]
    assert reasons(result) == [("unknown_control", None, None)] and result.audit.ok
    pic = B.component(2, "$pic", B.record(B.SC_PICTURE, 3, bytes(72)))
    short = B.record(B.SHAPE_COMPONENT, 2, b"cip$" * 2 + bytes(27))  # ID 두 번 뒤 크기까지 28바이트가 모자란다
    for control, what, at in ((B.gso(1, short), "SHAPE_COMPONENT", 5),
                              (B.gso(1, pic), "SHAPE_COMPONENT_PICTURE", 6),
                              (B.control("eqed", 1, B.record(B.EQEDIT, 2, struct.pack("<IH", 0, 9))), "EQEDIT", 5)):
        with pytest.raises(ParseError, match=f"{what} record too short") as info:
            section(B.paragraph(B.extended(11, "gso "), controls=[control]))
        assert info.value.location == f"a.hwp:BodyText/Section0#{at}"


def broken_textart(level: int, body: bytes, *children: bytes, top: bool = True) -> bytes:
    """글맵시 SHAPE_COMPONENT(level): children(레벨 level + 1) + 몸이 body인 SHAPE_COMPONENT_TEXTART."""
    return B.component(level, "$tat", *children, B.record(B.SC_TEXTART, level + 1, body), top=top)


def test_a_textart_whose_text_does_not_fit_or_is_not_plausible_takes_the_unknown_path_with_a_record():
    def art(text: str) -> bytes:
        return bytes(32) + B.hwp_string(text) + bytes(44)

    bodies = [bytes(33), bytes(32) + struct.pack("<H", 500) + "짧은 글".encode("utf-16-le"),  # 길이가 몸을 넘는다
              art("지어낸\ufffd글맵시"), bytes(32) + struct.pack("<HH", 2, 0xD800) + "글".encode("utf-16-le"),
              art("지어낸\x01글맵시"), art("지어낸\x0b글맵시")]
    for body in bodies:
        shape = broken_textart(2, body, B.listed(3, B.paragraph("글맵시 아래 글", level=3)))
        result = section(B.paragraph("앞", B.extended(11, "gso "), controls=[B.gso(1, shape)]))
        assert next(result.root.iter(f"{HP}textart"), None) is None
        rescued = result.root[0].find(f"{HP}run/{HP}ctrl/{HP}unknownObject")
        assert [tags(p)[1] for p in rescued.iter(f"{HP}p")] == ["글맵시 아래 글"]
        assert reasons(result) == [("record_inconsistent", "textart", 1), ("unknown_control", None, None)]
        assert result.audit.ok
    # 묶음 안의 깨진 글맵시: 그 개체만 빠지고 나머지 묶음은 그대로
    inside = B.gso(1, B.container(2, B.pic(3, 1, top=False), broken_textart(3, bodies[1], top=False)))
    result = section(B.paragraph(B.extended(11, "gso "), controls=[inside]))
    assert [local(e) for e in result.root[0].find(f"{HP}run/{HP}container")] == ["pic"]
    assert reasons(result) == [("record_inconsistent", "textart", 1), ("unknown_control", None, None)]
    assert result.audit.ok
    # 글맵시 개체 밖의 깨진 글맵시 레코드는 문서를 거부하지 않고 장부가 고아로 잡는다
    stray_art = B.gso(1, B.component(2, "$unk", B.record(B.SC_TEXTART, 3, bodies[1])))
    result = section(B.paragraph(B.extended(11, "gso "), controls=[stray_art]))
    assert result.audit == transcode.Audit(0, 1, 0)


def test_tab_and_line_breaks_in_a_textart_are_plausible_text_and_stay_in_the_ledger():
    text = "첫째 줄\n둘째\t줄\r\n셋째 줄"
    result = section(B.paragraph("앞", B.extended(11, "gso "), controls=[B.gso(1, B.textart(2, text))]))
    assert [e.get("text") for e in result.root.iter(f"{HP}textart")] == [text]
    assert reasons(result) == [] and result.audit.ok


def test_an_unknown_shape_nested_in_a_container_leaves_one_unknown_control_and_its_text_is_rescued_once():
    empty = B.gso(1, B.container(2, B.pic(3, 1, top=False), B.component(3, "$unk", top=False)))
    result = section(B.paragraph(B.extended(11, "gso "), controls=[empty]))
    assert [local(e) for e in result.root[0].find(f"{HP}run/{HP}container")] == ["pic"]
    assert reasons(result) == [("unknown_control", None, None)] and result.audit.ok
    two = B.gso(1, B.container(2, B.component(3, "$unk", top=False), B.component(3, "$unk", top=False),
                               B.component(3, "$unk", B.listed(4, B.paragraph("묶음 속 모르는 도형 글", level=4)),
                                           top=False)))
    result = section(B.paragraph(B.extended(11, "gso "), controls=[two]))
    assert reasons(result) == [("unknown_control", None, None)] and result.audit.ok
    assert [t.text for t in result.root.iter(f"{HP}t")] == ["묶음 속 모르는 도형 글"]


def test_a_readable_textart_under_a_second_component_is_skipped_with_the_record_not_an_orphan():
    both = B.gso(1, B.pic(2, 1) + B.textart(2, "둘째 글맵시"))
    result = section(B.paragraph("앞", B.extended(11, "gso "), controls=[both]))
    assert next(result.root.iter(f"{HP}textart"), None) is None
    assert [img.get("binaryItemIDRef") for img in result.root.iter(f"{container.HC}img")] == ["1"]
    assert reasons(result) == [("unknown_control", None, None)] and result.audit == transcode.Audit(0, 0, 0)


def test_shape_components_after_the_first_under_a_drawing_object_are_recorded_once_per_paragraph():
    two_pics = B.gso(1, B.pic(2, 1) + B.pic(2, 2))
    with_text = B.gso(1, B.pic(2, 3) + B.rect(2, B.paragraph("둘째 도형 글", level=3)))
    result = section(B.paragraph("앞", B.extended(11, "gso "), B.extended(11, "gso "), controls=[two_pics, with_text])
                     + B.paragraph(B.extended(11, "gso "), controls=[B.gso(1, B.pic(2, 4) + B.line(2))]))
    first, second = result.root
    assert [tags(e)[0] for e in first.find(f"{HP}run")][1:] == ["pic", "pic", "ctrl"]
    assert [img.get("binaryItemIDRef") for img in result.root.iter(f"{container.HC}img")] == ["1", "3", "4"]
    rescued = first.find(f"{HP}run/{HP}ctrl/{HP}unknownObject")
    assert [tags(p)[1] for p in rescued.iter(f"{HP}p")] == ["둘째 도형 글"]
    assert [tags(e)[0] for e in second.find(f"{HP}run")] == ["pic"]
    assert reasons(result) == [("unknown_control", None, None)] * 2
    assert [r.anchor for r in result.regions] == [0, 1] and result.audit.ok


def test_char_shape_bounds_after_the_text_fold_into_one_empty_run():
    many = [(0, 1)] + [(10 + k, k % 7) for k in range(5000)]
    result = section(B.paragraph("가나다", shapes=many))
    assert shape(result.root[0]) == [("1", [("가나다", [])]), ("0", [])]


TWO = (B.paragraph("첫째", B.TAB, "문단") + B.paragraph("둘째 문단")
       + B.paragraph("셋째", controls=[B.control("gso ", 1, B.paragraph("글상자", level=2))]))


REAL_AUDIT = transcode.audit


def audit_with(monkeypatch, mutate) -> transcode.Audit:
    """장부 판정 직전에 옮겨 쓴 트리를 고친다(결함 주입)."""

    def wrapped(records, root, marks, skipped, location):
        mutate(root)
        return REAL_AUDIT(records, root, marks, skipped, location)

    monkeypatch.setattr(transcode, "audit", wrapped)
    result = section(TWO)
    (region,) = [r for r in result.regions if r.reason == "coverage_mismatch"] or [None]
    if not result.audit.ok:
        assert region.region_id == "s0-transcode" and {c.name for c in region.gate.checks} == {
            "mismatched", "orphans", "stray"}
    return result.audit


def ts(root):
    return list(root.iter(f"{HP}t"))


def test_fault_injection_is_caught_record_by_record(monkeypatch):
    def drop_record(root):
        root.remove(root[1])

    def substitute(root):
        ts(root)[1].text = "둘째 문장"  # 같은 길이 치환

    def cancel(root):
        first, second = ts(root)[:2]
        first.text, second.text = "첫", second.text + "째"  # 한 글자를 빼고 다른 레코드에 하나 더한다(합계 같음)

    def drop_tab(root):
        t = ts(root)[0]
        tab = t[0]
        t.text += tab.tail
        t.remove(tab)

    def into_own_tail(root):
        t = ts(root)[1]
        t.tail, t.text = t.text, ""

    def tab_tail_is_fine(root):
        t = ts(root)[0]
        t.text, t[0].tail = "첫째", "문단"  # 이미 그 자리다(정상)

    assert audit_with(monkeypatch, drop_record) == transcode.Audit(0, 1, 0)
    assert audit_with(monkeypatch, substitute) == transcode.Audit(1, 0, 0)
    assert audit_with(monkeypatch, cancel) == transcode.Audit(2, 0, 0)
    assert audit_with(monkeypatch, drop_tab) == transcode.Audit(1, 0, 0)
    assert audit_with(monkeypatch, into_own_tail) == transcode.Audit(1, 0, 4)
    assert audit_with(monkeypatch, tab_tail_is_fine) == transcode.Audit(0, 0, 0)
    assert audit_with(monkeypatch, lambda root: None).ok


def test_element_budget_depth_limit_and_level_64_nesting(monkeypatch):
    nodes = Nodes()
    section(B.paragraph("가", B.LINE_BREAK * 10), nodes)
    assert nodes.count == 1 + 1 + 1 + 10 + 2  # 문단·런·t·줄바꿈 10·줄 배열과 줄 하나
    monkeypatch.setattr(transcode, "MAX_NODES", 20)
    with pytest.raises(ParseError, match="more than 20 transcoded elements in the document"):
        section(B.paragraph("가", B.LINE_BREAK * 30))
    monkeypatch.setattr(transcode, "MAX_NODES", 4_000_000)
    deep = B.list_header(64, 0)  # 가장 깊은 레코드가 레벨 64
    for level in range(62, -1, -2):  # 문단(L) → 컨트롤(L + 1) → 문단(L + 2)
        deep = B.paragraph(f"깊이 {level}", B.extended(11, "gso "), level=level,
                           controls=[B.control("gso ", level + 1, deep)])
    result = section(deep)
    assert result.audit.ok and len(list(result.root.iter(f"{HP}p"))) == 32
    monkeypatch.setattr(container, "MAX_XML_DEPTH", 100)
    with pytest.raises(ParseError, match="transcoded tree deeper than 100"):
        section(deep)
