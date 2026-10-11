"""스캔 쪽 OCR 실행부(onnxruntime + numpy + Pillow + pyclipper). 모델 파일(검출·인식 모델과 인식 설정)은 hanji.models가
찾는다(HANJI_MODEL_DIR → `hanji models fetch` 캐시, SHA-256 확인). 인식 글자 목록은 설정 파일에서 읽는다(charset.py).

OCR 추가 설치(hanji[ocr])가 없어도 이 모듈은 import된다: numpy·onnxruntime은 get_reader()가 처음 불릴 때
가져온다(available()은 모듈과 모델 파일을 찾기만 한다). 세션(검출·인식)은 프로세스에 하나이고 만들 때만 잠금으로
보호한다. PDFium은 부르지 않는다."""

import atexit
import importlib.util
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .... import models
from ....errors import ModelError, OcrUnavailable
from ....resources import ort_threads_env
from . import charset

if TYPE_CHECKING:
    from PIL import Image

    from .reader import OcrReader

__all__ = ["MODEL_NAMES", "OcrLine", "OcrUnavailable", "available", "get_reader", "read_lines"]

INSTALL_HINT = 'pip install "hanji[ocr]"'
MODULES = ("numpy", "onnxruntime", "pyclipper")  # OCR 추가 설치가 까는 모듈
MODEL_NAMES = ("ocr-det", "ocr-rec", "ocr-rec-config")  # hanji.models 이름(검출·인식·인식 설정)

# onnxruntime(1.30)은 POSIX에서도 원격 측정(1DS)을 켠다: import할 때 세션과 무관한 스레드를 띄우고 libcurl로
# mobile.events.data.microsoft.com에 올린다. 그 스레드는 _release로 놓을 수 없어 프로세스 끝(C++ 정적 소멸자)까지 살아
# 있다(macOS 종료 abort 134의 원인 후보). onnxruntime을 import하기 전에만 끌 수 있다. 사용자가 정한 값은 둔다
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")


@dataclass(frozen=True, slots=True)
class OcrLine:
    """그림 속 줄 하나. box는 네 꼭짓점(그림 화소, 왼쪽 위부터 시계 방향), text는 NFC, score는 글자 확률 평균(0~1)."""

    box: tuple[tuple[float, float], ...]
    text: str
    score: float


_lock = threading.Lock()
_reader: "OcrReader | None" = None


def _installed(module: str) -> bool:
    """모듈을 찾을 수 있는가(import하지 않는다). 찾다가 난 오류(깨진 상위 패키지, __spec__ 없는 모듈)는 없는 것."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _missing() -> str | None:
    """OCR에 필요한데 찾을 수 없는 것을 알리는 문구(갖추는 방법 포함). 다 찾히면 None. 찾기만 하고 import하지 않으며
    모델 해시도 보지 않는다: 깔렸는데 import가 깨지거나 찾은 모델 파일이 고정한 것과 다르면 '있음'이고 _build가
    크게 알린다(자동 모드가 조용히 물러나지 않게)."""
    for module in MODULES:
        if not _installed(module):
            return f"OCR is not installed (missing {module}); {INSTALL_HINT}"
    for name in MODEL_NAMES:
        if models.find(name) is None:
            return f"OCR model file {models.MANIFEST[name].path} not found; {models.fetch_hint(name)}"
    return None


def available() -> bool:
    """OCR 추가 설치 모듈과 모델 파일을 모두 찾을 수 있는가. 아무것도 import하지 않고 세션도 만들지 않는다.
    모델 파일이 없으면(받기 전) 거짓: 자동 모드는 설치가 없을 때와 같다. 깔렸는데 깨진 설치(import 오류, 찾은 모델
    파일의 크기·SHA-256이 다름)는 참이고 get_reader()가 OcrUnavailable."""
    return _missing() is None


def _build() -> "OcrReader":
    ort_threads_env()  # 잘못된 HANJI_ORT_THREADS는 설치 문제가 아니라 설정 오류다(HanjiError, 세션을 만들기 전에)
    missing = _missing()
    if missing is not None:
        raise OcrUnavailable(f"{missing}, or run with --no-ocr")
    for module in MODULES:  # 깔렸는데 import가 깨지는 의존성(공유 라이브러리·glibc 등): 모듈과 오류를 알린다
        try:
            __import__(module)
        except MemoryError:  # 메모리 부족은 설치 문제가 아니다
            raise
        except Exception as exc:
            raise OcrUnavailable(
                f"OCR dependency {module} is installed but could not be imported "
                f"({type(exc).__name__}: {exc}); reinstall with {INSTALL_HINT} or run with --no-ocr"
            ) from exc
    try:  # 찾은 모델 파일의 크기·SHA-256과 글자 목록(깨진 받기·다른 판의 파일): 읽개 모듈(onnxruntime)까지 가지 않고 알린다
        det, rec, config = (models.resolve(name) for name in MODEL_NAMES)
        symbols = charset.load_symbols(config)
    except ModelError as exc:
        raise OcrUnavailable(f"{exc}, or run with --no-ocr") from exc
    try:  # import는 되는데 쓸 것이 없는 의존성(덜 지운 onnxruntime = 빈 이름공간 패키지 등)도 크게 알린다
        from .reader import OcrReader
    except MemoryError:  # 메모리 부족은 설치 문제가 아니다
        raise
    except Exception as exc:
        raise OcrUnavailable(
            f"OCR runtime could not be loaded ({type(exc).__name__}: {exc}); "
            f"reinstall with {INSTALL_HINT} or run with --no-ocr"
        ) from exc
    try:
        return OcrReader(det, rec, symbols)
    except MemoryError:  # 메모리 부족은 설치 문제가 아니다
        raise
    except Exception as exc:  # 글자 목록·모델 불일치, onnxruntime 오류: 설정 문제로 알린다
        raise OcrUnavailable(
            f"OCR models could not be loaded: {exc}; reinstall with {INSTALL_HINT} or run with --no-ocr"
        ) from exc


def get_reader() -> "OcrReader":
    """프로세스에 하나뿐인 읽개. 처음 부를 때 세션을 만든다(동시에 불러도 한 번만). 설치·모델 파일이 없거나 깨졌으면
    OcrUnavailable. 부르는 쪽은 읽개를 인터프리터 종료 너머까지 붙잡아 두지 않는다(atexit의 _release가 놓을 수 있게)."""
    global _reader
    if _reader is None:
        with _lock:
            if _reader is None:
                _reader = _build()
    return _reader


def _release() -> None:
    """프로세스가 끝날 때 세션을 먼저 놓는다. onnxruntime 작업 스레드가 C++ 정적 소멸자와 겹치면 macOS에서 드물게
    'recursive_mutex lock failed'로 abort한다(실측: 10쪽 읽고 끝나는 프로세스 약 60번에 1번).
    인터프리터가 끝날 때 아직 추론 중인 데몬 스레드는 지원하지 않는다(그 스레드가 쥔 세션은 놓지 못한다)."""
    global _reader
    with _lock:  # 다른 스레드가 만드는 중이면 끝난 뒤에 놓는다
        _reader = None


atexit.register(_release)


def read_lines(image: "Image.Image") -> list[OcrLine]:
    """그림 한 장의 줄들(위→아래, 같은 줄은 왼쪽→오른쪽). 점수로는 거르지 않는다."""
    return get_reader()(image)
