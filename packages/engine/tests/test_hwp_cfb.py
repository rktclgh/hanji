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

    def past_fat(layout):  # 파일엔 있지만 FAT 칸이 없는 섹터(FAT 한 섹터는 128칸)
        layout.sectors += [bytearray(layout.sector) for _ in range(120)]
        entry(layout, "BodyText/Section1").update(start=len(layout.sectors) - 1)

    refused("has no FAT entry", past_fat)


def test_two_chains_sharing_a_big_sector_or_a_mini_sector_are_refused():
    def share(layout):
        entry(layout, "BodyText/Section1")["start"] = entry(layout, "BodyText/Section0")["start"]

    refused("sector is used twice", share)
    error = refused("mini sector is used twice", lambda layout: entry(layout, "PrvText").update(
        start=entry(layout, "DocInfo")["start"]))
    assert error.location == "a.hwp:PrvText"
    refused("mini sector out of range", lambda layout: entry(layout, "PrvText").update(start=500))
    # 뿌리(mini 스트림) 사슬이 큰 스트림과 섹터를 나눈다(크기를 사슬 길이에 맞춰야 '너무 김'보다 '공유'가 먼저 보인다)
    refused("used twice", lambda layout: layout.entries[0].update(
        start=layout.places["BodyText/Section0"][0], size=5000))


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

    def difat_unmarked(layout):  # FAT에 DIFSECT 표시가 없다(FAT 배열 뒤는 FREESECT로 채워진다)
        layout.sectors.append(bytearray(struct.pack("<128I", *[B.FREESECT] * 127, B.ENDOFCHAIN)))
        layout.header.update(difat_first=len(layout.sectors) - 1, ndifat=1)

    refused("DIFAT sector is not marked", difat_unmarked)

    def difat_cut(layout):  # 마지막 섹터(DIFAT)가 파일 끝에서 잘렸다
        layout.sectors.append(bytearray(struct.pack("<50I", *[B.FREESECT] * 50)))
        layout.fat.append(B.DIFSECT)
        layout.header.update(difat_first=len(layout.sectors) - 1, ndifat=1)

    refused("DIFAT sector is truncated", difat_cut)


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
    refused("forbidden character", lambda layout: entry(layout, "PrvText").update(name="Prv\\Text"))
    refused("forbidden character", lambda layout: entry(layout, "PrvText").update(name="Prv!Text"))
    refused("forbidden character", lambda layout: entry(layout, "PrvText").update(name="Prv\0Text"))  # 글자 사이 NUL
    error = refused("duplicate CFB entry name", streams={**STREAMS, "DOCINFO": b"x"})
    assert error.location in ("a.hwp:DocInfo", "a.hwp:DOCINFO")
    refused("duplicate CFB entry name", lambda layout: entry(layout, "BodyText/Section1").update(name="Section0"))


def test_an_entry_name_that_is_not_valid_utf16_is_refused():
    layout = []
    data = bytearray(B.compound(STREAMS, tamper=layout.append))
    at = (layout[0].places["dir"][0] + 1) * 512 + layout[0].entries.index(entry(layout[0], "PrvText")) * 128
    data[at:at + 2] = b"\x00\xd8"  # 짝 없는 앞 대리 글자(길이·끝 NUL은 그대로)
    refused(r"invalid CFB entry name \(a\.hwp\)", data=bytes(data))


def test_too_many_directory_entries_are_refused_before_reading_them(monkeypatch):
    monkeypatch.setattr(cfb, "MAX_DIR_ENTRIES", 4)
    refused("more than 4 CFB directory entries")


def test_the_mini_fat_must_have_exactly_the_declared_sectors_and_end_right_after_them():
    refused("mini FAT sector count does not match", lambda layout: layout.header.update(
        nminifat=layout.places["minifat"][1] + 1))
    # 작은 스트림 셋(63개씩 → mini 섹터 189개)이 mini FAT 섹터 둘에 걸친다: 머리가 하나만 말하면 거부(자르지 않는다)
    wide = {k: bytes([97 + n]) * 4000 for n, k in enumerate("ABC")}
    assert Compound(B.compound(wide), "a.hwp").read("C", 1 << 20) == wide["C"]
    refused("mini FAT chain is longer than the header declares", streams=wide,
            tamper=lambda layout: layout.header.update(nminifat=1))


def test_a_stream_cut_by_the_end_of_the_file_is_refused_when_read_in_both_the_big_and_the_mini_path():
    # 큰 스트림: 마지막 섹터가 파일 끝에서 잘렸다(열기는 되고, 잘린 자리를 읽는 read만 거부)
    big = B.compound(STREAMS)[:-150]
    found = Compound(big, "a.hwp")
    assert found.read("BodyText/Section1", 10) == b"b" * 11 and found.read("BodyText/Section0", 1 << 20) == b"a" * 5000
    with pytest.raises(ParseError, match="stream is truncated") as info:
        found.read("BodyText/Section1", 1 << 20)
    assert info.value.location == "a.hwp:BodyText/Section1"
    with pytest.raises(ParseError, match="stream is truncated"):
        found.read("BodyText/Section1", 4990)  # 상한 + 1이 잘린 자리를 덮는다
    # mini 스트림: mini 스트림의 마지막 큰 섹터가 잘렸다(작은 스트림만 있으면 mini 스트림이 파일 끝이다)
    small = {k: v for k, v in STREAMS.items() if len(v) < 4096}
    found = Compound(B.compound(small)[:-511], "a.hwp")
    assert found.read("DocInfo", 1 << 20) == STREAMS["DocInfo"] and found.read("PrvText", 5) == b"p" * 6
    with pytest.raises(ParseError, match="stream is truncated") as info:
        found.read("PrvText", 1 << 20)
    assert info.value.location == "a.hwp:PrvText"


def test_a_chain_longer_than_the_declared_size_must_end_right_after_the_needed_sectors():
    # 선언 크기를 줄여 사슬이 남는다(큰 섹터 사슬 둘째 칸이 끝이 아님): 큰·mini·mini 스트림(뿌리) 모두 거부
    error = refused("longer than its declared size",
                    lambda layout: entry(layout, "BodyText/Section0").update(size=4200))
    assert error.location == "a.hwp:BodyText/Section0"
    error = refused("mini chain is longer than its declared size",
                    lambda layout: entry(layout, "DocInfo").update(size=64))
    assert error.location == "a.hwp:DocInfo"
    refused("mini stream chain is longer than its declared size",
            lambda layout: layout.entries[0].update(size=500))

    # 사슬이 다른 스트림의 섹터로 이어져도 먼저 '끝이 아님'으로 거부한다(큰 사슬은 필요한 만큼만 따른다)
    def into_next(layout):
        last = layout.places["BodyText/Section0"][0] + layout.places["BodyText/Section0"][1] - 1
        layout.fat[last] = layout.places["BodyText/Section1"][0]

    refused("longer than its declared size", into_next)
    # 딱 맞는 크기(섹터·64B 경계)는 정상
    exact = {"A": b"a" * 4096, "B": b"b" * 64, "C": b"c" * 1024 * 3}
    found = Compound(B.compound(exact), "a.hwp")
    assert found.read("A", 1 << 20) == exact["A"] and found.read("B", 1 << 20) == exact["B"]
