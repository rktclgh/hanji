"""HWP 쌍둥이 골든(build_hwp_fixtures.py --check, HWP 스펙 §11.1)과 연결: 엔진 수집, CLI parse, 폴더 수집, 같은 원본."""

import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from hanji import LocalEngine, MemoryStore, batch
from hanji.cli import main
from hanji.formats.detect import default_parsers
from hanji_contracts import DocumentTree, ProcessingHistory

HERE = Path(__file__).resolve().parent
ROOT = HERE / "fixtures" / "hwp"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwp_fixtures", HERE / "build_hwp_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()
NAMES = sorted(BUILDER.SAMPLES)


def test_inputs_are_up_to_date_and_every_twin_matches_its_hwpx_golden():
    assert {p.name for p in (ROOT / "inputs").iterdir()} == set(NAMES)
    for name, data in BUILDER.SAMPLES.items():
        assert BUILDER.same(name, data, ROOT / "inputs" / name), name
        assert BUILDER.twin_ok(name, (ROOT / "inputs" / name).read_bytes()), name


@pytest.mark.parametrize("name", NAMES)
def test_engine_ingest_matches_the_twin_hwpx_golden_and_the_same_bytes_are_unchanged(name):
    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    ref = engine.ingest(str(ROOT / "inputs" / name), document_id="fx-hwp-" + Path(name).stem)
    tree = engine.get_tree(ref.document_id).model_dump(mode="json")
    history = engine.history(ref.document_id).model_dump(mode="json")
    assert BUILDER.twin_key(tree, history) == BUILDER.expected_key(BUILDER.TWINS[name])
    assert tree["source"]["mime"] == "application/x-hwp" and tree["source"]["page_count"] is None
    again = engine.ingest(str(ROOT / "inputs" / name), document_id="fx-hwp-" + Path(name).stem)
    assert again == ref  # 같은 원본은 그대로


def test_the_twin_key_catches_a_one_character_change_a_changed_heading_path_and_an_extra_record():
    tree, history = BUILDER.golden("basic.hwp", (ROOT / "inputs" / "basic.hwp").read_bytes())
    base = (tree.model_dump(mode="json"), history.model_dump(mode="json"))
    assert BUILDER.twin_key(*base) == BUILDER.expected_key("basic")
    changed = json.loads(json.dumps(base))
    changed[0]["blocks"][1]["text"] = changed[0]["blocks"][1]["text"][:-1] + "!"  # 글자 하나
    assert BUILDER.twin_key(*changed) != BUILDER.expected_key("basic")
    moved = json.loads(json.dumps(base))
    moved[0]["blocks"][1]["section_path"] = []  # 제목 경로 하나
    assert BUILDER.twin_key(*moved) != BUILDER.expected_key("basic")
    extra = json.loads(json.dumps(base))
    extra[1]["regions"].append({"region_id": "s0-p0-x-0"})  # 처리 이력 하나 더
    assert BUILDER.twin_key(*extra) != BUILDER.expected_key("basic")


def test_object_twins_need_no_dropped_records_and_the_check_sees_picture_pixels():
    assert set(BUILDER.DROPPED) == {"basic"}  # 표·그림·주석 쌍둥이는 HWPX 기대값을 하나도 빼지 않는다
    data = (ROOT / "inputs" / "figures.hwp").read_bytes()
    assert BUILDER.twin_ok("figures.hwp", data)
    buf = io.BytesIO()
    Image.new("RGB", (4, 2), (1, 2, 3)).save(buf, format="BMP")  # 같은 크기(4×2)의 다른 화소: 트리 키는 같다
    bins, streams = BUILDER.FIGURE_BINS
    swapped = BUILDER.document(BUILDER.from_section(BUILDER.HX.FIGURES), head=BUILDER.from_header(BUILDER.HX.header(),
                               bins), extra={**streams, "BinData/BIN0003.bmp": buf.getvalue()})
    tree, history = BUILDER.golden("figures.hwp", swapped)
    assert BUILDER.twin_key(tree.model_dump(mode="json"), history.model_dump(mode="json")) == BUILDER.expected_key(
        "figures")
    assert not BUILDER.twin_ok("figures.hwp", swapped)  # 그림 하나의 화소만 달라도 쌍둥이가 아니다


def test_the_basic_twin_differs_from_hwpx_only_by_the_dropped_highlight_record():
    hwpx = ProcessingHistory.model_validate_json((BUILDER.HWPX_EXPECTED / "basic.history.json").read_bytes())
    tree, history = BUILDER.golden("basic.hwp", (ROOT / "inputs" / "basic.hwp").read_bytes())
    assert [r.region_id for r in hwpx.regions] == list(BUILDER.DROPPED["basic"]) and history.regions == ()
    hwpx_tree = DocumentTree.model_validate_json((BUILDER.HWPX_EXPECTED / "basic.json").read_bytes())
    assert [b.text for b in tree.blocks] == [b.text for b in hwpx_tree.blocks]
    assert tree.blocks[3].text.startswith("구분\t내용\n둘째 줄")  # 탭·줄바꿈은 정확 출력이 본다(장부 밖 공백)


def test_the_deflate_twin_holds_the_same_streams_compressed():
    plain = BUILDER.streams_of((ROOT / "inputs" / "basic.hwp").read_bytes())
    packed = BUILDER.streams_of((ROOT / "inputs" / "basic-deflate.hwp").read_bytes())
    assert plain.pop("FileHeader")[36] == 0 and packed.pop("FileHeader")[36] == 1  # 압축 비트만 다르다
    assert plain == packed


def test_cli_parse_exit_codes_and_folders_pick_hwp(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HANJI_DB", raising=False)
    db = tmp_path / "state.db"
    assert main(["parse", str(ROOT / "inputs" / "basic.hwp"), "--db", str(db)]) == 0
    tree = DocumentTree.model_validate(json.loads(capsys.readouterr().out))
    assert tree.blocks[0].text == "2026년 지어낸 사업 계획" and tree.source.mime == "application/x-hwp"
    broken = tmp_path / "깨진.hwp"
    broken.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)
    assert main(["parse", str(broken), "--db", str(db)]) == 4
    assert "CFB byte order" in capsys.readouterr().err
    folder = tmp_path / "folder"
    folder.mkdir()
    for name in ("a.hwp", "b.HWP", "c.hwt"):
        (folder / name).write_bytes(b"x")
    extensions = frozenset(e for p in default_parsers(ocr=False, layout=False) for e in p.extensions)
    entries, skipped = batch.expand([folder], False, extensions)
    assert [Path(e.path).name for e in entries] == ["a.hwp", "b.HWP"] and skipped == 1
