import math
from collections import Counter

import pytest

from hanji_eval import guard
from hanji_eval.metrics import bootstrap, calib, failures
from hanji_eval.metrics.bootstrap import Unit

# 기대값: scipy 1.18.1 beta.ppf(1 − α, x + 1, n − x)로 계획 단계에 격리 venv에서 한 번 구했다(스펙 §6.6·§11 표의 값과
# 소수 둘째 자리(%)까지 같다)
CP = [
    (0.05, 0, 30, 0.09503385285530411), (0.05, 0, 58, 0.050339338330420105), (0.05, 0, 59, 0.04950760988822693),
    (0.05, 0, 60, 0.04870291331009749), (0.05, 1, 93, 0.049993978387988317), (0.05, 2, 124, 0.04990017122831202),
    (0.025, 0, 60, 0.05962949228616691), (0.025, 0, 71, 0.0506294079544037), (0.025, 0, 72, 0.049944083705854674),
    (0.025, 1, 109, 0.05005682308481245), (0.025, 1, 110, 0.04961118931198478),
    (0.025, 2, 141, 0.050298329586637054), (0.025, 2, 142, 0.04995063305485951),
]
TABLE_PERCENT = {(0.05, 0, 30): 9.50, (0.05, 0, 58): 5.03, (0.05, 0, 59): 4.95, (0.05, 0, 60): 4.87,
                 (0.05, 1, 93): 5.00, (0.05, 2, 124): 4.99, (0.025, 0, 60): 5.96, (0.025, 0, 71): 5.06,
                 (0.025, 0, 72): 4.99, (0.025, 1, 109): 5.01, (0.025, 1, 110): 4.96, (0.025, 2, 141): 5.03,
                 (0.025, 2, 142): 5.00}


@pytest.mark.parametrize(("alpha", "x", "n", "expected"), CP)
def test_clopper_pearson_upper_bounds(alpha, x, n, expected):
    got = bootstrap.cp_upper(x, n, alpha)
    assert got == pytest.approx(expected, abs=1e-9)
    assert round(got * 100, 2) == TABLE_PERCENT[(alpha, x, n)]


def test_clopper_pearson_edges_and_minimum_trials():
    assert bootstrap.cp_upper(7, 7) == 1.0
    assert bootstrap.cp_upper(0, 30) == pytest.approx(1 - 0.05 ** (1 / 30))
    assert [bootstrap.min_trials(x, 0.025, 0.05) for x in (0, 1, 2)] == [72, 110, 142]
    assert [bootstrap.min_trials(x, 0.05, 0.05) for x in (0, 1, 2)] == [59, 93, 124]
    with pytest.raises(ValueError):
        bootstrap.cp_upper(3, 2)


def _units(publishers: int, docs: int, value=lambda p, d: float((p + d) % 3 == 0)) -> list[Unit]:
    return [Unit(f"pub-{p:03d}", f"doc-{p}-{d}", value(p, d)) for p in range(publishers) for d in range(docs)]


def test_the_cluster_bootstrap_is_reproducible_with_a_fixed_seed():
    units = _units(10, 4)
    a, b = bootstrap.cluster_ci(units), bootstrap.cluster_ci(units)
    assert a == b and a.flags == ()
    assert a.lo <= bootstrap.weighted_mean(units) <= a.hi
    assert bootstrap.resamples(units, seed=1)[0] != bootstrap.resamples(units)[0]


def test_units_of_one_document_move_together():
    units = [Unit(f"pub-{p}", f"doc-{p}-{d}", float(r), 1.0) for p in range(9) for d in range(3) for r in (0, 1)]
    _, _, by_doc = bootstrap._docs(units)
    samples, few = bootstrap.resamples(units, reps=50)
    assert not few
    for drawn in samples:
        picked = Counter(u.doc for d in drawn for u in by_doc[d])
        assert all(n % 2 == 0 for n in picked.values())
        assert all({u.value for u in by_doc[d]} == {0.0, 1.0} for d in drawn)


def test_fewer_than_eight_publishers_fall_back_to_document_resampling():
    interval = bootstrap.cluster_ci(_units(7, 5))
    assert interval.flags == ("few_clusters",)
    assert bootstrap.cluster_ci([]).flags == ("no_units",)


def test_the_paired_bootstrap_uses_the_same_resamples():
    a = _units(9, 3)
    assert bootstrap.paired_ci(a, a) == bootstrap.Interval(0.0, 0.0, ())
    shifted = [Unit(u.publisher, u.doc, u.value - 0.1) for u in a]
    diff = bootstrap.paired_ci(a, shifted)
    assert diff.lo == pytest.approx(0.1) and diff.hi == pytest.approx(0.1)
    with pytest.raises(ValueError, match="same documents"):
        bootstrap.paired_ci(a, a[:-1])


def test_rare_events_use_clopper_pearson_instead_of_the_bootstrap():
    none_failed = _units(10, 6, lambda p, d: 0.0)
    assert bootstrap.rate_interval(none_failed) == bootstrap.Interval(0.0, bootstrap.cp_upper(0, 60),
                                                                      ("one_sided_upper",))
    ten = _units(10, 6, lambda p, d: float(d == 0))
    assert bootstrap.rate_interval(ten).flags == ("one_sided_upper",)
    eleven = ten + [Unit("pub-000", "doc-x", 1.0)]
    assert bootstrap.rate_interval(eleven).flags == ()
    almost_all = _units(10, 6, lambda p, d: float(not (p == 0 and d == 0)))
    low = bootstrap.rate_interval(almost_all)
    assert low == bootstrap.Interval(1.0 - bootstrap.cp_upper(1, 60), 1.0, ("one_sided_lower",))


def test_ece_uses_fifteen_equal_integer_bins():
    conf, labels = [0.2, 0.2, 0.6, 0.6, 0.6, 1.0], [0, 1, 1, 1, 0, 1]
    assert calib.ece(conf, labels) == pytest.approx(0.1 + 1 / 30, abs=1e-12)
    assert calib.ece(conf[::-1], labels[::-1]) == calib.ece(conf, labels)
    assert [k for k, *_ in calib.reliability(conf, labels)] == [3, 9, 14]
    assert [calib.bin_of(c) for c in (0.0, 0.199, 0.2, 0.933, 0.934, 1.0)] == [0, 2, 3, 13, 14, 14]
    assert calib.ece_same_value(conf, labels) == pytest.approx(0.1 + 1 / 30, abs=1e-12)


def test_brier_and_aurc_hand_values():
    assert calib.brier([0.2, 0.8], [0, 1]) == pytest.approx(0.04)
    assert calib.aurc([0.9, 0.9, 0.5], [1, 0, 1]) == pytest.approx(4 / 9)
    assert calib.aurc([0.9, 0.9, 0.5], [0, 1, 1]) == calib.aurc([0.9, 0.9, 0.5], [1, 0, 1])
    assert calib.selective_risk([0.9, 0.9, 0.5], [1, 0, 1], 2 / 3) == pytest.approx(0.5)
    assert calib.selective_risk([0.9, 0.9, 0.5], [1, 0, 1], 1.0) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        calib.ece([1.2], [1])


@pytest.mark.parametrize("metric", ["nid", "hangul_f1", "hangul_recall", "teds", "teds_s", "grits_con", "grits_top",
                                    "table_perfect", "heading_f1", "header_footer_precision", "header_footer_recall",
                                    "number_exact"])
def test_failed_documents_score_zero_and_stay_in_the_denominator(metric):
    out = failures.apply(metric, {"pdf-0001": 0.9, "pdf-0002": 0.8}, ["pdf-0003"])
    assert out.values == (("pdf-0001", 0.9), ("pdf-0002", 0.8), ("pdf-0003", 0.0)) and out.n_failed == 1


def test_failed_documents_score_one_for_cer():
    out = failures.apply("cer", {"pdf-0001": 0.1}, ["pdf-0002"])
    assert out.values == (("pdf-0001", 0.1), ("pdf-0002", 1.0))


@pytest.mark.parametrize("metric", ["hangul_precision", "table_precision", "teds_matched", "teds_s_matched", "ece",
                                    "brier", "aurc", "seconds_per_page", "max_rss_mib"])
def test_conditional_metrics_drop_failed_documents_and_count_them(metric):
    out = failures.apply(metric, {"pdf-0001": 0.9}, ["pdf-0002", "pdf-0003"])
    assert out.values == (("pdf-0001", 0.9),) and out.n_failed == 2


def test_kendall_tau_drops_failed_and_undefined_documents():
    out = failures.apply("kendall_tau", {"pdf-0001": 0.5, "pdf-0002": None}, ["pdf-0003"])
    assert (out.values, out.n_failed, out.n_undefined) == ((("pdf-0001", 0.5),), 1, 1)


def test_table_recall_counts_every_table_of_a_failed_document_as_missed():
    assert failures.table_recall({"pdf-0001": (2, 3)}, {"pdf-0002": 4}) == (2, 7)


def test_unknown_metrics_and_failed_documents_with_values_are_errors():
    with pytest.raises(ValueError, match="no failure rule"):
        failures.apply("speed", {}, [])
    with pytest.raises(ValueError, match="must not have a value"):
        failures.apply("nid", {"pdf-0001": 1.0}, ["pdf-0001"])
    assert math.isnan(bootstrap.weighted_mean([]))


def test_every_reported_metric_has_exactly_one_failure_rule():
    groups = [set(failures.WORST), set(failures.EXCLUDE), set(failures.COUNTED)]
    assert set().union(*groups) == set(guard.METRICS)
    assert sum(len(g) for g in groups) == len(guard.METRICS)
    with pytest.raises(ValueError, match="no failure rule"):
        failures.apply("table_recall", {}, [])  # 세는 지표는 table_recall()로


def test_the_two_stage_resample_is_pinned_and_draws_publishers_first():
    units = [Unit(f"pub-{p}", f"doc-{p}-{d}", float((p * 7 + d * 3) % 5) / 4)
             for p in range(8) for d in range(p % 3 + 1)]
    samples, few = bootstrap.resamples(units, reps=3)
    assert not few
    assert samples[0] == ["doc-6-0", "doc-0-0", "doc-7-1", "doc-7-1", "doc-7-1", "doc-7-0", "doc-2-1", "doc-2-0",
                          "doc-2-0", "doc-4-0", "doc-4-1", "doc-1-0", "doc-1-1", "doc-7-0", "doc-7-1"]
    assert len({len(s) for s in samples}) > 1  # 뽑힌 발행처의 문서 수만큼 뽑으므로 표본 크기가 변한다
    interval = bootstrap.cluster_ci(units)  # 2,000번, 95%
    assert (interval.lo, interval.hi) == (pytest.approx(0.22727272727272727), pytest.approx(0.6111111111111112))


def test_the_cluster_interval_is_wider_than_the_document_interval_when_publishers_differ():
    # 발행처 안 문서는 값이 같고 발행처끼리 다르다: 문서를 독립으로 보면 구간이 지나치게 좁다
    units = [Unit(f"pub-{p}", f"doc-{p}-{d}", float(p % 2)) for p in range(10) for d in range(8)]
    as_docs = [Unit(u.doc, u.doc, u.value) for u in units]
    cluster, docs = bootstrap.cluster_ci(units), bootstrap.cluster_ci(as_docs)
    assert cluster.hi - cluster.lo > 2 * (docs.hi - docs.lo)
    assert {len(s) for s in bootstrap.resamples(as_docs, reps=5)[0]} == {80}


def test_selective_risk_is_not_pushed_by_float_noise():
    conf = [1.0 - i / 100 for i in range(30)]
    labels = [1, 1, 0] + [1] * 27
    assert calib.selective_risk(conf, labels, 0.1) == pytest.approx(1 / 3)  # i = 3, 0.1 × 30 = 3.0000000000000004
