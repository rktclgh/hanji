"""HWP FileHeader·압축 풀기·레코드(HWP 스펙 §5): 모든 거부 경로가 ParseError이고 예산은 문서 전체다."""

import importlib.util
import struct
import time
import zlib
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwp import stream
from hanji.formats.hwp.cfb import Compound
from hanji.formats.hwp.stream import Reader, file_header

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwp_fixtures", HERE / "build_hwp_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
PARA = 0x10 + 50


def cfb(**streams: bytes) -> Compound:
    return Compound(B.compound({k.replace("__", "/"): v for k, v in streams.items()}), "a.hwp")


def header(**kwargs) -> object:
    return file_header(cfb(FileHeader=B.file_header(**kwargs)))


def test_file_header_signature_version_and_flags():
    assert header().version == (5, 1, 0, 1) and not header().compressed
    found = header(version=(5, 0, 3, 4), flags=0x1 | 0x4 | 0x8 | 0x4000)
    assert found.compressed and found.distribution
    with pytest.raises(ParseError, match="missing FileHeader") as info:
        file_header(cfb(DocInfo=b""))
    assert info.value.location == "a.hwp"
    with pytest.raises(ParseError, match="FileHeader signature") as info:
        file_header(cfb(FileHeader=b"HWP Document Fil\0" + bytes(239)))
    assert info.value.location == "a.hwp:FileHeader"
    with pytest.raises(ParseError, match="FileHeader signature"):
        file_header(cfb(FileHeader=B.file_header()[:39]))
    for version in ((3, 0, 0, 0), (6, 0, 0, 0)):
        with pytest.raises(ParseError, match=f"unsupported HWP version {version[0]}.0.0.0"):
            header(version=version)
    with pytest.raises(ParseError, match="password-protected"):
        header(flags=0x3)
    for bit in (4, 8, 10):
        with pytest.raises(ParseError, match="DRM-protected"):
            header(flags=1 << bit)


def test_compressed_streams_are_raw_deflate_and_trailing_bytes_are_ignored():
    body = B.record(PARA, 0, b"x" * 30) * 3
    reader = Reader(cfb(DocInfo=B.deflate(body) + bytes(range(8))), compressed=True)
    assert reader.data("DocInfo") == body and reader.read == len(body)
    with pytest.raises(ParseError, match="invalid compressed stream") as info:
        Reader(cfb(DocInfo=zlib.compress(body)), compressed=True).data("DocInfo")  # zlib 머리가 있으면 raw가 아니다
    assert info.value.location == "a.hwp:DocInfo"
    with pytest.raises(ParseError, match="truncated compressed stream"):
        Reader(cfb(DocInfo=B.deflate(body)[:-3]), compressed=True).data("DocInfo")
    assert Reader(cfb(DocInfo=body), compressed=False).data("DocInfo") == body


def test_a_decompression_bomb_stops_at_the_limit_plus_one_within_a_second(monkeypatch):
    bomb = B.deflate(bytes(80 * 1024 * 1024))
    asked = []
    real = zlib.decompressobj

    class Spy:
        def __init__(self, wbits):
            self.inner = real(wbits)

        def decompress(self, data, max_length=0):
            asked.append(max_length)
            return self.inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(stream.zlib, "decompressobj", Spy)
    start = time.perf_counter()
    with pytest.raises(ParseError, match="HWP stream larger than 67108864 bytes"):
        Reader(cfb(BodyText__Section0=bomb), compressed=True).data("BodyText/Section0")
    assert time.perf_counter() - start < 1.0 and asked == [64 * 1024 * 1024 + 1]


def test_stream_limits_apply_to_each_stream_and_to_the_document_total(monkeypatch):
    body = B.record(PARA, 0, b"y" * 96)  # 100B
    monkeypatch.setattr(stream, "MAX_STREAM_BYTES", 99)
    with pytest.raises(ParseError, match="larger than 99 bytes"):
        Reader(cfb(DocInfo=body), compressed=False).data("DocInfo")
    with pytest.raises(ParseError, match="larger than 99 bytes"):
        Reader(cfb(DocInfo=B.deflate(body)), compressed=True).data("DocInfo")
    monkeypatch.setattr(stream, "MAX_STREAM_BYTES", 100)
    monkeypatch.setattr(stream, "MAX_STREAM_TOTAL", 150)
    reader = Reader(cfb(DocInfo=body, BodyText__Section0=body), compressed=False)
    reader.data("DocInfo")
    with pytest.raises(ParseError, match="larger than 150 bytes in total") as info:
        reader.data("BodyText/Section0")
    assert info.value.location == "a.hwp:BodyText/Section0"


def test_records_form_a_tree_by_level_with_extended_sizes():
    data = (B.record(PARA, 0, b"a") + B.record(PARA + 1, 1, b"b" * 5000) + B.record(PARA + 5, 1)
            + B.record(PARA + 6, 2, b"c") + B.record(PARA, 3) + B.record(PARA, 0, b"d"))
    top = Reader(cfb(DocInfo=data), compressed=False).tree("DocInfo")
    assert [(r.tag, r.index, len(r.body)) for r in top] == [(PARA, 0, 1), (PARA, 5, 1)]
    assert [(r.tag, r.level, r.index, len(r.body)) for r in top[0].children] == [
        (PARA + 1, 1, 1, 5000), (PARA + 5, 1, 2, 0)]
    (inner,) = top[0].children[1].children
    assert (inner.index, inner.body, inner.children[0].index) == (3, b"c", 4)
    assert Reader(cfb(DocInfo=b""), compressed=False).tree("DocInfo") == []


def test_truncated_records_level_jumps_and_record_and_level_budgets_are_refused(monkeypatch):
    def tree(data: bytes, **more: bytes) -> list:
        reader = Reader(cfb(DocInfo=data, **more), compressed=False)
        reader.tree("DocInfo")
        return reader.tree("BodyText/Section0") if more else []

    good = B.record(PARA, 0, b"abc")
    extended = struct.pack("<I", PARA | 0xFFF << 20)  # 0xFFF 크기 뒤 DWORD가 모자란다
    for data, match, where in ((good + b"\x01\x02", "truncated record header", "#1"),
                               (good + B.record(PARA, 0, b"abcd")[:-1], "truncated record", "#1"),
                               (good + extended + b"\x10", "truncated record header", "#1"),
                               (good + B.record(PARA, 2), "record level jump", "#1"),
                               (B.record(PARA, 1), "record level jump", "#0")):
        with pytest.raises(ParseError, match=match) as info:
            tree(data)
        assert info.value.location == f"a.hwp:DocInfo{where}"
    deep = b"".join(B.record(PARA, level) for level in range(65))
    tree(deep)  # 레벨 64까지는 정상
    with pytest.raises(ParseError, match="record level deeper than 64"):
        tree(deep + B.record(PARA, 65))
    monkeypatch.setattr(stream, "MAX_RECORDS", 5)
    with pytest.raises(ParseError, match="more than 5 records in the document") as info:
        tree(good * 3, BodyText__Section0=good * 3)  # 구역 둘에 나눠 넘긴다
    assert info.value.location == "a.hwp:BodyText/Section0#2"
