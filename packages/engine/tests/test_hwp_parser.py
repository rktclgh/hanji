"""HWP 파서 연결(HWP 스펙 §3·§8·§9·§11.2): 공유 처리부와 두 장부, 형식 엿보기, 배포용 미리보기, 구역 차례, 등록."""

import importlib.util
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.detect import default_parsers, detect_parser
from hanji.formats.hwp import HwpParser, parser, transcode
from hanji.formats.hwpx import HwpxParser
from hanji_contracts import FlowLocator

HERE = Path(__file__).resolve().parent


def _load(name: str, module: str):
    spec = importlib.util.spec_from_file_location(module, HERE / name)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


B = _load("build_hwp_fixtures.py", "engine_build_hwp_fixtures")
HX = _load("build_hwpx_fixtures.py", "engine_build_hwpx_fixtures")
HEAD = B.docinfo(headings={1: ("OUTLINE", 0), 2: ("NUMBER", 0)}, sections=2)


def shown(parsed) -> list[tuple[str, str, float]]:
    return [(b["kind"], b["text"], b["confidence"]) for b in parsed.blocks]


def reasons(parsed) -> list[str]:
    return [r.fallback_reason for r in parsed.regions]


def test_a_compressed_document_reads_headings_lists_and_paragraphs_with_both_ledgers_clean():
    data = B.document(B.paragraph("지어낸 보고서 제목", pr=1) + B.paragraph("본문 ", B.TAB, "문단이다.")
                      + B.paragraph("번호 항목", pr=2), B.paragraph("둘째 구역 문단"), head=HEAD,
                      compressed=True)
    parsed, counts, audits = HwpParser()._parse(data, "a.hwp")
    assert parsed.mime == "application/x-hwp"
    assert shown(parsed) == [("heading", "지어낸 보고서 제목", 1.0), ("paragraph", "본문 \t문단이다.", 1.0),
                             ("list_item", "번호 항목", 1.0), ("paragraph", "둘째 구역 문단", 1.0)]
    assert parsed.blocks[3]["locator"] == FlowLocator(section_path=("지어낸 보고서 제목",), paragraph_index=3)
    assert parsed.regions == () and all(c.ok for c in counts) and all(a.ok for a in audits) and len(audits) == 2


def test_unknown_controls_rescue_their_text_at_section_end_and_both_ledgers_agree():
    table = B.control("tbl ", 1, B.list_header(2, 1), B.paragraph("칸 글", level=2))
    deep = B.list_header(64, 0)
    for level in range(62, 1, -2):  # 레벨 64까지 이어진 글상자 속 글상자
        deep = B.paragraph(f"깊이 {level}", B.extended(11, "gso "), level=level,
                           controls=[B.control("gso ", level + 1, deep)])
    data = B.document(B.paragraph("표 앞", B.extended(11, "tbl "), controls=[table]) + B.paragraph("다음 문단")
                      + B.paragraph(B.extended(11, "gso "), controls=[B.control("gso ", 1, deep)]))
    parsed, counts, audits = HwpParser()._parse(data, "a.hwp")
    assert shown(parsed)[:2] == [("paragraph", "표 앞", 1.0), ("paragraph", "다음 문단", 1.0)]
    assert shown(parsed)[2] == ("paragraph", "칸 글", 0.3)  # 닻 문단마다 구조 문단 하나
    assert len(shown(parsed)) == 4 and shown(parsed)[3][1].startswith("깊이 2\n깊이 4\n") and shown(parsed)[3][2] == 0.3
    assert reasons(parsed) == ["unknown_control", "unknown_control"] + ["unknown_control"] * 31 + ["unassigned_text"]
    assert counts[0].ok and counts[0].rescued > 0 and audits[0].ok


def test_a_transcode_mismatch_is_recorded_without_failing_the_document():
    data = B.document(B.paragraph("정상 문단") + B.record(B.PARA_TEXT, 0, "떠도는 글".encode("utf-16-le")))
    parsed, _, audits = HwpParser()._parse(data, "a.hwp")
    assert shown(parsed) == [("paragraph", "정상 문단", 1.0)]
    assert audits[0] == transcode.Audit(0, 1, 0)
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id) == ("coverage_mismatch", "s0-transcode")
    assert {c.name: (c.value, c.passed) for c in region.gate.checks} == {
        "mismatched": (0, True), "orphans": (1, False), "stray": (0, True)}


def test_bytes_are_sniffed_so_a_zip_hwp_and_an_ole_hwpx_read_as_what_they_are():
    hwpx = HX.package(HX.section(HX.para(HX.run(HX.t("zip인 한글 문서")))))
    hwp = B.document(B.paragraph("OLE인 한글 문서"))
    assert HwpParser().parse(hwpx, "a.hwp").mime == "application/hwp+zip"
    assert shown(HwpParser().parse(hwpx, "a.hwp")) == [("paragraph", "zip인 한글 문서", 1.0)]
    assert HwpxParser().parse(hwp, "a.hwpx").mime == "application/x-hwp"
    assert shown(HwpxParser().parse(hwp, "a.hwpx")) == [("paragraph", "OLE인 한글 문서", 1.0)]
    old = b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + bytes(100)
    for reader, name in ((HwpParser(), "a.hwp"), (HwpxParser(), "a.hwpx")):
        with pytest.raises(ParseError, match="HWP 3.x documents are not supported"):
            reader.parse(old, name)
    with pytest.raises(ParseError, match="CFB signature"):
        HwpParser().parse(b"plain text, not a document", "a.hwp")
    with pytest.raises(ParseError, match="not an HWPX"):
        HwpxParser().parse(b"plain text, not a document", "a.hwpx")


def test_distribution_documents_give_preview_paragraphs_only_without_reading_the_body(monkeypatch):
    preview = "첫 줄 미리보기\r\n\r\n  \n둘째 줄\n".encode("utf-16-le")
    broken_body = b"\xff" * 10  # 읽으면 잘린 레코드다
    data = B.document(broken_body, flags=0x4, extra={"PrvText": preview, "ViewText/Section0": broken_body})
    parsed = HwpParser().parse(data, "a.hwp")
    assert shown(parsed) == [("paragraph", "첫 줄 미리보기", 0.3), ("paragraph", "둘째 줄", 0.3)]
    assert [b["locator"].paragraph_index for b in parsed.blocks] == [0, 3]
    (region,) = parsed.regions
    assert (region.fallback_reason, region.region_id, region.gate.checks[0].name, region.gate.checks[0].value) == (
        "distribution_preview_only", "distribution", "preview_chars", 9)
    assert [(c.name, c.value, c.passed, c.threshold) for c in region.gate.checks] == [
        ("preview_chars", 9, True, None), ("truncated", 0, True, None)]
    odd = HwpParser().parse(B.document(broken_body, flags=0x4, extra={"PrvText": preview[:-1]}), "a.hwp")
    assert reasons(odd) == ["distribution_preview_only", "invalid_utf16"]
    assert shown(odd) == shown(parsed)  # 홀수 바이트는 버린다(U+FFFD 줄을 만들지 않는다)
    assert [(c.name, c.value) for c in odd.regions[1].gate.checks] == [("chars", 1)]
    monkeypatch.setattr(parser, "PRVTEXT_BYTES", 8)  # 앞부분만
    assert shown(HwpParser().parse(data, "a.hwp")) == [("paragraph", "첫 줄", 0.3)]
    for extra in ({}, {"PrvText": " \r\n".encode("utf-16-le")}):
        with pytest.raises(ParseError, match="preview text unavailable") as info:
            HwpParser().parse(B.document(broken_body, flags=0x4, extra=extra), "a.hwp")
        assert info.value.location == "a.hwp:PrvText"
    with pytest.raises(ParseError, match="DocInfo CHAR_SHAPE record too short"):  # DocInfo는 그래도 읽는다
        HwpParser().parse(B.document(head=B.record(B.CHAR_SHAPE, 0, bytes(10)), flags=0x4,
                                     extra={"PrvText": preview}), "a.hwp")


def _preview(raw: bytes):
    return HwpParser().parse(B.document(b"\xff" * 10, flags=0x4, extra={"PrvText": raw}), "a.hwp")


def _texts(parsed) -> list[str]:
    return [b["text"] for b in parsed.blocks]


def test_a_distribution_preview_drops_an_odd_last_byte_without_a_replacement_char_block():
    parsed = _preview("가나\n다라\n".encode("utf-16-le") + b"\x41")  # 마지막 줄바꿈 뒤 홀수 바이트
    assert _texts(parsed) == ["가나", "다라"] and reasons(parsed) == ["distribution_preview_only", "invalid_utf16"]
    assert [(c.name, c.value) for c in parsed.regions[1].gate.checks] == [("chars", 1)]
    assert all("�" not in text for text in _texts(parsed))


def test_a_distribution_preview_with_nothing_but_replacement_chars_or_spaces_is_an_error():
    for raw in (" \r\n".encode("utf-16-le") + b"\x41", b"\x41", "\ud800".encode("utf-16-le", "surrogatepass"),
                " �\n�".encode("utf-16-le")):
        with pytest.raises(ParseError, match="preview text unavailable") as info:
            _preview(raw)
        assert info.value.location == "a.hwp:PrvText"


def test_a_distribution_preview_keeps_real_text_around_replacement_chars_and_drops_only_replacement_lines():
    raw = "가\ud800나\n�\n \ufffd \n다".encode("utf-16-le", "surrogatepass")
    parsed = _preview(raw)
    assert _texts(parsed) == ["가�나", "다"] and reasons(parsed) == ["distribution_preview_only", "invalid_utf16"]
    assert [(c.name, c.value) for c in parsed.regions[1].gate.checks] == [("chars", 1)]  # 짝 없는 대리 문자만 센다


def test_a_distribution_preview_cut_inside_a_surrogate_pair_is_truncation_not_invalid_utf16(monkeypatch):
    raw = "가나😀다".encode("utf-16-le")  # 가(2) 나(2) 😀(4: 높은+낮은) 다(2)
    for limit in (6, 7):  # 6: 높은 대리 문자에서 잘림, 7: 거기에 홀수 바이트까지
        monkeypatch.setattr(parser, "PRVTEXT_BYTES", limit)
        parsed = _preview(raw)
        assert _texts(parsed) == ["가나"] and reasons(parsed) == ["distribution_preview_only"]
        assert [(c.name, c.value, c.passed) for c in parsed.regions[0].gate.checks] == [
            ("preview_chars", 2, True), ("truncated", 1, True)]
    monkeypatch.setattr(parser, "PRVTEXT_BYTES", 8)  # 쌍이 온전히 들어간다
    whole = _preview(raw)
    assert _texts(whole) == ["가나😀"] and whole.regions[0].gate.checks[1].value == 1
    monkeypatch.setattr(parser, "PRVTEXT_BYTES", len(raw))  # 딱 맞으면 잘린 게 아니다
    exact = _preview(raw)
    assert _texts(exact) == ["가나😀다"] and exact.regions[0].gate.checks[1].value == 0


def test_a_lone_high_surrogate_at_the_end_of_an_untruncated_preview_is_still_invalid_utf16():
    parsed = _preview("가나\ud83d".encode("utf-16-le", "surrogatepass"))
    assert _texts(parsed) == ["가나�"] and reasons(parsed) == ["distribution_preview_only", "invalid_utf16"]
    assert parsed.regions[0].gate.checks[1].value == 0


def test_the_signature_copies_in_hwpx_parser_equal_the_hwp_ones():
    from hanji.formats.hwp import cfb, stream
    from hanji.formats.hwpx import parser as hwpx_parser

    assert (hwpx_parser.OLE_SIGNATURE, hwpx_parser.HWP3_SIGNATURE) == (cfb.SIGNATURE, stream.HWP3_SIGNATURE)


def test_sections_are_numbered_from_zero_without_gaps_and_docinfo_is_required():
    streams = {"FileHeader": B.file_header(), "DocInfo": B.docinfo(), "BodyText/Section0": B.paragraph("가"),
               "BodyText/Section2": B.paragraph("나")}
    with pytest.raises(ParseError, match="not numbered 0..n-1"):
        HwpParser().parse(B.compound(streams), "a.hwp")
    del streams["BodyText/Section2"]
    with pytest.raises(ParseError, match="unexpected stream under BodyText") as info:
        HwpParser().parse(B.compound({**streams, "BodyText/Section01": b""}), "a.hwp")
    assert info.value.location == "a.hwp:BodyText/Section01"
    with pytest.raises(ParseError, match="no BodyText sections"):
        HwpParser().parse(B.compound({"FileHeader": B.file_header(), "DocInfo": B.docinfo()}), "a.hwp")
    with pytest.raises(ParseError, match="missing DocInfo stream"):
        HwpParser().parse(B.compound({"FileHeader": B.file_header(), "BodyText/Section0": b""}), "a.hwp")


def test_hwp_is_a_default_parser_chosen_by_extension_case_insensitive():
    parsers = default_parsers(ocr=False, layout=False)
    assert isinstance(detect_parser("보고서.HWP", parsers), HwpParser)
    assert isinstance(detect_parser("보고서.hwpx", parsers), HwpxParser)
    assert HwpParser.mimes == ("application/x-hwp",) and HwpParser.extensions == (".hwp",)
