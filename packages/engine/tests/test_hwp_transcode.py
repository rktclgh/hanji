"""HWP 레코드 → OWPML 요소 트리 옮겨 쓰기(HWP 스펙 §7.1·§7.2, HP-A의 §7.3)와 옮겨 쓰기 장부(§6)·예산(§5.4)."""

import importlib.util
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


def test_unknown_record_tags_are_counted_per_section():
    result = section(B.paragraph("글") + B.record(0x3FF, 0, b"x") + B.record(0x200, 1))
    assert reasons(result) == [("unknown_record", "records", 2)]
    assert result.regions[0].region_id == "s0-unknown-record" and result.audit.ok


TWO = (B.paragraph("첫째", B.TAB, "문단") + B.paragraph("둘째 문단")
       + B.paragraph("셋째", controls=[B.control("gso ", 1, B.paragraph("글상자", level=2))]))


REAL_AUDIT = transcode.audit


def audit_with(monkeypatch, mutate) -> transcode.Audit:
    """장부 판정 직전에 옮겨 쓴 트리를 고친다(결함 주입)."""

    def wrapped(records, root, marks, skipped):
        mutate(root)
        return REAL_AUDIT(records, root, marks, skipped)

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
