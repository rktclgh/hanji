"""ONNX 세션 둘(검출·인식)과 글자 목록으로 그림 한 장을 읽는다. 방향 판정은 하지 않는다(스펙 O2)."""

import math
import unicodedata
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

from ....resources import set_ort_threads
from . import OcrLine
from .det import detect
from .pixels import crop_quad, resize_linear
from .rec import recognize

MAX_SIDE = 2000  # 긴 변이 이보다 크면 줄여서 읽는다(RapidOCR max_side_len)
PRE_SHRINK_SIDE = 2 * MAX_SIDE  # 긴 변이 이보다 크거나(A4·A3 200 DPI는 긴 변 2339·3307px라 해당 없음)
PRE_SHRINK_PIXELS = 16_000_000  # 화소 수가 이보다 많은 그림은 numpy로 바꾸기 전에 Pillow로 정수배 줄인다(메모리)


def _session(path: Path) -> ort.InferenceSession:
    """CPU, 스레드 수는 HANJI_ORT_THREADS(없으면 onnxruntime 기본값). 메모리 아레나와 메모리 패턴(입력 크기별 미리 잡는 버퍼)을 끈다: A4 200 DPI
    한 쪽을 거듭 읽을 때 최대 RSS 약 1.42GiB → 0.97GiB, 채점 10쪽 연속 약 1.63GiB → 1.16GiB(macOS arm64 실측).
    결과는 같고 쪽당 시간 차이는 1% 안."""
    options = ort.SessionOptions()
    options.log_severity_level = 4
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    set_ort_threads(options)
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


def _shrunk_size(h: int, w: int) -> tuple[int, int]:
    s = MAX_SIDE / max(h, w)
    return max(32, int(round(int(h * s) / 32) * 32)), max(32, int(round(int(w * s) / 32) * 32))


class OcrReader:
    """세션은 만들 때 한 번 연다. 호출은 여러 스레드에서 동시에 해도 된다(onnxruntime run은 스레드 안전)."""

    def __init__(self, det: Path, rec: Path, symbols: list[str]) -> None:
        """det·rec: hanji.models가 찾아 크기·SHA-256을 확인한 검출·인식 모델, symbols: 인식 글자 목록
        (charset.load_symbols)."""
        self.det = _session(det)
        self.rec = _session(rec)
        symbols = list(symbols)
        self.symbols = ["blank", *symbols, " "]  # CTC 빈칸 + 글자 목록 + 공백(use_space_char)
        classes = self.rec.get_outputs()[0].shape[-1]
        if isinstance(classes, int) and classes != len(self.symbols):  # 글자 번호가 어긋난다
            raise ValueError(f"the character list gives {len(self.symbols)} classes but the recognition model has "
                             f"{classes}")
        if not isinstance(classes, int):  # 갈래 수가 정해지지 않은 모델: 모델 정보의 글자 목록이 있으면 그것과 맞춘다
            inner = self.rec.get_modelmeta().custom_metadata_map.get("character")
            if inner is not None and symbols != inner.removesuffix("\n").split("\n"):
                raise ValueError("the character list does not match the one in the recognition model")

    def __call__(self, image: Image.Image) -> list[OcrLine]:
        """줄마다 OcrLine(상자는 입력 그림 화소, 글자는 NFC·앞뒤 공백 없음). 글자가 빈 줄은 버린다. 점수로는 거르지 않는다.
        아주 큰 그림은 먼저 Pillow로 정수배 줄여(Image.reduce, 칸 평균) 원래 크기의 배열을 만들지 않는다. 보통 쪽은 그대로."""
        width, height = image.size
        if not width or not height:  # 빈 그림(미리 줄이기 전에 본다)
            return []
        f = max(math.ceil(max(width, height) / PRE_SHRINK_SIDE), math.ceil(math.sqrt(width * height / PRE_SHRINK_PIXELS)))
        if f <= 1:
            return self._read(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        lines = self._read(image.reduce(f))  # 줄인 화소 i는 원래 화소 [i·f, (i+1)·f)의 평균
        return [OcrLine(box=tuple((min(x * f, width), min(y * f, height)) for x, y in ln.box), text=ln.text, score=ln.score)
                for ln in lines]

    def _read(self, image: Image.Image) -> list[OcrLine]:
        img = np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1])  # RapidOCR처럼 BGR
        h, w = img.shape[:2]
        if not h or not w:
            return []
        sy = sx = 1.0
        if max(h, w) > MAX_SIDE:
            nh, nw = _shrunk_size(h, w)
            img, sy, sx = resize_linear(img, nw, nh), h / nh, w / nw
        boxes = detect(self.det, img)
        if not len(boxes):
            return []
        lines = []
        for box, (text, score) in zip(boxes, recognize(self.rec, self.symbols, [crop_quad(img, b) for b in boxes])):
            text = unicodedata.normalize("NFC", text).strip()
            if not text:
                continue
            pts = np.minimum(np.maximum(box.astype(np.float64) * [sx, sy], 0), [w, h])
            lines.append(OcrLine(box=tuple((float(x), float(y)) for x, y in pts), text=text, score=score))
        return lines
