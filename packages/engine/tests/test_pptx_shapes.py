"""PPTX 크기 규칙 제목(스펙 §8.3): 크기 사슬의 각 단계, 슬라이드마다 가장 위 문단 하나, 그룹 변환, 후보가 아닌 것.
합성 문서는 build_ooxml_fixtures.py로 만든다."""

import importlib.util
from pathlib import Path

from hanji.formats.ooxml.pptx.parser import PptxParser

HERE = Path(__file__).resolve().parent


def _load_builder():
    spec = importlib.util.spec_from_file_location("engine_build_ooxml_fixtures", HERE / "build_ooxml_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_builder()
BODY = B.sp("지어낸 본문 문단은 크기 규칙의 본문 크기를 정할 만큼 길게 쓴다")  # 크기 없음: 사슬 끝 18pt


def heads(*slides: str, **kw) -> list[tuple[str, int, float]]:
    """(제목 글자, 슬라이드, 신뢰도)."""
    parsed, counts = PptxParser()._parse(B.pptx(*slides, **kw), "a.pptx")
    assert all(c.ok for c in counts)
    return [(b["text"], b["locator"].slide, b["confidence"]) for b in parsed.blocks if b["kind"] == "heading"]


def test_each_step_of_the_size_chain_and_direct_formatting_first():
    slides = [
        B.slide(BODY, B.sp(B.para(B.a_r("런 크기", sz=2800)), y=0)),
        B.slide(BODY, B.sp(B.para("문단 기본 크기", def_sz=2800), y=0)),
        B.slide(BODY, B.sp("도형 목록 스타일", lst=B.levels(sz=2800), y=0)),
        B.slide(BODY, B.sp(B.para("마스터 둘째 수준", lvl=1), y=0)),
        B.slide(BODY, B.sp(B.para("발표 셋째 수준", lvl=2), y=0)),
        B.slide(BODY, B.sp(B.para("문단 기본이 목록 스타일보다 앞", def_sz=1800), lst=B.levels(sz=2800), y=0)),
        B.slide(BODY, B.sp(B.para(B.a_r("런이 문단 기본보다 앞", sz=1800), def_sz=2800), y=0)),
        B.slide(BODY, B.sp(B.para(B.a_r("굵은 작은 제목", sz=1900, b="1")), y=0),
                B.sp(B.para(B.a_r("안 굵은 같은 크기", sz=1900)), y=0)),
    ]
    got = heads(*slides, other_style=B.levels(sz=2800, lvl=2), default_style=B.levels(sz=2800, lvl=3))
    assert got == [("런 크기", 1, 0.6), ("문단 기본 크기", 2, 0.6), ("도형 목록 스타일", 3, 0.6), ("마스터 둘째 수준", 4, 0.6),
                   ("발표 셋째 수준", 5, 0.6), ("굵은 작은 제목", 8, 0.6)]


def test_one_topmost_paragraph_per_slide_with_group_transforms_and_exclusions():
    big = 3200
    slides = [
        B.slide(BODY, B.sp(B.para(B.a_r("아래 큰 글", sz=big)), y=3_000_000), B.sp(B.para(B.a_r("위 큰 글", sz=big)),
                                                                              y=1_000_000)),
        B.slide(BODY, B.sp(B.para(B.a_r("같은 높이 앞", sz=big)), y=500_000),
                B.sp(B.para(B.a_r("같은 높이 뒤", sz=big)), y=500_000)),
        B.slide(BODY, B.sp(B.para(B.a_r("그룹 밖 글", sz=big)), y=1_000_000),
                B.grp(B.sp(B.para(B.a_r("그룹 안 글", sz=big)), y=1_800_000), ext=B.EMU, ch_ext=2 * B.EMU)),
        B.slide(BODY, B.grp(B.sp(B.para(B.a_r("변환 없는 그룹 글", sz=big)), y=0), ch_ext=None),
                B.sp(B.para(B.a_r("변환 있는 글", sz=big)), y=2_000_000)),
        B.slide(BODY, B.sp("자리 제목", ph="title"), B.sp(B.para(B.a_r("제목 있는 슬라이드 큰 글", sz=big)), y=0)),
        B.slide(BODY, B.sp(B.para(B.a_r("본문 자리 큰 글", sz=big)), ph="body"),
                B.sp(B.para(B.a_r("큰 글머리", sz=big), bullet="char"), y=0),
                B.sp(B.para(B.a_r("큰", sz=big), "", B.a_r("세", sz=big), "", B.a_r("줄", sz=big)), y=0),
                B.sp(B.para(B.a_r("좌표 없는 큰 글", sz=big)))),
    ]
    assert heads(*slides) == [("위 큰 글", 1, 0.6), ("같은 높이 앞", 2, 0.6), ("그룹 안 글", 3, 0.6), ("변환 있는 글", 4, 0.6),
                              ("자리 제목", 5, 1.0)]


def test_hidden_title_placeholder_does_not_count_as_a_title_and_stays_hidden():
    big = 3200
    slide = B.slide(BODY, B.sp("숨긴 자리 제목", ph="title", hidden=True), B.sp(B.para(B.a_r("보이는 큰 글", sz=big)), y=0))
    parsed, counts = PptxParser()._parse(B.pptx(slide), "a.pptx")
    assert all(c.ok for c in counts)
    got = [(b["kind"], b["text"], b.get("level"), b["confidence"]) for b in parsed.blocks if b["kind"] == "heading"]
    assert got == [("heading", "보이는 큰 글", 1, 0.6)]
    assert all("숨긴 자리 제목" not in b["text"] for b in parsed.blocks)
    assert counts[0].hidden == 6  # 숨긴 제목 글자는 장부 숨김
    assert [r.fallback_reason for r in parsed.regions] == ["hidden_text"]
