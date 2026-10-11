"""PP-DocLayout_plus-L ONNX 세션 하나로 쪽 그림 한 장의 레이아웃 상자를 찾는다(입출력 처리는 boxes.py)."""

from pathlib import Path

import onnxruntime as ort
from PIL import Image

from ....resources import set_ort_threads
from . import LayoutBox
from .boxes import LABELS, postprocess, preprocess, read_labels

INPUTS = ("im_shape", "image", "scale_factor")


class LayoutDetector:
    """세션은 만들 때 한 번 연다. 호출은 여러 스레드에서 동시에 해도 된다(onnxruntime run은 스레드 안전)."""

    def __init__(self, model: Path, config: Path) -> None:
        """model: inference.onnx, config: inference.yml(분류 목록이 LABELS와 같아야 한다: 세션을 열기 전에 본다).
        CPU, 스레드 수는 HANJI_ORT_THREADS(없으면 onnxruntime 기본값). 메모리 아레나와 메모리 패턴을 끈다(OCR 세션과 같다: 결과는 같고 최대 RSS가
        준다). 분류 목록이나 입력 이름이 PP-DocLayout_plus-L과 다르면 ValueError."""
        labels = read_labels(config)
        if labels != LABELS:
            raise ValueError(f"{config} lists {len(labels)} labels that are not the PP-DocLayout_plus-L labels")
        options = ort.SessionOptions()
        options.log_severity_level = 4
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL  # 채점 게이트(Task 10)도 이 값
        set_ort_threads(options)
        self.session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        names = sorted(i.name for i in self.session.get_inputs())
        if names != sorted(INPUTS):
            raise ValueError(f"{model} is not a PP-DocLayout model (inputs {names})")

    def __call__(self, image: Image.Image) -> list[LayoutBox]:
        """쪽 그림 한 장(어떤 모드든 RGB로 읽는다)의 상자, 점수 높은 순. 빈 그림은 빈 목록."""
        width, height = image.size
        if not width or not height:
            return []
        rows = self.session.run(None, preprocess(image))[0]
        return postprocess(rows, width, height)
