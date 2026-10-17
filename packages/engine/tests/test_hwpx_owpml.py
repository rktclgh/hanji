"""공유 구역 처리부(hwpx/owpml.py, HWP 스펙 §3)와 공용 크기 규칙의 선형 시간(HWP 스펙 §5.4)."""

import importlib.util
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from hanji.formats.flowdoc import headings_by_size
from hanji.formats.hwpx.container import Item
from hanji.formats.hwpx.header import Styles
from hanji.formats.hwpx.owpml import _Doc, _finish, _Section

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_hwpx_fixtures", HERE / "build_hwpx_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()


class FakePictures:
    """zip 꾸러미가 아닌 그림 원천(HWP BinData 자리): 항목·유무·바이트만 있다."""

    def __init__(self) -> None:
        self.items = {"7": Item("BinData/BIN0007.png", "", True), "8": Item("BinData/BIN0008.png", "", False)}
        self.read: list[str] = []

    def has(self, member: str) -> bool:
        return member == "BinData/BIN0007.png"

    def binary(self, member: str) -> bytes:
        self.read.append(member)
        return B.GRAY_PNG


def test_the_shared_section_processor_reads_pictures_through_the_protocol():
    pictures = FakePictures()
    doc = _Doc("a.hwp", Styles(), pictures)
    root = ET.fromstring(B.section(B.para(B.run(B.t("그림 앞 문단"))), B.para(B.run(B.pic("7"))),
                                   B.para(B.run(B.pic("8"))), B.para(B.run(B.pic("9")))))
    _Section(doc, 0, root, 0).build()
    blocks, regions = _finish(doc)
    assert [(b["kind"], "figure" in b) for b in blocks] == [("paragraph", False), ("figure", True), ("figure", False),
                                                           ("figure", False)]
    assert [r.fallback_reason for r in regions] == ["figure_image_linked", "figure_image_missing"]
    assert pictures.read == ["BinData/BIN0007.png"] and len(doc.budget.assets) == 1


def test_size_rule_levels_are_linear_in_the_number_of_distinct_heading_sizes():
    """서로 다른 크기 제목 4만 개: 크기 → 단계 사전이라 선형이다(목록 index는 이차 시간이라 수 초가 걸린다)."""
    n = 40_000
    specs = [{"kind": "paragraph", "text": "본문", "_sizes": Counter({(10.0, False): 10**6}), "_size": 10.0,
              "_bold": False, "_lines": 1}]
    specs += [{"kind": "paragraph", "text": f"제목 {k}", "_sizes": Counter(), "_size": 20.0 + k / 2, "_bold": False,
               "_lines": 1} for k in range(n)]
    start = time.perf_counter()
    headings_by_size(specs)
    assert time.perf_counter() - start < 1.0
    assert [s.get("level") for s in specs[-7:]] == [6, 6, 5, 4, 3, 2, 1] and specs[0]["kind"] == "paragraph"
