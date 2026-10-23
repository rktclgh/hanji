"""공용 zip·XML 읽기·장부·문서 마무리 모듈(OOXML 스펙 §3). HWPX 동작은 기존 HWPX 테스트·골든이 그대로 본다."""

import dataclasses
import struct
import zipfile
import zlib
from io import BytesIO

import pytest

from hanji.errors import ParseError
from hanji.formats import flowdoc, flowledger, zipxml
from hanji.formats.hwpx import container, ledger
from hanji.formats.hwpx import parser as hwpx

LIMITS = zipxml.Limits(members=8, xml_bytes=1000, xml_total=4000, binary_bytes=100, binary_total=150, ratio=200,
                       ratio_floor=1 << 20, xml_depth=16)


def _zip(members: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_scan_returns_every_uri_declared_for_each_prefix():
    xml = b'<a:r xmlns:a="urn:one" xmlns:b="urn:two"><a:x xmlns:a="urn:three"/><c xmlns="urn:default"/></a:r>'
    assert zipxml.scan(xml, "f:p", 16) == {"a": frozenset({"urn:one", "urn:three"}), "b": frozenset({"urn:two"})}
    with pytest.raises(ParseError, match="deeper than 2") as info:
        zipxml.scan(b"<a><b><c/></b></a>", "f:p", 2)
    assert info.value.location == "f:p"


def test_archive_reads_the_limits_each_time_it_reads():
    limits = [LIMITS]
    archive = zipxml.Archive(_zip({"x.xml": b"<r>" + b"a" * 500 + b"</r>"}), "f.zip", "not a zip", lambda: limits[0])
    tree, prefixes = archive.tree("x.xml", "r")
    assert tree.tag == "r" and prefixes == {} and archive.xml_read == 507
    limits[0] = dataclasses.replace(LIMITS, xml_bytes=100)
    with pytest.raises(ParseError, match="larger than 100 bytes") as info:
        archive.xml("x.xml", "r")
    assert info.value.location == "f.zip:x.xml" and archive.xml_read == 507 + 101
    with pytest.raises(ParseError, match="not a zip"):
        zipxml.Archive(b"plain", "f.zip", "not a zip", lambda: LIMITS)


def test_members_before_a_damaged_member_are_found_at_their_written_offsets():
    """멤버 데이터 몇 바이트가 빠지면 EOCD의 중앙 디렉터리 위치가 틀리고 zipfile은 모든 멤버 위치를 그만큼 옮긴다(앞에
    다른 바이트가 붙은 zip으로 본다). 손상 앞 멤버는 적힌 위치에서, 뒤 멤버는 옮긴 위치에서 읽고 손상 멤버만 잃는다."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.xml", b"<a>" + b"x" * 200 + b"</a>", zipfile.ZIP_DEFLATED)
        z.writestr("m/img.bin", bytes(range(90)), zipfile.ZIP_STORED)
        z.writestr("c.xml", b"<c>" + b"y" * 200 + b"</c>", zipfile.ZIP_DEFLATED)
    data = buf.getvalue()
    at = data.index(bytes(range(90))) + 40
    broken = data[:at] + data[at + 3:]
    with pytest.raises((zipfile.BadZipFile, ValueError, OSError)):  # zipfile만으로는 앞 멤버를 읽지 못한다
        zipfile.ZipFile(BytesIO(broken)).read("a.xml")
    archive = zipxml.Archive(broken, "f.zip", "not a zip", lambda: LIMITS)
    assert archive.xml("a.xml", "a").text == "x" * 200 and archive.xml("c.xml", "c").text == "y" * 200
    with pytest.raises(ParseError, match="cannot read zip member") as info:
        archive.binary("m/img.bin")
    assert info.value.location == "f.zip:m/img.bin"
    prefixed = zipxml.Archive(b"junk" + data, "f.zip", "not a zip", lambda: LIMITS)  # 앞에 붙은 바이트는 그대로
    assert prefixed.binary("m/img.bin") == bytes(range(90)) and prefixed.xml("a.xml", "a").text == "x" * 200
    assert prefixed._moved == {}  # 내부 값: 옮긴 위치에 모두 머리가 있어 다시 찾은 멤버가 없다


def _local(name: bytes, data: bytes, size: int | None = None) -> bytes:
    """무압축 로컬 머리 + 데이터(size: 머리에 적을 크기, 없으면 실제 길이)."""
    size = len(data) if size is None else size
    return struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 0, 0, 0x21, zlib.crc32(data), size, size,
                       len(name), 0) + name + data


def _central(name: bytes, data: bytes, offset: int, size: int | None = None) -> bytes:
    """무압축 중앙 디렉터리 항목(적힌 위치 offset)."""
    size = len(data) if size is None else size
    return struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 20, 20, 0, 0, 0, 0x21, zlib.crc32(data), size,
                       size, len(name), 0, 0, 0, 0, 0, offset) + name


def _end(directory: bytes, count: int, offset: int) -> bytes:
    """EOCD(중앙 디렉터리 크기, 적힌 위치 offset)."""
    return struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, count, count, len(directory), offset, 0)


def _covering(outer: bytes) -> bytes:
    """조작 파일: 중앙 디렉터리 위치가 실제보다 작아(zipfile은 앞에 붙은 바이트로 본다) 앞 멤버 outer는 옮긴 위치에서,
    뒤 멤버 m.xml은 적힌 위치에서 찾는데 outer의 데이터가 m.xml을 덮는다. 중앙 디렉터리 순서는 물리 순서와 다르다."""
    inner = _local(b"m.xml", b"<m>z</m>")
    shift = len(inner) + 40
    body = b"q" * 10 + inner
    entry = _local(outer, body)
    written = shift + 30 + len(outer) + 10  # m의 적힌 위치(outer의 데이터 안)
    directory = _central(b"m.xml", b"<m>z</m>", written) + _central(outer, body, 0)
    return b"\0" * shift + entry + directory + _end(directory, 2, len(entry))


def test_a_member_found_again_inside_an_earlier_member_refuses_the_file():
    """zipfile은 m의 옮긴 위치까지 e를 허용하므로 같은 바이트를 두 번 읽게 된다. 옮기지 않은 앞 멤버와 다시 찾은 멤버가
    겹치면 문서를 거부한다."""
    data = _covering(b"e.bin")
    assert zipfile.ZipFile(BytesIO(data)).read("e.bin")[:10] == b"q" * 10  # zipfile만으로는 e가 읽힌다
    with pytest.raises(ParseError, match="overlapped zip members") as info:
        zipxml.Archive(data, "f.zip", "not a zip", lambda: LIMITS)
    assert info.value.location == "f.zip:e.bin"


def test_member_names_are_checked_as_written_in_the_headers():
    """zipfile은 이름을 첫 NUL에서 자르고(윈도에서는 \\를 /로 바꾼다) 로컬 머리는 원래 이름과 견준다. NUL·역슬래시
    이름은 거부하고, 겹침 검사도 원래 이름으로 머리를 찾는다(잘린 이름으로 찾으면 그 멤버가 검사에서 빠진다)."""
    nul = _covering(b"e.bin\0x")
    with pytest.raises(ParseError, match="NUL in zip member name") as info:
        zipxml.Archive(nul, "f.zip", "not a zip", lambda: LIMITS)
    assert info.value.location == "f.zip:e.bin"
    with pytest.raises(ParseError, match="overlapped zip members"):
        zipxml._relocate(nul, zipfile.ZipFile(BytesIO(nul)).infolist(), "f.zip")
    with pytest.raises(ParseError, match="backslash in zip member name"):
        zipxml.Archive(_covering(b"e\\bin"), "f.zip", "not a zip", lambda: LIMITS)


def test_overlap_between_two_members_left_in_place_stays_a_member_error():
    """다시 찾은 멤버(r)가 있어도 옮기지 않은 두 멤버(e·n)끼리의 겹침은 zipfile의 읽기 오류 그대로 그 멤버만 잃는다."""
    first = _local(b"r.xml", b"<r>a</r>")
    shift, at = len(first) + 5, len(first) + 20  # r의 옮긴 위치는 채움 바이트 안, e의 실제 위치
    entry = _local(b"e.bin", b"w" * 10, size=25)  # 적힌 크기가 다음 멤버 머리를 넘는다
    last = _local(b"n.xml", b"<n>b</n>")
    directory = (_central(b"r.xml", b"<r>a</r>", 0) + _central(b"e.bin", b"w" * 10, at - shift, size=25)
                 + _central(b"n.xml", b"<n>b</n>", at + len(entry) - shift))
    start = at + len(entry) + len(last)
    data = first + b"\0" * 20 + entry + last + directory + _end(directory, 3, start - shift)
    archive = zipxml.Archive(data, "f.zip", "not a zip", lambda: LIMITS)
    assert list(archive._moved) == ["r.xml"]  # 내부 값: r만 적힌 위치로 다시 찾았다
    assert archive.xml("r.xml", "r").text == "a" and archive.xml("n.xml", "n").text == "b"
    with pytest.raises(ParseError, match="cannot read zip member") as info:
        archive.binary("e.bin")
    assert info.value.location == "f.zip:e.bin"


def test_hwpx_modules_reexport_the_shared_objects():
    assert container.TooLarge is zipxml.TooLarge and container.OverBudget is zipxml.OverBudget
    assert issubclass(container.Package, zipxml.Archive)
    for name in ("Bundle", "Counts", "Ledger", "Source", "ink"):
        assert getattr(ledger, name) is getattr(flowledger, name)
    assert hwpx.Region is flowdoc.Region and hwpx.finish is flowdoc.finish
