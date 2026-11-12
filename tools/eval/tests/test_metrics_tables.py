import unicodedata
from pathlib import Path

import pytest

from hanji_eval.blocks import Cell, Table
from hanji_eval.metrics import grits, tables, teds


def _t(n_rows: int, n_cols: int, *cells: tuple[int, int, int, int, str]) -> Table:
    return Table(n_rows, n_cols, tuple(Cell(r, c, rs, cs, x) for r, c, rs, cs, x in cells))


# 합성 표 짝 10개(글자는 지어낸 것)와 기대값 (TEDS, TEDS-S, GriTS-Con, GriTS-Top). 기대값은 계획 단계에 격리 venv에서
# 원 구현(PubTabNet metric.py + Distance 0.1.3 + lxml, table-transformer grits.py + numpy + PyMuPDF)으로 한 번 구해
# 숫자만 적었다(하네스 코드로 만들지 않는다)
CASES = [
    ("identical",
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합계"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120")),
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합계"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120")),
     (1.0, 1.0, 1.0, 1.0)),
    ("one_char",
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합계"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120")),
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합게"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120")),
     (0.9285714285714286, 1.0, 0.875, 1.0)),
    ("extra_row",
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합계"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120")),
     _t(3, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합계"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120"),
        (2, 0, 1, 1, "부산"), (2, 1, 1, 1, "80")),
     (0.7, 0.7, 0.8, 0.8)),
    ("missing_col",
     _t(2, 3, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "남"), (0, 2, 1, 1, "여"), (1, 0, 1, 1, "인원"), (1, 1, 1, 1, "12"),
        (1, 2, 1, 1, "15")),
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "남"), (1, 0, 1, 1, "인원"), (1, 1, 1, 1, "12")),
     (0.7777777777777778, 0.7777777777777778, 0.8, 0.8)),
    ("split_merge",
     _t(2, 2, (0, 0, 1, 2, "연도별 현황"), (1, 0, 1, 1, "2025"), (1, 1, 1, 1, "2026")),
     _t(2, 2, (0, 0, 1, 1, "연도별 현황"), (0, 1, 1, 1, ""), (1, 0, 1, 1, "2025"), (1, 1, 1, 1, "2026")),
     (0.7142857142857143, 0.7142857142857143, 0.75, 0.75)),
    ("rowspan_shift",
     _t(3, 2, (0, 0, 2, 1, "지역"), (0, 1, 1, 1, "가"), (1, 1, 1, 1, "나"), (2, 0, 1, 1, "계"), (2, 1, 1, 1, "다")),
     _t(3, 2, (0, 0, 2, 1, "지역"), (0, 1, 1, 1, "나"), (1, 1, 1, 1, "다"), (2, 0, 1, 1, "계"), (2, 1, 1, 1, "가")),
     (0.6666666666666667, 1.0, 0.5, 1.0)),
    ("empty_cells",
     _t(2, 3, (0, 0, 1, 1, ""), (0, 1, 1, 1, "가"), (0, 2, 1, 1, ""), (1, 0, 1, 1, "나"), (1, 1, 1, 1, ""),
        (1, 2, 1, 1, "다")),
     _t(2, 3, (0, 0, 1, 1, ""), (0, 1, 1, 1, "가"), (0, 2, 1, 1, "라"), (1, 0, 1, 1, "나"), (1, 1, 1, 1, ""),
        (1, 2, 1, 1, "다")),
     (0.8888888888888888, 1.0, 0.8333333333333334, 1.0)),
    ("transposed",
     _t(2, 3, (0, 0, 1, 1, "가"), (0, 1, 1, 1, "나"), (0, 2, 1, 1, "다"), (1, 0, 1, 1, "1"), (1, 1, 1, 1, "2"),
        (1, 2, 1, 1, "3")),
     _t(3, 2, (0, 0, 1, 1, "가"), (0, 1, 1, 1, "1"), (1, 0, 1, 1, "나"), (1, 1, 1, 1, "2"), (2, 0, 1, 1, "다"),
        (2, 1, 1, 1, "3")),
     (0.30000000000000004, 0.5, 0.3333333333333333, 0.6666666666666666)),
    ("different_text",
     _t(2, 2, (0, 0, 1, 1, "구분"), (0, 1, 1, 1, "합계"), (1, 0, 1, 1, "서울"), (1, 1, 1, 1, "120")),
     _t(2, 2, (0, 0, 1, 1, "항목"), (0, 1, 1, 1, "비율"), (1, 0, 1, 1, "대구"), (1, 1, 1, 1, "7.5%")),
     (0.4285714285714286, 1.0, 0.125, 1.0)),
    ("other_merge",
     _t(3, 3, (0, 0, 1, 3, "총괄표"), (1, 0, 2, 1, "구분"), (1, 1, 1, 1, "가"), (1, 2, 1, 1, "나"),
        (2, 1, 1, 1, "10"), (2, 2, 1, 1, "20")),
     _t(3, 3, (0, 0, 1, 2, "총괄표"), (0, 2, 1, 1, ""), (1, 0, 1, 1, "구분"), (1, 1, 1, 1, "가"), (1, 2, 1, 1, "나"),
        (2, 0, 1, 1, ""), (2, 1, 1, 1, "10"), (2, 2, 1, 1, "20")),
     (0.6666666666666667, 0.6666666666666667, 0.7777777777777778, 0.7407407407407407)),
]


@pytest.mark.parametrize(("name", "truth", "pred", "expected"), CASES, ids=[c[0] for c in CASES])
def test_ported_metrics_match_the_original_implementations(name, truth, pred, expected):
    got = (teds.teds(pred, truth), teds.teds(pred, truth, structure_only=True), grits.grits_con(pred, truth).fscore,
           grits.grits_top(pred, truth).fscore)
    assert got == pytest.approx(expected, abs=1e-6)


def test_teds_of_a_missing_table_is_zero():
    table = CASES[0][1]
    assert teds.teds(None, table) == 0.0 and teds.teds(table, None) == 0.0


def test_grits_treats_an_uncovered_position_as_an_empty_cell():
    holed = _t(2, 2, (0, 0, 1, 1, "가"), (0, 1, 1, 1, "나"), (1, 0, 1, 1, "다"))
    filled = _t(2, 2, (0, 0, 1, 1, "가"), (0, 1, 1, 1, "나"), (1, 0, 1, 1, "다"), (1, 1, 1, 1, ""))
    assert grits.grits_con(holed, filled).fscore == 1.0
    assert grits.grits_top(holed, filled).fscore == 1.0


def test_perfect_needs_the_same_structure_and_the_same_cell_text_without_spaces():
    truth = _t(2, 2, (0, 0, 1, 2, "연도별 현황"), (1, 0, 1, 1, "2025"), (1, 1, 1, 1, "2026"))
    assert tables.perfect(_t(2, 2, (0, 0, 1, 2, "연도별현황"), (1, 0, 1, 1, "2025"), (1, 1, 1, 1, " 2026")), truth)
    assert not tables.perfect(_t(2, 2, (0, 0, 1, 2, "연도별 현황"), (1, 0, 1, 1, "2025"), (1, 1, 1, 1, "2027")),
                              truth)
    assert not tables.perfect(_t(2, 2, (0, 0, 1, 1, "연도별 현황"), (0, 1, 1, 1, ""), (1, 0, 1, 1, "2025"),
                                 (1, 1, 1, 1, "2026")), truth)
    assert not tables.perfect(None, truth)


@pytest.mark.parametrize("pred_cell", ["2025\u00a0", "20\u200925", "\u2003\v2025\f\x85"], ids=["nbsp", "thin", "other"])
def test_perfect_removes_every_unicode_whitespace_like_tableeval(pred_cell):
    # 컨트롤러 판정 R7: research/tableeval의 norm() = NFC 뒤 re.sub(r"\s+", "")
    truth = _t(1, 2, (0, 0, 1, 1, "연도"), (0, 1, 1, 1, "2025"))
    assert tables.perfect(_t(1, 2, (0, 0, 1, 1, "연도"), (0, 1, 1, 1, pred_cell)), truth)


def test_perfect_compares_hangul_after_nfc():
    nfd = unicodedata.normalize("NFD", "연도")
    assert nfd != "연도"
    truth = _t(1, 1, (0, 0, 1, 1, "연도"))
    assert tables.perfect(_t(1, 1, (0, 0, 1, 1, nfd)), truth)
    assert tables.perfect(truth, _t(1, 1, (0, 0, 1, 1, nfd)))


def test_grits_scores_are_always_floats():
    empty, letter = _t(1, 1, (0, 0, 1, 1, "")), _t(1, 1, (0, 0, 1, 1, "a"))
    for score in (grits.grits_con(empty, letter), grits.grits_top(empty, letter), grits.grits_con(letter, letter)):
        assert all(type(v) is float for v in score)


def test_detection_precision_and_recall():
    assert tables.detection_pr(3, 4, 6) == (0.75, 0.5)
    assert tables.detection_pr(0, 0, 2) == (None, 0.0)
    with pytest.raises(ValueError):
        tables.detection_pr(3, 2, 6)


def test_ported_files_keep_their_origin_and_both_notices_list_them():
    root = Path(__file__).resolve().parents[3]
    eval_notice = (root / "tools" / "eval" / "NOTICE").read_text(encoding="utf-8")
    root_notice = (root / "NOTICE").read_text(encoding="utf-8")
    for module, commit, license_name in [(teds, "8ffde9024bd331f5a61c3c549fdf30dd3c3e43a5", "Apache 2.0"),
                                         (grits, "16d124f616109746b7785f03085100f1f6247575", "MIT License")]:
        head = Path(module.__file__).read_text(encoding="utf-8")[:1200]
        assert commit in head and license_name in head
        assert commit in eval_notice
        assert Path(module.__file__).name in root_notice
