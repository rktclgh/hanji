"""공용 zip·XML 읽기·장부·문서 마무리 모듈(OOXML 스펙 §3). HWPX 동작은 기존 HWPX 테스트·골든이 그대로 본다."""

import dataclasses
import zipfile
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


def test_hwpx_modules_reexport_the_shared_objects():
    assert container.TooLarge is zipxml.TooLarge and container.OverBudget is zipxml.OverBudget
    assert issubclass(container.Package, zipxml.Archive)
    for name in ("Bundle", "Counts", "Ledger", "Source", "ink"):
        assert getattr(ledger, name) is getattr(flowledger, name)
    assert hwpx.Region is flowdoc.Region and hwpx.finish is flowdoc.finish
