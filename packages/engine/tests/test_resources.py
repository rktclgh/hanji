"""실행 모델 자원: ORT 스레드 손잡이, 메모리·CPU 한도 알아내기(가짜 /proc·cgroup 파일을 임시 뿌리에), 크기 문자열,
비용·입장 산수. onnxruntime이 필요한 테스트는 실제 모델 세션을 연다."""

import os
from types import SimpleNamespace

import pytest

from hanji import models, resources
from hanji.errors import HanjiError
from hanji.formats.pdf import layout, ocr


def require_models(*names: str) -> None:
    """실제 모델 파일이 필요한 테스트: 찾을 수 없으면 건너뛰고, HANJI_CI_REQUIRE_MODELS=1이면 실패한다."""
    missing = [name for name in names if models.find(name) is None]
    if missing:
        if os.environ.get("HANJI_CI_REQUIRE_MODELS") == "1":
            pytest.fail(f"model files not found: {missing}; run `hanji models fetch`")
        pytest.skip(f"model files not found: {missing} (hanji models fetch)")


@pytest.mark.parametrize(("raw", "threads"), [(None, None), ("", None), ("2", 2), (" 3 ", 3)])
def test_ort_threads_env_reads_a_positive_integer(monkeypatch, raw, threads):
    monkeypatch.delenv("HANJI_ORT_THREADS", raising=False)
    if raw is not None:
        monkeypatch.setenv("HANJI_ORT_THREADS", raw)
    assert resources.ort_threads_env() == threads


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5"])
def test_ort_threads_env_rejects_other_values_as_a_setting_error(monkeypatch, raw):
    monkeypatch.setenv("HANJI_ORT_THREADS", raw)
    with pytest.raises(HanjiError, match=r"HANJI_ORT_THREADS must be a positive integer"):
        resources.ort_threads_env()


def test_set_ort_threads_changes_session_options_only_when_asked(monkeypatch):
    monkeypatch.delenv("HANJI_ORT_THREADS", raising=False)
    options = SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
    resources.set_ort_threads(options)
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (0, 0)
    monkeypatch.setenv("HANJI_ORT_THREADS", "2")
    resources.set_ort_threads(options)
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (2, 1)


def test_ocr_and_layout_sessions_follow_hanji_ort_threads(monkeypatch):
    """HANJI_ORT_THREADS=2면 OCR·레이아웃 세션이 intra 2·inter 1, 없으면 onnxruntime 기본값(0)."""
    pytest.importorskip("onnxruntime")
    require_models(*ocr.MODEL_NAMES, *layout.MODEL_NAMES)
    from hanji.formats.pdf.layout.detector import LayoutDetector
    from hanji.formats.pdf.ocr import reader

    def threads():
        sessions = (reader._session(models.resolve("ocr-det")),
                    LayoutDetector(models.resolve("layout"), models.resolve("layout-config")).session)
        return [(s.get_session_options().intra_op_num_threads, s.get_session_options().inter_op_num_threads)
                for s in sessions]

    monkeypatch.delenv("HANJI_ORT_THREADS", raising=False)
    assert threads() == [(0, 0), (0, 0)]
    monkeypatch.setenv("HANJI_ORT_THREADS", "2")
    assert threads() == [(2, 1), (2, 1)]


def test_bad_hanji_ort_threads_stops_sessions_as_a_setting_error(monkeypatch):
    """잘못된 값은 설치 문제(OcrUnavailable·LayoutUnavailable)가 아니라 설정 오류(HanjiError, 종료 코드 1)이고 세션을
    만들기 전에 알린다(모델 파일이 없어도 같다)."""
    monkeypatch.setenv("HANJI_ORT_THREADS", "many")
    monkeypatch.setattr(ocr, "_reader", None)
    monkeypatch.setattr(layout, "_detector", None)
    for build in (ocr.get_reader, layout.get_detector):
        with pytest.raises(HanjiError, match=r"^HANJI_ORT_THREADS must be a positive integer, got 'many'$") as info:
            build()
        assert type(info.value) is HanjiError
    assert ocr._reader is None and layout._detector is None
