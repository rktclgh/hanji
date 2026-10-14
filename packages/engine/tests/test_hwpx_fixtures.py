"""HWPX 골든 예제(build_hwpx_fixtures.py --check)와 연결: 형식 판별, CLI parse, 폴더 수집, 뷰어, 같은 원본."""

import importlib.util
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from hanji import LocalEngine, MemoryStore, batch
from hanji.cli import main
from hanji.formats.detect import default_parsers, detect_parser
from hanji.formats.hwpx import HwpxParser
from hanji.viewer import render_html
from hanji_contracts import DocumentTree, ProcessingHistory

HERE = Path(__file__).resolve().parent
ROOT = HERE / "fixtures" / "hwpx"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()
NAMES = sorted(BUILDER.SAMPLES)


def _expected(name: str) -> DocumentTree:
    return DocumentTree.model_validate_json((ROOT / "expected" / f"{Path(name).stem}.json").read_bytes())


def _history(name: str) -> ProcessingHistory:
    return ProcessingHistory.model_validate_json((ROOT / "expected" / f"{Path(name).stem}.history.json").read_bytes())


def test_fixtures_are_up_to_date():
    """트리는 그림 자산 키에서 나온 값을 가려 비교하고, 그림은 화소 정보(pixels.json)로 정확히 비교한다."""
    built = BUILDER.build_all()
    assert {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()} >= set(built)
    for rel, data in built.items():
        assert BUILDER.same(rel, data, ROOT / rel), rel


@pytest.mark.parametrize("name", NAMES)
def test_golden_zip_members_pin_create_system_for_cross_os_bytes(name):
    with zipfile.ZipFile(BytesIO(BUILDER.SAMPLES[name])) as z:
        assert {info.create_system for info in z.infolist()} == {3}


@pytest.mark.parametrize("name", NAMES)
def test_engine_ingest_matches_golden_tree_and_history(name):
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(ROOT / "inputs" / name), document_id=BUILDER.document_id(name))
    tree = engine.get_tree(ref.document_id)
    assert BUILDER.portable(tree.model_dump(mode="json")) == BUILDER.portable(_expected(name).model_dump(mode="json"))
    order = list(dict.fromkeys(b.figure.asset for b in tree.blocks if b.figure is not None))
    shown = json.loads((ROOT / "expected" / f"{Path(name).stem}.pixels.json").read_text(encoding="utf-8"))
    assert [BUILDER.pixels(engine.get_asset(key)) for key in order] == shown
    assert engine.history(ref.document_id) == _history(name)
    again = engine.ingest(str(ROOT / "inputs" / name), document_id=BUILDER.document_id(name))
    assert again == ref  # 같은 원본은 그대로


def test_basic_keeps_numbers_units_and_order_exactly():
    tree = _expected("basic.hwpx")
    assert [(b.kind, b.level, b.text, b.locator.paragraph_index) for b in tree.blocks] == [
        ("heading", 1, "2026년 지어낸 사업 계획", 0),
        ("paragraph", None, "본 문서는 합성 예제로 만든 글이다.", 1),
        ("heading", 2, "1. 추진 배경", 3),
        ("paragraph", None, "구분\t내용\n둘째 줄 묶음 고정폭", 4),
        ("list_item", None, "첫째 번호 항목", 5), ("list_item", None, "글머리 항목", 6),
        ("paragraph", None, "모르는 문단 모양", 7),
        ("heading", 6, "아주 깊은 개요", 8),
        ("paragraph", None, "총액 1,234.5억원, 증가율 12.3%, 면적 84㎡, 변동 △0.5, GDP 대비 2.1%p(OECD 평균)", 9),
        ("paragraph", None, "표시 강조한 글자", 10),
        ("heading", 2, "2. 다음 절", 11), ("paragraph", None, "끝 문단이다.", 12)]
    assert tree.blocks[-1].section_path == ("2026년 지어낸 사업 계획", "2. 다음 절")
    assert tree.source.mime == "application/hwp+zip" and tree.source.page_count is None and tree.pages == ()
    assert [(r.fallback_reason, r.region_id) for r in _history("basic.hwpx").regions] == [
        ("unknown_inline_element", "s1-p10-unknown-inline-0")]


def test_headings_follow_the_pdf_size_rule_and_empty_has_no_blocks():
    tree = _expected("headings.hwpx")
    assert [(b.kind, b.level) for b in tree.blocks] == [
        ("heading", 1, ), ("paragraph", None), ("heading", 2), ("paragraph", None), ("paragraph", None),
        ("heading", 3), ("paragraph", None), ("paragraph", None)]
    assert _expected("empty.hwpx").blocks == () and _history("empty.hwpx").regions == ()


def test_detection_is_by_extension_case_insensitive_and_folders_pick_hwpx(tmp_path):
    assert isinstance(detect_parser("보고서.HWPX", default_parsers(ocr=False, layout=False)), HwpxParser)
    for name in ("a.hwpx", "b.HWPX", "c.hwp"):
        (tmp_path / name).write_bytes(b"x")
    extensions = frozenset(e for p in default_parsers(ocr=False, layout=False) for e in p.extensions)
    entries, skipped = batch.expand([tmp_path], False, extensions)
    assert [Path(e.path).name for e in entries] == ["a.hwpx", "b.HWPX"] and skipped == 1


def test_cli_parse_exit_codes_and_viewer_without_pages(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HANJI_DB", raising=False)
    db = tmp_path / "state.db"
    assert main(["parse", str(ROOT / "inputs" / "basic.hwpx"), "--db", str(db)]) == 0
    tree = DocumentTree.model_validate(json.loads(capsys.readouterr().out))
    assert tree.blocks[0].text == "2026년 지어낸 사업 계획"
    broken = tmp_path / "깨진.hwpx"
    broken.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)
    assert main(["parse", str(broken), "--db", str(db)]) == 4
    assert "not an HWPX" in capsys.readouterr().err
    (raw,) = re.findall(r'<script type="application/json" id="ko-data">(.*?)</script>', render_html(tree), re.DOTALL)
    data = json.loads(raw)
    assert data["pages"] == [] and data["blocks"][0]["where"] == "문단 0" and data["blocks"][0]["page"] is None
