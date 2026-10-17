"""DocInfo → Styles(HWP 스펙 §7.5): HWPX header.py와 같은 형·같은 id 규칙, 짧은 레코드는 ParseError."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwp.cfb import Compound
from hanji.formats.hwp.docinfo import flatten, read_styles
from hanji.formats.hwp.stream import Reader
from hanji.formats.hwpx import header as hwpx_header
from hanji.formats.hwpx.header import CharShape

HERE = Path(__file__).resolve().parent


def _load(name: str, module: str):
    spec = importlib.util.spec_from_file_location(module, HERE / name)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


B = _load("build_hwp_fixtures.py", "engine_build_hwp_fixtures")
HX = _load("build_hwpx_fixtures.py", "engine_build_hwpx_fixtures")


def styles(data: bytes):
    cfb = Compound(B.compound({"DocInfo": data}), "a.hwp")
    return read_styles(flatten(Reader(cfb, compressed=False).tree("DocInfo")), "a.hwp:DocInfo")


def test_ids_are_ordinals_with_heading_kinds_levels_sizes_bold_and_fills():
    data = (B.record(B.ID_MAPPINGS, 0, bytes(72))
            + B.record(B.BORDER_FILL, 1, B.border_fill("none")) + B.record(B.BORDER_FILL, 1, B.border_fill("#FFFFFF"))
            + B.record(B.BORDER_FILL, 1, B.border_fill("#D9D9D9")) + B.record(B.BORDER_FILL, 1, B.border_fill(kind=4))
            + B.record(B.BORDER_FILL, 1, B.border_fill(kind=2)) + B.record(B.BORDER_FILL, 1, B.border_fill(kind=0))
            + B.record(B.CHAR_SHAPE, 1, B.char_shape(1000)) + B.record(B.CHAR_SHAPE, 1, B.char_shape(1600, True, 80))
            + B.record(B.PARA_SHAPE, 1, B.para_shape()) + B.record(B.PARA_SHAPE, 1, B.para_shape("OUTLINE", 7))
            + B.record(B.PARA_SHAPE, 1, B.para_shape("NUMBER", 2)) + B.record(B.PARA_SHAPE, 1, B.para_shape("BULLET")))
    found = styles(data)
    assert found.headings == {"0": ("NONE", 0), "1": ("OUTLINE", 7), "2": ("NUMBER", 2), "3": ("BULLET", 0)}
    assert found.chars == {"0": CharShape(10.0, False), "1": CharShape(12.8, True)}
    assert found.fills == {"1": False, "2": False, "3": True, "4": True, "5": True, "6": False}


def test_docinfo_written_like_the_hwpx_header_reads_the_same_styles():
    for args in ({"headings": {1: ("OUTLINE", 0), 2: ("OUTLINE", 1), 3: ("OUTLINE", 7), 4: ("NUMBER", 0),
                               5: ("BULLET", 0)}},
                 {"chars": {1: (1600, False), 2: (1100, True), 3: (2000, False)}},
                 {"headings": {1: ("OUTLINE", 0)}, "fills": {2: "#D9D9D9"}}):
        assert styles(B.docinfo(**args)) == hwpx_header.read_styles(ET.fromstring(HX.header(**args)))


def test_docinfo_records_shorter_than_the_fields_read_are_refused():
    for tag, body, name in ((B.CHAR_SHAPE, B.char_shape()[:49], "CHAR_SHAPE"), (B.PARA_SHAPE, b"\0\0\0", "PARA_SHAPE"),
                            (B.BORDER_FILL, bytes(35), "BORDER_FILL"),
                            (B.BORDER_FILL, B.border_fill()[:39], "BORDER_FILL")):  # 단색이면 배경색까지 있어야 한다
        with pytest.raises(ParseError, match=f"DocInfo {name} record too short") as info:
            styles(B.record(B.ID_MAPPINGS, 0, bytes(72)) + B.record(tag, 1, body))
        assert info.value.location == "a.hwp:DocInfo#1"
    assert styles(B.record(B.BORDER_FILL, 0, bytes(36))).fills == {"1": False}  # 채우기 없음은 종류까지만 읽는다
