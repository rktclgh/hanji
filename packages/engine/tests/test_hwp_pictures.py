"""BinData → 그림 원천(HWP 스펙 §7.4): BIN_DATA 순번·종류·압축, 스트림 이름, HWPX와 같은 읽기 상한과 예외."""

import importlib.util
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwp.cfb import Compound
from hanji.formats.hwp.docinfo import flatten
from hanji.formats.hwp.pictures import BinData
from hanji.formats.hwp.stream import Reader
from hanji.formats.hwpx import container
from hanji.formats.hwpx.container import Item, OverBudget, TooLarge

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwp_fixtures", HERE / "build_hwp_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
PNG = bytes(range(256)) * 20  # 그림 바이트 자리(디코드하지 않는다)


def pictures(bins, streams, compressed=False) -> BinData:
    data = B.compound({"DocInfo": B.docinfo(bins=bins), **streams})
    cfb = Compound(data, "a.hwp")
    return BinData(cfb, flatten(Reader(cfb, compressed=False).tree("DocInfo")), compressed, "a.hwp:DocInfo")


def test_items_are_bin_data_ordinals_with_kind_and_the_stream_named_by_storage_id_and_extension():
    found = pictures([B.bin_data(3, "png"), B.bin_data(kind=B.LINK), B.bin_data(1, "jpg"), B.bin_data(2, "PNG"),
                      B.bin_data(4, kind=B.STORAGE), B.bin_data(5, "bmp")],
                     {"BinData/BIN0003.png": PNG, "BinData/BIN0001.jpg": b"j", "BinData/BIN0002.png": b"p"})
    assert found.items == {"1": Item("BinData/BIN0003.png", "", True), "2": Item("", "", False),
                           "3": Item("BinData/BIN0001.jpg", "", True), "4": Item("BinData/BIN0002.png", "", True),
                           "5": Item("", "", True), "6": Item("BinData/BIN0005.bmp", "", True)}
    # 확장자 대소문자만 다른 스트림 하나는 그것을 쓴다. 없는 스트림·연결·저장소는 has가 거짓
    assert [found.has(found.items[k].href) for k in "123456"] == [True, False, True, True, False, False]
    assert found.binary("BinData/BIN0003.png") == PNG and found.read == len(PNG)


def test_compression_follows_the_document_unless_the_item_says_always_or_never():
    packed = B.deflate(PNG)
    streams = {"BinData/BIN0001.png": packed, "BinData/BIN0002.png": PNG, "BinData/BIN0003.png": packed}
    bins = [B.bin_data(1, mode=0), B.bin_data(2, mode=2), B.bin_data(3, mode=1)]
    on = pictures(bins, streams, compressed=True)
    assert [on.binary(f"BinData/BIN000{k}.png") for k in (1, 2, 3)] == [PNG, PNG, PNG]
    off = pictures(bins, streams, compressed=False)
    assert off.binary("BinData/BIN0001.png") == packed and off.binary("BinData/BIN0003.png") == PNG


def test_picture_limits_and_broken_streams_raise_the_hwpx_exceptions(monkeypatch):
    streams = {"BinData/BIN0001.png": PNG, "BinData/BIN0002.png": PNG, "BinData/BIN0003.png": B.deflate(PNG)[:-9],
               "BinData/BIN0004.png": b"not deflate at all", "BinData/BIN0005.png": B.deflate(bytes(3 * 2**20))}
    bins = [B.bin_data(1, mode=2), B.bin_data(2, mode=2), B.bin_data(3, mode=1), B.bin_data(4, mode=1),
            B.bin_data(5, mode=1)]
    with pytest.raises(ParseError, match="truncated compressed picture stream") as info:
        pictures(bins, streams).binary("BinData/BIN0003.png")
    assert info.value.location == "a.hwp:BinData/BIN0003.png"
    with pytest.raises(ParseError, match="invalid compressed picture stream"):
        pictures(bins, streams).binary("BinData/BIN0004.png")
    with pytest.raises(TooLarge, match="compression ratio over 200"):  # 3 MiB가 약 3KB에서 풀린다
        pictures(bins, streams).binary("BinData/BIN0005.png")
    monkeypatch.setattr(container, "MAX_BINARY_BYTES", len(PNG) - 1)
    with pytest.raises(TooLarge, match=f"larger than {len(PNG) - 1} bytes"):
        pictures(bins, streams).binary("BinData/BIN0001.png")
    monkeypatch.setattr(container, "MAX_BINARY_BYTES", 64 * 2**20)
    monkeypatch.setattr(container, "MAX_BINARY_TOTAL", len(PNG) + 10)
    budget = pictures(bins, streams)
    assert budget.binary("BinData/BIN0001.png") == PNG
    with pytest.raises(OverBudget):
        budget.binary("BinData/BIN0002.png")  # 남은 몫 10 + 1바이트만 읽는다
    assert budget.read == len(PNG) + 11
    with pytest.raises(OverBudget):
        budget.binary("BinData/BIN0001.png")  # 몫을 다 쓴 뒤에는 열지 않는다


def test_short_bin_data_records_are_refused():
    for body in (b"\x01", B.bin_data(1, "png")[:5], B.bin_data(1, "png")[:9]):
        with pytest.raises(ParseError, match="DocInfo BIN_DATA record too short") as info:
            pictures([body], {})
        assert info.value.location == "a.hwp:DocInfo#2"
