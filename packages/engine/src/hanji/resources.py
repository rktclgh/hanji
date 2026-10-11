"""실행 모델 자원(onnxruntime·numpy를 import하지 않는 순수 계산): ORT 스레드 손잡이, 메모리·CPU 한도 알아내기(cgroup v2·v1,
물리 메모리), 크기 문자열, 문서 비용 추정과 입장(ingest_many의 부모가 쓴다)."""

import os

from .errors import HanjiError

ORT_THREADS_ENV = "HANJI_ORT_THREADS"


def ort_threads_env() -> int | None:
    """환경변수 HANJI_ORT_THREADS(양의 정수). 없거나 비었으면 None(onnxruntime 기본값). 잘못된 값은 HanjiError(설정 오류,
    종료 코드 1). OCR·레이아웃 세션과 ingest_many가 같은 함수로 읽는다."""
    raw = os.environ.get(ORT_THREADS_ENV, "").strip()
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise HanjiError(f"{ORT_THREADS_ENV} must be a positive integer, got {raw!r}")
    return int(raw)


def set_ort_threads(options: object) -> None:
    """onnxruntime 세션 옵션에 스레드 수를 정한다: HANJI_ORT_THREADS가 있으면 intra_op n, inter_op 1. 없으면 그대로."""
    threads = ort_threads_env()
    if threads is not None:
        options.intra_op_num_threads = threads  # type: ignore[attr-defined]
        options.inter_op_num_threads = 1  # type: ignore[attr-defined]
