"""OLE 복합 파일 안전 읽기(HWP 스펙 §4): 모든 거부 경로가 ParseError(원인, 위치)이고 순환·공유·범위 밖이 없다."""

import importlib.util
import struct
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwp import cfb
from hanji.formats.hwp.cfb import Compound

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwp_fixtures", HERE / "build_hwp_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
STREAMS = {"FileHeader": b"h" * 256, "DocInfo": b"d" * 100, "PrvText": b"p" * 130,
           "BodyText/Section0": b"a" * 5000, "BodyText/Section1": b"b" * 5000}


def entry(layout, path):
    return next(e for e in layout.entries if e["path"] == path)


def refused(match, tamper=None, streams=STREAMS, data=None):
    with pytest.raises(ParseError, match=match) as info:
        Compound(data if data is not None else B.compound(streams, tamper=tamper), "a.hwp")
    return info.value


def test_a_valid_file_lists_exact_paths_shares_big_sectors_between_mini_streams_and_reads_up_to_the_limit():
    for major in (3, 4):
        found = Compound(B.compound(STREAMS, major=major), "a.hwp")
        assert found.streams == {k: len(v) for k, v in STREAMS.items()}
        assert all(found.read(k, 1 << 20) == v for k, v in STREAMS.items())
        assert found.read("BodyText/Section0", 10) == b"a" * 11 and found.read("PrvText", 64) == b"p" * 65
    # 작은 스트림 셋(256·100·130B → mini 섹터 4·2·3개, 64B마다)이 큰 섹터 둘을 나눠 쓴다(정상)
    layout = []
    B.compound(STREAMS, tamper=layout.append)
    assert layout[0].places["mini"][1] == 2 and len(layout[0].minifat) == 9
    assert not found.has("bodytext/section0") and found.has("BodyText/Section0")
    with pytest.raises(ParseError, match="missing CFB stream") as info:
        found.read("BodyText/Section9", 10)
    assert info.value.location == "a.hwp:BodyText/Section9"


def test_header_signature_version_shifts_and_cutoff_are_checked():
    good = B.compound(STREAMS)
    error = refused("CFB signature", data=b"PK\x03\x04" + good[4:])
    assert error.location == "a.hwp"
    refused("CFB signature", data=good[:300])
    for key, value, match in (("order", 0xFEFF, "byte order"), ("major", 2, "version 2"), ("major", 5, "version 5"),
                              ("shift", 12, "sector shift 12"), ("mini_shift", 7, "mini sector shift"),
                              ("cutoff", 8192, "cutoff")):
        refused(match, lambda layout, key=key, value=value: layout.header.update({key: value}))


def test_fat_chains_out_of_range_cyclic_or_shorter_than_declared_are_refused():
    def cycle(layout):
        first, _ = layout.places["BodyText/Section0"]
        layout.fat[first + 1] = first

    error = refused("used twice", cycle)
    assert error.location == "a.hwp:BodyText/Section0"
    refused("out of range", lambda layout: layout.fat.__setitem__(layout.places["BodyText/Section1"][0], 10**6))
    refused("out of range", lambda layout: entry(layout, "BodyText/Section1").update(start=4000))
    refused("ends before its declared size", lambda layout: entry(layout, "BodyText/Section0").update(size=9000))
    refused("ends before its declared size", lambda layout: entry(layout, "DocInfo").update(size=4000))


def test_two_chains_sharing_a_big_sector_or_a_mini_sector_are_refused():
    def share(layout):
        entry(layout, "BodyText/Section1")["start"] = entry(layout, "BodyText/Section0")["start"]

    refused("sector is used twice", share)
    error = refused("mini sector is used twice", lambda layout: entry(layout, "PrvText").update(
        start=entry(layout, "DocInfo")["start"]))
    assert error.location == "a.hwp:PrvText"
    refused("mini sector out of range", lambda layout: entry(layout, "PrvText").update(start=500))
    refused("used twice", lambda layout: layout.entries[0].update(start=layout.places["BodyText/Section0"][0]))


def test_difat_and_fat_declarations_must_match_the_file():
    refused("DIFAT does not list exactly", lambda layout: layout.header.update(nfat=2))
    refused("used twice", lambda layout: layout.header.update(nfat=2, difat=[0, 0] + [B.FREESECT] * 107))
    refused("more FAT or DIFAT sectors than the file has", lambda layout: layout.header.update(ndifat=10**6))
    refused("not marked as such", lambda layout: layout.fat.__setitem__(0, B.ENDOFCHAIN))

    def difat_cycle(layout):
        s = len(layout.sectors)
        layout.sectors.append(bytearray(struct.pack("<127I", *[B.FREESECT] * 127) + struct.pack("<I", s)))
        layout.fat.append(B.DIFSECT)
        layout.header.update(difat_first=s, ndifat=2)

    refused("DIFAT sector is used twice", difat_cycle)

    def difat_longer(layout):
        s = len(layout.sectors)
        layout.sectors.append(bytearray(struct.pack("<128I", *[B.FREESECT] * 127, 0)))
        layout.fat.append(B.DIFSECT)
        layout.header.update(difat_first=s, ndifat=1)

    refused("longer than the header declares", difat_longer)


def test_directory_tree_cycles_missing_unused_and_childful_stream_entries_are_refused():
    def index(layout, path):
        return layout.entries.index(entry(layout, path))

    refused("cycle", lambda layout: entry(layout, "PrvText").update(right=index(layout, "PrvText")))
    refused("cycle", lambda layout: entry(layout, "BodyText/Section1").update(right=index(layout, "BodyText")))
    refused("missing entry", lambda layout: entry(layout, "PrvText").update(right=999))
    refused("unused entry", lambda layout: (layout.entries.append(
        {"path": "-", "name": "", "kind": 0, "left": B.NOSTREAM, "right": B.NOSTREAM, "child": B.NOSTREAM,
         "start": 0, "size": 0}), entry(layout, "PrvText").update(right=len(layout.entries) - 1)))
    error = refused("stream entry has children", lambda layout: entry(layout, "PrvText").update(child=1))
    assert error.location == "a.hwp:PrvText"
    refused("exactly one root", lambda layout: entry(layout, "PrvText").update(kind=5))
    refused("unknown CFB directory entry type 3", lambda layout: entry(layout, "PrvText").update(kind=3))


def test_entry_names_are_checked_for_length_nul_forbidden_characters_and_case_folded_duplicates():
    refused("name length", lambda layout: entry(layout, "PrvText").update(length=5))
    refused("name length", lambda layout: entry(layout, "PrvText").update(length=8))  # 끝 NUL 자리가 글자다
    refused("name length", lambda layout: entry(layout, "PrvText").update(name="가" * 33))
    refused("forbidden character", lambda layout: entry(layout, "PrvText").update(name="Prv/Text"))
    refused("forbidden character", lambda layout: entry(layout, "PrvText").update(name="Prv:Text"))
    error = refused("duplicate CFB entry name", streams={**STREAMS, "DOCINFO": b"x"})
    assert error.location in ("a.hwp:DocInfo", "a.hwp:DOCINFO")
    refused("duplicate CFB entry name", lambda layout: entry(layout, "BodyText/Section1").update(name="Section0"))


def test_too_many_directory_entries_are_refused_before_reading_them(monkeypatch):
    monkeypatch.setattr(cfb, "MAX_DIR_ENTRIES", 4)
    refused("more than 4 CFB directory entries")
