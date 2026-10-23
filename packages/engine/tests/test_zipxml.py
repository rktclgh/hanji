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


def _local(name: bytes, data: bytes) -> bytes:
    """무압축 로컬 머리 + 데이터."""
    return struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 0, 0, 0x21, zlib.crc32(data), len(data), len(data),
                       len(name), 0) + name + data


def _central(name: bytes, data: bytes, offset: int) -> bytes:
    """무압축 중앙 디렉터리 항목(적힌 위치 offset)."""
    return struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 20, 20, 0, 0, 0, 0x21, zlib.crc32(data), len(data),
                       len(data), len(name), 0, 0, 0, 0, 0, offset) + name


def test_a_member_found_again_inside_an_earlier_member_refuses_the_file():
    """조작 파일: 중앙 디렉터리 위치가 실제보다 작아(zipfile은 앞에 붙은 바이트로 본다) 앞 멤버 e는 옮긴 위치에서, 뒤
    멤버 m은 적힌 위치에서 찾는데 e의 데이터가 m을 덮는다. zipfile은 m의 옮긴 위치까지 e를 허용하므로 같은 바이트를
    두 번 읽게 된다. 옮기지 않은 앞 멤버와 다시 찾은 멤버가 겹치면 문서를 거부한다."""
    inner = _local(b"m.xml", b"<m>z</m>")
    shift = len(inner) + 40
    body = b"q" * 10 + inner
    entry = _local(b"e.bin", body)
    written = shift + 30 + 5 + 10  # m의 적힌 위치(e의 데이터 안)
    directory = _central(b"m.xml", b"<m>z</m>", written) + _central(b"e.bin", body, 0)  # 물리 순서와 다르다
    start = shift + len(entry)
    data = b"\0" * shift + entry + directory + struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 2, 2, len(directory),
                                                           start - shift, 0)
    assert zipfile.ZipFile(BytesIO(data)).read("e.bin") == body  # zipfile만으로는 e가 읽힌다
    with pytest.raises(ParseError, match="overlapped zip members") as info:
        zipxml.Archive(data, "f.zip", "not a zip", lambda: LIMITS)
    assert info.value.location == "f.zip:e.bin"


def test_hwpx_modules_reexport_the_shared_objects():
    assert container.TooLarge is zipxml.TooLarge and container.OverBudget is zipxml.OverBudget
    assert issubclass(container.Package, zipxml.Archive)
    for name in ("Bundle", "Counts", "Ledger", "Source", "ink"):
        assert getattr(ledger, name) is getattr(flowledger, name)
    assert hwpx.Region is flowdoc.Region and hwpx.finish is flowdoc.finish
