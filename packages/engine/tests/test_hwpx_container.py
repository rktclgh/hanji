"""HWPX 컨테이너 안전 읽기(스펙 §4): 모든 거부 경로가 ParseError(원인, 위치)이고 재귀·엔티티 확장·파일 쓰기가 없다."""

import importlib.util
import os
import struct
import time
import warnings
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.hwpx import container
from hanji.formats.hwpx.container import HH, HS, Package

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
SECTION = B.section(B.para(B.run(B.t("지어낸 문단이다."))))


def raw_zip(members, compression=zipfile.ZIP_STORED) -> bytes:
    """(이름, 바이트) 차례 그대로의 zip(같은 이름 둘도 쓴다)."""
    buf = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 같은 이름 경고
        with zipfile.ZipFile(buf, "w", compression) as z:
            for name, data in members:
                z.writestr(zipfile.ZipInfo(name, B.WHEN), data, compress_type=compression)
    return buf.getvalue()


def members(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(BytesIO(data)) as z:
        return [(info.filename, z.read(info)) for info in z.infolist()]


def patch_central(data: bytes, member: str, offset: int, fmt: str, value: int) -> bytes:
    """central directory에서 member 항목 머리의 offset 위치 필드를 바꾼다(8: 일반 플래그, 24: 풀린 크기)."""
    start = 0
    while (start := data.find(b"PK\x01\x02", start)) >= 0:
        (length,) = struct.unpack_from("<H", data, start + 28)
        if data[start + 46:start + 46 + length].decode("utf-8") == member:
            out = bytearray(data)
            struct.pack_into(fmt, out, start + offset, value)
            return bytes(out)
        start += 4
    raise AssertionError(member)


def refused(data: bytes, match: str, name: str = "a.hwpx") -> ParseError:
    with pytest.raises(ParseError, match=match) as info:
        Package(data, name)
    return info.value


def test_a_valid_package_lists_sections_in_spine_order_and_reads_xml():
    data = B.package(SECTION, SECTION, spine=["header", "section1", "section0"])
    package = Package(data, "a.hwpx")
    assert package.sections == ("Contents/section1.xml", "Contents/section0.xml")
    assert package.xml("Contents/section0.xml", f"{HS}sec").tag == f"{HS}sec"
    assert package.masterpages == ()


@pytest.mark.parametrize("data", [b"not a zip at all", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504, b""])
def test_bytes_that_are_not_a_zip_are_refused_naming_hwp5(data):
    error = refused(data, "not an HWPX")
    assert "HWP 5.0" in error.reason and error.location == "a.hwpx"


def test_encrypted_member_duplicate_name_and_too_many_members_are_refused(monkeypatch):
    data = B.package(SECTION)
    error = refused(patch_central(data, "Contents/section0.xml", 8, "<H", 1), "encrypted")
    assert error.location == "a.hwpx:Contents/section0.xml"
    error = refused(raw_zip(members(data) + [("Contents/section0.xml", SECTION.encode())]), "duplicate zip member")
    assert error.location == "a.hwpx:Contents/section0.xml"
    monkeypatch.setattr(container, "MAX_MEMBERS", 3)
    refused(data, "too many zip members")


def test_mimetype_must_be_present_exact_and_short_but_not_first_or_stored():
    data = B.package(SECTION)
    rest = [m for m in members(data) if m[0] != "mimetype"]
    refused(raw_zip(rest), "missing mimetype")
    refused(B.package(SECTION, mimetype=b"application/zip"), "mimetype is not application/hwp\\+zip")
    error = refused(B.package(SECTION, mimetype=b" " * 300), "longer than 256")
    assert error.location == "a.hwpx:mimetype"
    moved = raw_zip(rest + [("mimetype", b"application/hwp+zip\n")], zipfile.ZIP_DEFLATED)
    assert Package(moved, "a.hwpx").sections == ("Contents/section0.xml",)


def test_a_long_mimetype_is_refused_after_one_read_of_257_bytes(monkeypatch):
    """압축률 검사를 통과하는(무압축) 긴 mimetype도 257바이트를 한 번만 읽고 거부한다(끝까지 읽지 않는다)."""
    sizes = []
    original = zipfile.ZipExtFile.read

    def spy(self, n=-1):
        sizes.append(n)
        return original(self, n)

    data = B.package(SECTION, mimetype=b"a" * 100_000)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)
    refused(data, "longer than 256")
    assert sizes == [257]


@pytest.mark.parametrize(("change", "match", "where"), [
    ({"spine": ["header", "section0", "section0"]}, "duplicate spine itemref", "Contents/content.hpf"),
    ({"spine": ["header", "section9"]}, "not in the manifest", "Contents/content.hpf"),
    ({"spine": ["header", "section0", "twice"], "items": [("twice", "Contents/section0.xml", "application/xml", True)]},
     "refers to 'Contents/section0.xml' twice", "Contents/content.hpf"),
    ({"spine": ["header", "section0", "other"], "items": [("other", "Contents/other.xml", "application/xml", True)]},
     "unsupported spine item", "Contents/content.hpf"),
    ({"spine": ["header", "section0", "up"], "items": [("up", "../Contents/section1.xml", "application/xml", True)]},
     "unsupported spine item", "Contents/content.hpf"),
    ({"spine": ["header", "section0", "abs"], "items": [("abs", "/Contents/section1.xml", "application/xml", True)]},
     "unsupported spine item", "Contents/content.hpf"),
    ({"spine": ["header", "section0", "s5"], "items": [("s5", "Contents/section5.xml", "application/xml", True)]},
     "spine member is missing", "Contents/section5.xml"),
    ({"items": [("section0", "Contents/x.xml", "application/xml", True)]}, "duplicate manifest id",
     "Contents/content.hpf"),
])
def test_spine_and_manifest_mismatches_are_refused(change, match, where):
    error = refused(B.package(SECTION, **change), match)
    assert error.location == f"a.hwpx:{where}"


def test_no_sections_an_orphan_section_and_a_missing_content_hpf_are_refused():
    assert refused(B.package(), "no sections").location == "a.hwpx:Contents/content.hpf"
    error = refused(B.package(SECTION, extra={"Contents/section7.xml": SECTION.encode()}), "not in the spine")
    assert error.location == "a.hwpx:Contents/section7.xml"
    rest = [m for m in members(B.package(SECTION)) if m[0] != "Contents/content.hpf"]
    assert refused(raw_zip(rest), "missing zip member").location == "a.hwpx:Contents/content.hpf"


def test_xml_size_ratio_and_total_limits_are_checked_on_read_bytes(monkeypatch):
    data = B.package(SECTION)
    monkeypatch.setattr(container, "MAX_XML_BYTES", 200)
    error = refused(data, "larger than 200 bytes")  # content.hpf가 먼저 읽힌다
    assert error.location == "a.hwpx:Contents/content.hpf"
    monkeypatch.setattr(container, "MAX_XML_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(container, "MAX_XML_TOTAL", len(B.content(1)) + 10)
    package = Package(data, "a.hwpx")
    with pytest.raises(ParseError, match="in total"):
        package.xml("Contents/header.xml", f"{HH}head")
    assert package.xml_read == container.MAX_XML_TOTAL + 1  # 남은 몫 + 넘음을 아는 1바이트만 읽었다
    monkeypatch.setattr(container, "MAX_XML_TOTAL", 256 * 1024 * 1024)
    monkeypatch.setattr(container, "RATIO_FLOOR", 1000)
    padded = B.section(B.para(B.run(B.t("가" + " " * 200_000))))
    deflated = raw_zip([(n, padded.encode() if n == "Contents/section0.xml" else d) for n, d in members(data)],
                       zipfile.ZIP_DEFLATED)
    with pytest.raises(ParseError, match="compression ratio") as info:
        Package(deflated, "a.hwpx").xml("Contents/section0.xml", f"{HS}sec")
    assert info.value.location == "a.hwpx:Contents/section0.xml"


def test_a_member_whose_declared_size_lies_or_whose_crc_is_wrong_is_refused():
    data = B.package(SECTION)
    lying = patch_central(data, "Contents/section0.xml", 24, "<I", 10)
    with pytest.raises(ParseError, match="cannot read zip member"):
        Package(lying, "a.hwpx").xml("Contents/section0.xml", f"{HS}sec")
    at = data.index("지어낸".encode())
    broken = data[:at] + "꾸며낸".encode() + data[at + len("지어낸".encode()):]
    with pytest.raises(ParseError, match="cannot read zip member") as info:
        Package(broken, "a.hwpx").xml("Contents/section0.xml", f"{HS}sec")
    assert info.value.location == "a.hwpx:Contents/section0.xml"


LAUGHS = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
          + "".join(f'<!ENTITY lol{k} "{f"&lol{k - 1 if k > 1 else ""};" * 10}">' for k in range(1, 10))
          + f']><hs:sec xmlns:hs="{B.HS}">&lol9;</hs:sec>')
EXTERNAL = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
            f'<hs:sec xmlns:hs="{B.HS}">&e;</hs:sec>')


@pytest.mark.parametrize("xml", [LAUGHS, EXTERNAL, f'<!DOCTYPE hs:sec><hs:sec xmlns:hs="{B.HS}"/>'])
def test_doctype_is_refused_before_any_tree_or_entity_expansion(xml):
    package = Package(B.package(xml), "a.hwpx")
    started = time.perf_counter()
    with pytest.raises(ParseError, match="DOCTYPE is not allowed") as info:
        package.xml("Contents/section0.xml", f"{HS}sec")
    # 느린 CI 러너를 위해 넉넉히 잡는다(막지 못하면 엔티티 확장이 이보다 훨씬 오래 걸린다)
    assert time.perf_counter() - started < 10 and info.value.location == "a.hwpx:Contents/section0.xml"


def test_deep_nesting_is_refused_without_recursion_error():
    deep = f'<hs:sec xmlns:hs="{B.HS}" xmlns:hp="{B.HP}">' + "<hp:x>" * 10_000 + "</hp:x>" * 10_000 + "</hs:sec>"
    package = Package(B.package(deep), "a.hwpx")
    with pytest.raises(ParseError, match="deeper than 256"):
        package.xml("Contents/section0.xml", f"{HS}sec")


def test_syntax_errors_carry_the_line_and_a_wrong_root_is_refused():
    package = Package(B.package('<?xml version="1.0"?>\n<hs:sec xmlns:hs="x">\n<a></b></hs:sec>'), "a.hwpx")
    with pytest.raises(ParseError, match="invalid XML") as info:
        package.xml("Contents/section0.xml", f"{HS}sec")
    assert info.value.location == "a.hwpx:Contents/section0.xml:3"
    with pytest.raises(ParseError, match="unexpected root element") as info:
        Package(B.package(SECTION), "a.hwpx").xml("Contents/header.xml", f"{HS}sec")
    assert info.value.location == "a.hwpx:Contents/header.xml"


def test_names_outside_the_zip_are_only_missing_names(tmp_path, monkeypatch):
    """../·절대 경로는 zip 이름 목록에 없으므로 '없음'이다. 디스크를 읽거나 쓰지 않는다."""
    monkeypatch.chdir(tmp_path)
    package = Package(B.package(SECTION), "a.hwpx")
    assert not package.has("../Contents/section0.xml") and not package.has("/etc/passwd")
    with pytest.raises(ParseError, match="missing zip member"):
        package.xml("../Contents/section0.xml", None)
    assert list(tmp_path.iterdir()) == []


def test_binary_members_over_the_limits_are_too_large_or_over_budget_not_parse_errors(monkeypatch):
    data = B.package(SECTION, binaries={f"BinData/{k}.bin": bytes([65 + k]) * 100 for k in range(3)})
    monkeypatch.setattr(container, "MAX_BINARY_BYTES", 99)
    package = Package(data, "a.hwpx")
    with pytest.raises(container.TooLarge):
        package.binary("BinData/0.bin")
    assert package.binary_read == 100  # 상한 + 1바이트만 읽었다
    monkeypatch.setattr(container, "MAX_BINARY_BYTES", 100)
    monkeypatch.setattr(container, "MAX_BINARY_TOTAL", 150)
    package = Package(data, "a.hwpx")
    opened = []
    original = package._zip.open

    def spy(info, *args, **kwargs):
        opened.append(info.filename)
        return original(info, *args, **kwargs)

    monkeypatch.setattr(package._zip, "open", spy)
    assert package.binary("BinData/0.bin") == b"A" * 100
    with pytest.raises(container.OverBudget):
        package.binary("BinData/1.bin")  # 남은 50 + 1바이트만 읽는다
    with pytest.raises(container.OverBudget):
        package.binary("BinData/2.bin")  # 다 쓴 뒤에는 열지 않는다
    assert opened == ["BinData/0.bin", "BinData/1.bin"] and package.binary_read == 151


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_members_with_unbounded_decompressors_are_refused(method):
    """BZIP2·LZMA 멤버는 읽기 상한이 먹지 않으므로 이름을 대고 거부한다(작은 입력)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in members(B.package(SECTION)):
            z.writestr(zipfile.ZipInfo(name, B.WHEN), data)
        z.writestr(zipfile.ZipInfo("BinData/x.bin", B.WHEN), b"tiny", compress_type=method)
    error = refused(buf.getvalue(), "unsupported zip compression method")
    assert error.location == "a.hwpx:BinData/x.bin"
    # mimetype 멤버 자체가 그 방식이어도 같다
    rest = [m for m in members(B.package(SECTION)) if m[0] != "mimetype"]
    packed = raw_zip([("mimetype", B.MIME)] + rest, method)
    assert refused(packed, "unsupported zip compression method").location == "a.hwpx:mimetype"


def test_binary_of_a_missing_member_is_a_parse_error_with_location():
    package = Package(B.package(SECTION), "a.hwpx")
    with pytest.raises(ParseError, match="missing zip member") as info:
        package.binary("BinData/none.png")
    assert info.value.location == "a.hwpx:BinData/none.png"


@pytest.mark.parametrize("encoding", ["EUC-KR", "cp949", "UTF-32", "foo-bar"])
def test_an_unsupported_xml_encoding_declaration_is_a_parse_error_with_location(encoding):
    xml = f'<?xml version="1.0" encoding="{encoding}"?>' + SECTION.split("?>", 1)[1]
    package = Package(B.package(xml), "a.hwpx")
    with pytest.raises(ParseError, match="unsupported XML encoding") as info:
        package.xml("Contents/section0.xml", f"{HS}sec")
    assert info.value.location == "a.hwpx:Contents/section0.xml"


@pytest.mark.skipif(os.sep == "\\", reason="윈도의 zipfile은 이름의 \\를 /로 바꿔 읽는다")
def test_a_member_name_with_a_backslash_is_refused():
    data = raw_zip([*members(B.package(SECTION)), ("Contents\\extra.xml", b"<a/>")])
    assert refused(data, "backslash in zip member name").location == "a.hwpx:Contents\\extra.xml"
