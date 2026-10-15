"""OOXML 꾸러미 읽기와 안전(스펙 §4, §5.4, §11.2 안전 목록). 합성 문서는 build_ooxml_fixtures.py의 쓰기 도구로 만든다."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hanji.errors import ParseError
from hanji.formats.ooxml import package as ox
from hanji.formats.ooxml.package import Package, choose, ignorable, resolve

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
DOC = B.document(B.p("지어낸 문단"))


def open_docx(data: bytes) -> Package:
    return Package(data, "a.docx", "DOCX", B.DOCX_MAIN)


def test_a_minimal_docx_package_finds_its_main_part_and_related_parts():
    package = open_docx(B.docx(DOC, styles_xml=B.styles()))
    assert package.main == "word/document.xml"
    assert package.content_type("word/document.xml") == B.DOCX_MAIN
    assert package.content_type("word/_rels/document.xml.rels").endswith("package.relationships+xml")
    assert package.related(package.main, "styles") == ["word/styles.xml"] and package.related(package.main, "x") == []
    tree, prefixes = package.part(package.main, f"{{{B.W}}}document")
    assert tree.tag == f"{{{B.W}}}document" and prefixes["w"] == frozenset({B.W})


def test_not_a_zip_and_legacy_binary_documents_are_refused_with_their_own_messages():
    with pytest.raises(ParseError, match=r"not a DOCX \(zip\) package") as info:
        open_docx(b"plain text")
    assert info.value.location == "a.docx"
    with pytest.raises(ParseError, match="legacy Word/PowerPoint binary documents are not supported"):
        open_docx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)


def test_content_types_and_the_office_document_relationship_are_required():
    members = [("_rels/.rels", B.rels(("rId1", B.REL["officeDocument"], "word/document.xml"))),
               ("word/document.xml", DOC)]
    with pytest.raises(ParseError, match="missing zip member") as info:
        open_docx(B.zip_bytes(members))
    assert info.value.location == "a.docx:[Content_Types].xml"
    with pytest.raises(ParseError, match="no officeDocument relationship") as info:
        open_docx(B.docx(DOC, root_rels=[("rId1", f"{B.R}/thumbnail", "word/document.xml")]))
    assert info.value.location == "a.docx:_rels/.rels"
    with pytest.raises(ParseError, match="missing zip member") as info:
        open_docx(B.docx(DOC, root_rels=[("rId1", B.REL["officeDocument"], "word/other.xml")]))
    assert info.value.location == "a.docx:word/other.xml"


@pytest.mark.parametrize("main_type", [
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
])
def test_macro_template_and_other_main_part_types_are_unsupported(main_type):
    with pytest.raises(ParseError, match="unsupported OOXML document type") as info:
        open_docx(B.docx(DOC, main_type=main_type))
    assert info.value.location == "a.docx:word/document.xml"


def test_strict_ooxml_is_refused_by_relationship_type_and_by_root_namespace():
    strict = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
    with pytest.raises(ParseError, match="Strict OOXML is not supported"):
        open_docx(B.docx(DOC, root_rels=[("rId1", strict, "word/document.xml")]))
    package = open_docx(B.docx('<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"/>'))
    with pytest.raises(ParseError, match="Strict OOXML is not supported") as info:
        package.part(package.main, f"{{{B.W}}}document")
    assert info.value.location == "a.docx:word/document.xml"
    with pytest.raises(ParseError, match="unexpected root element"):
        open_docx(B.docx(B.styles())).part("word/document.xml", f"{{{B.W}}}document")


def test_duplicate_relationship_ids_are_refused():
    package = open_docx(B.docx(DOC, doc_rels=[("rId9", B.REL["hyperlink"], "a.xml"),
                                              ("rId9", B.REL["hyperlink"], "b.xml")]))
    with pytest.raises(ParseError, match="duplicate relationship id 'rId9'") as info:
        package.rels(package.main)
    assert info.value.location == "a.docx:word/_rels/document.xml.rels"


@pytest.mark.parametrize(("part", "target", "message"), [
    ("word/document.xml", "../../x", "outside the package"),
    ("word/document.xml", "/../x", "outside the package"),
    ("", "a/../../x", "outside the package"),
    ("", "%2e%2e/x", "outside the package"),
    ("word/document.xml", "http://example.invalid/x", "invalid relationship target"),
    ("word/document.xml", "//example.invalid/x", "invalid relationship target"),
    ("word/document.xml", "C:\\x", "invalid relationship target"),
    ("word/document.xml", "media\\x.png", "invalid relationship target"),
    ("word/document.xml", "media/x.png#frag", "invalid relationship target"),
    ("word/document.xml", "media/x%5c.png", "invalid relationship target"),
])
def test_bad_relationship_targets_are_refused_before_normalising(part, target, message):
    with pytest.raises(ParseError, match=message) as info:
        resolve(part, target, "a.docx:r")
    assert info.value.location == "a.docx:r"


def test_relationship_targets_resolve_by_a_stack_of_segments():
    assert resolve("word/document.xml", "media/a%20b.png", "l") == "word/media/a b.png"
    assert resolve("word/document.xml", "./../word/./styles.xml", "l") == "word/styles.xml"
    assert resolve("word/document.xml", "/customXml/item1.xml", "l") == "customXml/item1.xml"
    assert resolve("", "word/document.xml", "l") == "word/document.xml"


def test_external_relationships_are_not_followed(monkeypatch):
    opened = []
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a) or pytest.fail("file access"))
    package = open_docx(B.docx(DOC, doc_rels=[("rId7", B.REL["hyperlink"], "file:///etc/passwd", "External"),
                                              ("rId8", f"{B.R}/image", "http://example.invalid/a.png", "External")]))
    found = package.rels(package.main)
    assert (found["rId7"].target, found["rId8"].target) == (None, None) and opened == []
    assert package.related(package.main, "hyperlink") == []


def test_doctype_deep_nesting_and_compression_bombs_are_refused_through_the_shared_reader(monkeypatch):
    with pytest.raises(ParseError, match="DOCTYPE"):
        open_docx(B.docx('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]><d/>')).xml("word/document.xml", None)
    deep = "<a>" * 10_000 + "</a>" * 10_000
    with pytest.raises(ParseError, match="deeper than 256"):
        open_docx(B.docx(deep)).xml("word/document.xml", None)
    monkeypatch.setattr(ox, "RATIO_FLOOR", 1000)
    data = B.docx(DOC, extra=[("word/bomb.xml", "<a>" + " " * 400_000 + "</a>")], deflated=True)
    with pytest.raises(ParseError, match="compression ratio over 200") as info:
        open_docx(data).xml("word/bomb.xml", None)
    assert info.value.location == "a.docx:word/bomb.xml"


def _alt(*children: str) -> ET.Element:
    return ET.fromstring(f'<mc:AlternateContent xmlns:mc="{B.NS["mc"]}">{"".join(children)}</mc:AlternateContent>')


PREFIXES = {"wps": frozenset({B.NS["wps"]}), "v": frozenset({B.NS["v"]}), "x": frozenset({B.NS["x"]}),
            "two": frozenset({B.NS["wps"], B.NS["x"]})}


def test_choose_takes_the_first_choice_whose_requires_are_all_read():
    mc = B.NS["mc"]
    alt = _alt('<mc:Choice Requires="x"><a/></mc:Choice>', '<mc:Choice Requires="wps v"><b/></mc:Choice>',
               '<mc:Fallback><c/></mc:Fallback>')
    assert choose(alt, PREFIXES) is alt[1]
    unknown = _alt('<mc:Choice Requires="x"/>', '<mc:Choice Requires="nope"/>', '<mc:Choice Requires="two"/>',
                   '<mc:Fallback><c/></mc:Fallback>')
    assert choose(unknown, PREFIXES).tag == f"{{{mc}}}Fallback"  # 모르는 URI·풀 수 없는 접두어·URI 둘
    empty = _alt('<mc:Choice Requires="wps"/>', '<mc:Fallback><c/></mc:Fallback>')
    assert choose(empty, PREFIXES) is empty[0]  # 고른 쪽이 비어도 바꾸지 않는다
    assert choose(_alt('<mc:Choice Requires="x"/>'), PREFIXES) is None


def test_ignorable_lists_unread_namespaces_and_process_content():
    mc = B.NS["mc"]
    element = ET.fromstring(f'<a xmlns:mc="{mc}" mc:Ignorable="x wps nope" mc:ProcessContent="x:box x:*"/>')
    assert ignorable(element, PREFIXES) == (frozenset({B.NS["x"]}), frozenset({(B.NS["x"], "box"), (B.NS["x"], "*")}))
