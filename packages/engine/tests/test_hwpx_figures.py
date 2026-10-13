"""HWPX 그림·캡션·자산(스펙 §4.4, §7)과 묶음·글맵시·그림 성격 개체(§9). 합성 문서는 build_hwpx_fixtures.py로 만든다."""

import importlib.util
import io
import struct
import zlib
from pathlib import Path

from PIL import Image, PngImagePlugin

from hanji.core import build_tree, check_assets
from hanji.formats.hwpx import container, images, parser
from hanji.formats.hwpx.parser import HwpxParser
from hanji.formats.pdf import figures
from hanji_contracts import DocumentTree, SourceInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE / "fixtures" / "hwpx"


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


def one_picture(raw: bytes, media: str = "image/png", **kw) -> tuple:
    """그림 하나(image1)인 문서의 (ParsedSource, 장부 숫자)."""
    data = B.package(B.section(B.para(B.run(B.pic("image1", **kw)))),
                     items=[("image1", "BinData/image1.bin", media, True)], binaries={"BinData/image1.bin": raw})
    return HwpxParser()._parse(data, "a.hwpx")


def reasons(parsed) -> list[str]:
    return [r.fallback_reason for r in parsed.regions]


def test_figures_golden_blocks_assets_and_records():
    tree = DocumentTree.model_validate_json((ROOT / "expected" / "figures.json").read_bytes())
    shapes = [(b.kind, b.locator.paragraph_index, (b.figure.width_px, b.figure.height_px, b.figure.dpi)
               if b.figure else None) for b in tree.blocks]
    assert shapes == [
        ("paragraph", 0, None), ("figure", 1, (4, 4, 4)), ("figure", 2, (8, 8, 4)), ("figure", 3, (4, 2, 4)),
        ("figure", 4, (4000, 2, 400)), ("figure", 5, (4, 4, 4)), ("figure", 6, None), ("figure", 7, None),
        ("figure", 8, None), ("figure", 9, (4, 2, 96)), ("caption", 9, None), ("figure", 10, None),
        ("caption", 10, None), ("caption", 11, None), ("figure", 11, (8, 8, 8)), ("figure", 12, (4, 2, 4)),
        ("figure", 12, (4, 4, 4)), ("paragraph", 13, None), ("paragraph", 13, None)]
    blocks = tree.blocks
    assert blocks[1].figure.asset == blocks[5].figure.asset == blocks[16].figure.asset  # 같은 그림은 자산 하나
    assert blocks[9].figure.caption_block_id == blocks[10].block_id
    assert blocks[14].figure.caption_block_id == blocks[13].block_id  # TOP 캡션은 그림 앞
    assert blocks[11].figure is None and blocks[12].text == "그림 2. 이미지 없는 그림의 캡션"  # 연결 없이 바로 뒤
    assert [b.text for b in blocks[17:]] == ["글맵시 앞", "지어낸 글맵시"]
    history = (ROOT / "expected" / "figures.history.json").read_text(encoding="utf-8")
    for reason in ("figure_image_unsupported", "figure_image_linked", "figure_image_missing", "shape_text"):
        assert reason in history
    assert "coverage_mismatch" not in history and "unassigned_text" not in history


def test_the_textart_text_is_a_ledger_source_and_the_ledger_balances():
    parsed, counts = HwpxParser()._parse(B.SAMPLES["figures.hwpx"], "figures.hwpx")
    (count,) = counts
    assert count.ok and count.rescued == 0 and count.source == count.in_blocks
    assert count.source == sum(sum(not ch.isspace() for ch in s) for s in (
        "그림 예제 문단이다.", "그림 1. 지어낸 막대 그림", "그림 2. 이미지 없는 그림의 캡션", "그림 3. 위에 둔 캡션",
        "글맵시 앞", "지어낸 글맵시"))
    assert len(parsed.assets) == 4


def test_pngs_are_rgb_on_white_without_metadata_and_match_their_keys():
    parsed, _ = HwpxParser()._parse(B.SAMPLES["figures.hwpx"], "figures.hwpx")
    source = SourceInfo(name="f.hwpx", mime=parsed.mime, content_hash="sha256:" + "0" * 64)
    check_assets(build_tree(parsed, "d", 1, source), parsed.assets)
    for png in parsed.assets.values():
        with Image.open(io.BytesIO(png)) as image:
            assert image.format == "PNG" and image.mode == "RGB" and not image.text
    rgba = next(png for png in parsed.assets.values() if Image.open(io.BytesIO(png)).size == (4, 4))
    red, green, blue = Image.open(io.BytesIO(rgba)).getpixel((0, 0))
    assert abs(red - 227) <= 1 and abs(green - 142) <= 1 and abs(blue - 142) <= 1  # 반투명 빨강을 흰 바탕에


def _chunks(png: bytes) -> list[bytes]:
    out, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        out.append(png[pos + 4:pos + 8])
        pos += 12 + length
    return out


def test_icc_profiles_and_text_from_the_source_image_are_not_kept():
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "지어낸 설명")
    buf = io.BytesIO()
    Image.new("RGB", (3, 3), (10, 20, 30)).save(buf, format="PNG", icc_profile=b"invented icc bytes", pnginfo=info)
    assert b"iCCP" in _chunks(buf.getvalue()) and b"iTXt" in _chunks(buf.getvalue())
    png = images.to_png(buf.getvalue())
    assert isinstance(png, images.Png) and _chunks(png.data) == [b"IHDR", b"IDAT", b"IEND"]


def _png_header(width: int, height: int) -> bytes:
    """IHDR만 있는 PNG(화소 데이터 없음)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))


def test_decoding_refuses_header_only_bombs_damaged_and_unsupported_images():
    assert images.to_png(_png_header(20_000, 20_000)) == images.TOO_LARGE  # 4억 화소: load 전에
    assert images.to_png(_png_header(10_000, 9_500)) == images.TOO_LARGE  # Pillow 폭탄 경고 기준 위
    assert images.to_png(B.CMYK_JPEG[:200]) == images.UNREADABLE
    assert images.to_png(B.FAKE_WMF) == images.UNSUPPORTED
    parsed, counts = one_picture(_png_header(20_000, 20_000))
    assert reasons(parsed) == ["figure_image_too_large"] and parsed.assets == {} and counts[0].ok
    assert [b["kind"] for b in parsed.blocks] == ["figure"] and "figure" not in parsed.blocks[0]


def test_binary_size_limits_and_the_asset_budget_leave_the_figure_without_an_image(monkeypatch):
    monkeypatch.setattr(container, "MAX_BINARY_BYTES", len(B.GRAY_PNG) - 1)
    assert reasons(one_picture(B.GRAY_PNG)[0]) == ["figure_image_too_large"]
    monkeypatch.setattr(container, "MAX_BINARY_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(container, "MAX_BINARY_TOTAL", len(B.GRAY_PNG) - 1)
    assert reasons(one_picture(B.GRAY_PNG)[0]) == ["figure_image_budget"]
    monkeypatch.setattr(container, "MAX_BINARY_TOTAL", 512 * 1024 * 1024)
    monkeypatch.setattr(figures, "ASSET_LIMIT", 0)
    parsed, _ = one_picture(B.GRAY_PNG)
    assert reasons(parsed) == ["figure_image_budget"] and parsed.assets == {}


def test_a_picture_with_a_corrupt_member_loses_only_its_image():
    data = B.package(B.section(B.para(B.run(B.t("본문"), B.pic("image1")))),
                     items=[("image1", "BinData/image1.png", "image/png", True)],
                     binaries={"BinData/image1.png": B.GRAY_PNG})
    at = data.index(B.GRAY_PNG) + 30
    broken = data[:at] + bytes([data[at] ^ 0xFF]) + data[at + 1:]  # CRC 오류
    parsed = HwpxParser().parse(broken, "a.hwpx")
    assert [b["kind"] for b in parsed.blocks] == ["paragraph", "figure"]
    assert reasons(parsed) == ["figure_image_unreadable"]


def test_dpi_uses_the_display_width_and_falls_back_to_96():
    assert images.dpi(400, "7200") == 400 and images.dpi(400, "14400") == 200
    assert images.dpi(400, None) == 96 and images.dpi(400, "0") == 96 and images.dpi(400, "4294965095") == 96
    assert images.dpi(400, "x") == 96


def test_embedded_objects_are_figures_without_images_and_containers_keep_order():
    data = B.package(B.section(B.para(B.run(B.container(B.ole(), B.pic("image1"))))),
                     items=[("image1", "BinData/image1.png", "image/png", True)],
                     binaries={"BinData/image1.png": B.GRAY_PNG})
    parsed = HwpxParser().parse(data, "a.hwpx")
    assert [("figure" in b) for b in parsed.blocks] == [False, True]
    assert reasons(parsed) == ["embedded_object"] and parsed.regions[0].kind == "figure"


def test_deeply_nested_groups_do_not_hit_the_recursion_limit():
    """묶음 개체는 XML 깊이 상한(256) 안에서 몇 겹이든 Python 재귀 한도 안에서 돈다."""
    nested = B.pic("image1")
    for _ in range(240):
        nested = B.container(nested)
    data = B.package(B.section(B.para(B.run(nested))), items=[("image1", "BinData/image1.png", "image/png", True)],
                     binaries={"BinData/image1.png": B.GRAY_PNG})
    parsed = HwpxParser().parse(data, "a.hwpx")
    assert [b["kind"] for b in parsed.blocks] == ["figure"] and len(parsed.assets) == 1


def test_a_png_refused_by_the_asset_budget_is_not_kept_and_later_references_have_no_image(monkeypatch):
    first = images.to_png(B.GRAY_PNG)
    monkeypatch.setattr(figures, "ASSET_LIMIT", len(first.data))  # 첫 그림만 들어간다
    docs = []
    finish = parser._finish
    monkeypatch.setattr(parser, "_finish", lambda doc: docs.append(doc) or finish(doc))
    data = B.package(B.section(B.para(B.run(B.pic("image1"))), B.para(B.run(B.pic("image2"))),
                               B.para(B.run(B.pic("image2")))),
                     items=[("image1", "BinData/image1.png", "image/png", True),
                            ("image2", "BinData/image2.png", "image/png", True)],
                     binaries={"BinData/image1.png": B.GRAY_PNG, "BinData/image2.png": B.RGBA_PNG})
    parsed = HwpxParser().parse(data, "a.hwpx")
    assert [("figure" in b) for b in parsed.blocks] == [True, False, False] and len(parsed.assets) == 1
    assert reasons(parsed) == ["figure_image_budget", "figure_image_budget"]
    (doc,) = docs
    assert doc.pictures["BinData/image2.png"] == "figure_image_budget"
    assert [href for href, v in doc.pictures.items() if isinstance(v, images.Png)] == ["BinData/image1.png"]


def test_a_picture_inside_a_caption_is_not_silently_lost():
    inner = B.pic("image1")
    data = B.package(B.section(B.para(B.run(B.pic("image1", B.caption(B.para(B.run(B.t("그림 4. 지어낸 캡션"), inner))))))),
                     items=[("image1", "BinData/image1.png", "image/png", True)],
                     binaries={"BinData/image1.png": B.GRAY_PNG})
    parsed, counts = HwpxParser()._parse(data, "a.hwpx")
    assert [b["kind"] for b in parsed.blocks] == ["figure", "caption", "figure"]
    assert parsed.blocks[0]["figure"]["caption_ref"] == 1 and "figure" in parsed.blocks[2]
    assert parsed.blocks[1]["text"] == "그림 4. 지어낸 캡션" and len(parsed.assets) == 1
    assert counts[0].ok and parsed.regions == ()
