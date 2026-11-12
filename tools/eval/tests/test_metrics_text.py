import math

import pytest

from hanji_eval.blocks import Cell, Table
from hanji_eval.metrics import heading, numbers, order, text
from hanji_eval.metrics.numbers import Token


def test_nid_and_cer_hand_values():
    assert text.nid("가나다라", "가나다마") == pytest.approx(0.75)
    assert text.nid("", "") == 1.0 and text.nid("가", "") == 0.0
    assert text.cer("가나다라", "가나다마") == pytest.approx(0.25)
    assert text.cer("가나다라마바", "가") == 1.0
    assert (text.cer("", ""), text.cer("가", "")) == (0.0, 1.0)


def test_hangul_precision_recall_use_syllable_multisets():
    p, r = text.hangul_pr("가가나 A1", "가나나다")
    assert (p, r) == (pytest.approx(2 / 3), pytest.approx(0.5))
    assert text.f1(p, r) == pytest.approx(4 / 7)
    assert text.hangul_pr("123", "가") == (None, 0.0)
    assert text.f1(None, 1.0) is None and text.f1(0.0, 0.0) == 0.0


def test_order_gap_is_unordered_f1_minus_ordered_nid():
    assert text.order_gap("나다가", "가나다") == pytest.approx(1 / 3)
    assert text.order_gap("가나다", "가나다") == pytest.approx(0.0)
    assert text.order_gap("abc", "") is None


def test_class_and_missing_rates():
    assert text.class_rates({"insertion": 2, "outside_gt": 1}, 10) == {
        "outside_gt": 0.1, "insertion": 0.2, "duplicate": 0.0, "ambiguous": 0.0}
    with pytest.raises(ValueError, match="unknown prediction classes"):
        text.class_rates({"hallucination": 1}, 10)
    assert text.missing_rates(200, 10, 30) == {"missing": 0.2, "recorded": 0.05, "unrecorded": 0.15}
    with pytest.raises(ValueError):
        text.missing_rates(10, 6, 6)


def test_mean_and_weighted_mean():
    assert text.mean([0.5, 1.0]) == 0.75 and text.mean([]) is None
    assert text.weighted_mean([0.5, 1.0], [3, 1]) == pytest.approx(0.625)
    assert text.weighted_mean([0.5], [0]) is None


@pytest.mark.parametrize(("source", "expected"), [
    ("△1,234.5억원", [("△", "1,234.5", "억원")]),
    ("증가율 -3.2%p, 인구 12,345명", [("-", "3.2", "%p"), ("", "12,345", "명")]),
    ("2026년 3 월", [("", "2026", "년"), ("", "3", "월")]),
    ("1,2,3", [("", "1", ""), ("", "2", ""), ("", "3", "")]),
    ("▲10% ▽0.5", [("▲", "10", "%"), ("▽", "0.5", "")]),
    ("5개월 5개 5개사", [("", "5", "개월"), ("", "5", "개"), ("", "5", "개사")]),
    ("+ 5 10km2 3만명", [("", "5", ""), ("", "10", "km2"), ("", "3", "만명")]),
])
def test_num_v1_tokens(source, expected):
    assert numbers.tokens(source) == [Token(*t) for t in expected]


def test_the_same_number_with_another_unit_is_another_token():
    assert numbers.tokens("100원") != numbers.tokens("100만원")
    assert numbers.token_pr(numbers.tokens("100원 5명"), numbers.tokens("100만원 5명")) == (0.5, 0.5)


def _table(amounts: tuple[str, str], merged: bool = False) -> Table:
    head = (Cell(0, 0, text="2025년"), Cell(0, 1, text="2026년"))
    if merged:
        return Table(2, 2, head + (Cell(1, 0, 1, 2, f"{amounts[0]} {amounts[1]}"),))
    return Table(2, 2, head + (Cell(1, 0, text=amounts[0]), Cell(1, 1, text=amounts[1])))


def test_numbers_in_tables_are_scored_cell_by_cell_only_when_the_structure_matches():
    truth = _table(("100억원", "200억원"))
    assert numbers.table_cells(truth, _table(("100억원", "200억원"))) == numbers.CellScore(4, 4, 0)
    assert numbers.table_cells(truth, _table(("200억원", "100억원"))) == numbers.CellScore(4, 2, 0)
    assert numbers.table_cells(truth, _table(("200억원", "100억원"), merged=True)) == numbers.CellScore(4, 0, 4)
    assert numbers.table_cells(truth, None) == numbers.CellScore(4, 0, 4)


@pytest.mark.parametrize(("pred", "truth", "expected"), [
    ([1, 2, 3, 4], [1, 2, 3, 4], 1.0),
    ([4, 3, 2, 1], [1, 2, 3, 4], -1.0),
    ([1, 3, 2, 4], [1, 2, 3, 4], 2 / 3),
    ([1, 1, 2], [1, 2, 3], 2 / math.sqrt(6)),
])
def test_kendall_tau_b(pred, truth, expected):
    assert order.kendall_tau(pred, truth) == pytest.approx(expected)


def test_kendall_tau_is_undefined_for_fewer_than_two_pairs_or_all_ties():
    assert order.kendall_tau([1], [1]) is None
    assert order.kendall_tau([1, 1, 1], [1, 2, 3]) is None
    with pytest.raises(ValueError):
        order.kendall_tau([1, 2], [1])


def test_heading_scores_count_paired_headings_and_level_agreement():
    score = heading.heading_scores({0: 1, 3: 2, 5: 1}, {0: 1, 2: 2}, [(0, 0), (3, 2), (5, 4), (1, 1)])
    assert (score.precision, score.recall, score.level_agreement) == (pytest.approx(2 / 3), 1.0, 1.0)
    assert score.f1 == pytest.approx(0.8)
    mismatch = heading.heading_scores({0: 2}, {0: 1}, [(0, 0)])
    assert mismatch.level_agreement == 0.0 and mismatch.f1 == 1.0
    empty = heading.heading_scores({}, {}, [])
    assert (empty.precision, empty.recall, empty.f1, empty.level_agreement) == (None, None, None, None)


def test_spaced_units_attach_across_any_whitespace_and_adjacent_ones_only_when_touching():
    assert numbers.tokens("5 명") == numbers.tokens("5\n명") == numbers.tokens("5명") == [Token("", "5", "명")]
    assert numbers.tokens("12 %") == numbers.tokens("12\n\t%") == [Token("", "12", "%")]
    assert numbers.tokens("3 개월") == [Token("", "3", "개월")]
    assert numbers.tokens("3대") == [Token("", "3", "대")]
    assert numbers.tokens("3 대학") == numbers.tokens("3\n대학") == [Token("", "3", "")]
    assert numbers.tokens("2 시간") == [Token("", "2", "시간")] and numbers.tokens("2 시청") == [Token("", "2", "")]
    assert numbers.tokens("3만원") == numbers.tokens("3 만원") == [Token("", "3", "만원")]
    assert numbers.tokens("3만 원") == [Token("", "3", "만")]


def test_numbers_in_text_blocks_compare_whole_token_sequences():
    assert numbers.block_exact("올해 1,200억원, 작년 900억원", "올해 1,200억원, 작년 900억원") is True
    assert numbers.block_exact("올해 900억원, 작년 1,200억원", "올해 1,200억원, 작년 900억원") is False
    assert numbers.block_exact("", "모두 12명") is False
    assert numbers.block_exact("숫자 7개", "숫자 없는 문단") is None
    assert numbers.exact_rate([True, False, None, True]) == pytest.approx(2 / 3)
    assert numbers.exact_rate([None]) is None


def test_header_footer_precision_and_recall_hand_values():
    p, r = text.header_footer_pr(["보도자료", "1"], ["보도자료", "보도자료"])
    assert (p, r) == (pytest.approx(4 / 5), pytest.approx(4 / 8))
    assert text.header_footer_pr([], ["머리"]) == (None, 0.0)
