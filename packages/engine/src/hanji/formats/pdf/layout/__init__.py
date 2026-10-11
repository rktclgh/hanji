"""레이아웃 모델 실행부(PP-DocLayout_plus-L 공식 ONNX, onnxruntime + numpy + Pillow). 모델 파일(inference.onnx·
inference.yml)은 hanji.models가 찾는다(HANJI_MODEL_DIR → `hanji models fetch` 캐시, SHA-256 확인).

레이아웃 추가 설치(hanji[layout])가 없어도 이 모듈은 import된다: numpy·onnxruntime은 get_detector()가
처음 불릴 때 가져온다(available()은 모듈과 모델 파일을 찾기만 한다). 세션은 프로세스에 하나이고 만들 때만 잠금으로
보호한다. PDFium은 부르지 않는다(부르는 쪽이 PDFIUM_LOCK 밖에서 쓴다)."""

import atexit
import importlib.util
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .... import models
from ....errors import LayoutUnavailable, ModelError
from ....resources import ort_threads_env

if TYPE_CHECKING:
    from PIL import Image

    from .detector import LayoutDetector

__all__ = ["MODEL_ID", "MODEL_NAMES", "LayoutBox", "LayoutUnavailable", "available", "detect", "get_detector"]

INSTALL_HINT = 'pip install "hanji[layout]"'
MODULES = ("numpy", "onnxruntime")  # 레이아웃 추가 설치가 까는 모듈
MODEL_NAMES = ("layout", "layout-config")  # hanji.models 이름(모델·설정)
MODEL_ID = "PP-DocLayout_plus-L"

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")  # onnxruntime import 전에 원격 측정을 끈다(OCR 실행부와 같은 이유)


@dataclass(frozen=True, slots=True)
class LayoutBox:
    """레이아웃 상자 하나. cls는 모델 분류 이름(image·chart·figure_title·table 등), score는 0~1,
    box는 입력 그림 화소 (x0, y0, x1, y1)(그림 안으로 자른 값)."""

    cls: str
    score: float
    box: tuple[float, float, float, float]


_lock = threading.Lock()
_detector: "LayoutDetector | None" = None


def _installed(module: str) -> bool:
    """모듈을 찾을 수 있는가(import하지 않는다). 찾다가 난 오류(깨진 상위 패키지, __spec__ 없는 모듈)는 없는 것."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _missing() -> str | None:
    """레이아웃에 필요한데 찾을 수 없는 것을 알리는 문구(갖추는 방법 포함). 다 찾히면 None. import하지 않고 모델 해시도
    보지 않는다(깔렸는데 깨진 설치는 _build가 알린다)."""
    for module in MODULES:
        if not _installed(module):
            return f"layout is not installed (missing {module}); {INSTALL_HINT}"
    for name in MODEL_NAMES:
        if models.find(name) is None:
            return f"layout model file {models.MANIFEST[name].path} not found; {models.fetch_hint(name)}"
    return None


def available() -> bool:
    """레이아웃 추가 설치 모듈과 모델 파일을 모두 찾을 수 있는가. 아무것도 import하지 않고 세션도 만들지 않는다.
    모델 파일이 없으면(받기 전) 거짓: 자동 모드는 설치가 없을 때와 같다. 깔렸는데 깨진 설치(import 오류, 찾은 모델
    파일의 크기·SHA-256이 다름)는 참이고 get_detector()가 LayoutUnavailable."""
    return _missing() is None


def _build() -> "LayoutDetector":
    ort_threads_env()  # 잘못된 HANJI_ORT_THREADS는 설치 문제가 아니라 설정 오류다(HanjiError, 세션을 만들기 전에)
    missing = _missing()
    if missing is not None:
        raise LayoutUnavailable(f"{missing}, or run with --no-layout")
    for module in MODULES:  # 깔렸는데 import가 깨지는 의존성(공유 라이브러리 등): 모듈과 오류를 알린다
        try:
            __import__(module)
        except MemoryError:  # 메모리 부족은 설치 문제가 아니다
            raise
        except Exception as exc:
            raise LayoutUnavailable(
                f"layout dependency {module} is installed but could not be imported "
                f"({type(exc).__name__}: {exc}); reinstall with {INSTALL_HINT} or run with --no-layout"
            ) from exc
    try:  # 찾은 모델 파일의 크기·SHA-256(깨진 받기·다른 판의 파일): 세션 모듈까지 가지 않고 알린다
        model, config = (models.resolve(name) for name in MODEL_NAMES)
    except ModelError as exc:
        raise LayoutUnavailable(f"{exc}, or run with --no-layout") from exc
    try:  # import는 되는데 쓸 것이 없는 의존성도 크게 알린다
        from .detector import LayoutDetector
    except MemoryError:
        raise
    except Exception as exc:
        raise LayoutUnavailable(
            f"layout runtime could not be loaded ({type(exc).__name__}: {exc}); "
            f"reinstall with {INSTALL_HINT} or run with --no-layout"
        ) from exc
    try:
        return LayoutDetector(model, config)
    except MemoryError:
        raise
    except ValueError as exc:  # 확인을 지난 파일의 분류 목록·입력 이름이 다르다: 이 빌드끼리 맞지 않는다(OCR 글자 목록과 같다)
        raise LayoutUnavailable(
            f"layout model could not be loaded: {exc}; reinstall hanji or report it, or run with --no-layout"
        ) from exc
    except Exception as exc:  # onnxruntime 오류: 설정 문제로 알린다(오류에 파일 경로가 있다)
        raise LayoutUnavailable(
            f"layout model could not be loaded: {exc}; reinstall with {INSTALL_HINT} or run with --no-layout"
        ) from exc


def get_detector() -> "LayoutDetector":
    """프로세스에 하나뿐인 검출기. 처음 부를 때 세션을 만든다(동시에 불러도 한 번만). 설치·모델 파일이 없거나
    깨졌으면 LayoutUnavailable."""
    global _detector
    if _detector is None:
        with _lock:
            if _detector is None:
                _detector = _build()
    return _detector


def _release() -> None:
    """프로세스가 끝날 때 세션을 먼저 놓는다(OCR 실행부와 같은 이유: macOS에서 onnxruntime 작업 스레드가 C++ 정적
    소멸자와 겹치면 드물게 abort한다)."""
    global _detector
    with _lock:
        _detector = None


atexit.register(_release)


def detect(image: "Image.Image") -> list[LayoutBox]:
    """쪽 그림 한 장의 레이아웃 상자(점수 높은 순, 점수 ≥ 0.2). 그림·캡션 기준값은 부르는 쪽(figures.py)이 정한다."""
    return get_detector()(image)
