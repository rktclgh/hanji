"""그림 멤버 → PNG(스펙 §4.4, §7.1). 허용한 디코더만 쓰고, 화소 수를 먼저 보고, 알파는 흰 바탕에 합친 뒤 RGB로,
긴 변은 MAX_FIGURE_SIDE까지 줄여 PDF와 같은 방식(최적화 끔·메타데이터 없음)으로 저장한다."""

import io
import warnings
from dataclasses import dataclass

from hanji_contracts import MAX_FIGURE_SIDE
from PIL import Image, UnidentifiedImageError

from ..pdf.figures import PNG_LEVEL

FORMATS = ("PNG", "JPEG", "BMP", "GIF", "TIFF")  # 허용 디코더(WMF·EMF 등은 지원하지 않는 형식)
MAX_IMAGE_PIXELS = 100_000_000
UNSUPPORTED = "figure_image_unsupported"
TOO_LARGE = "figure_image_too_large"
UNREADABLE = "figure_image_unreadable"


@dataclass(frozen=True, slots=True)
class Png:
    data: bytes
    width: int
    height: int


def to_png(raw: bytes) -> Png | str:
    """PNG 또는 이미지 없음 사유. 전역 Image.MAX_IMAGE_PIXELS는 바꾸지 않는다(Pillow 폭탄 경고·오류도 이미지 없음)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=FORMATS) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return TOO_LARGE
                image.load()  # 여러 장(GIF·TIFF)은 첫 장
                return _encode(_rgb(image))
    except UnidentifiedImageError:
        return UNSUPPORTED
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return TOO_LARGE
    except (OSError, ValueError, SyntaxError, EOFError):
        return UNREADABLE


def _rgb(image: Image.Image) -> Image.Image:
    """알파(RGBA·LA·투명 P)는 흰 바탕에 합치고, 나머지(CMYK·L·P 등)는 RGB로."""
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        back = Image.new("RGB", rgba.size, (255, 255, 255))
        back.paste(rgba, mask=rgba.getchannel("A"))
        return back
    return image.convert("RGB")


def _encode(image: Image.Image) -> Png:
    if max(image.size) > MAX_FIGURE_SIDE:
        scale = MAX_FIGURE_SIDE / max(image.size)
        size = (max(1, min(MAX_FIGURE_SIDE, round(image.width * scale))),
                max(1, min(MAX_FIGURE_SIDE, round(image.height * scale))))
        image = image.resize(size, Image.Resampling.LANCZOS)
    image.info = {}  # 원본의 ICC·글자·dpi 등을 PNG에 옮기지 않는다(메타데이터 없음)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False, compress_level=PNG_LEVEL)
    return Png(buf.getvalue(), image.width, image.height)


def dpi(width_px: int, width_hwp: str | None) -> int:
    """dpi = 그림 화소 너비 ÷ (표시 너비 HWPUNIT ÷ 7200). 표시 너비가 없거나 0·음수(부호 없는 값으로 저장된 음수)면 96."""
    try:
        width = int(width_hwp) if width_hwp is not None else 0
    except ValueError:
        width = 0
    if not 0 < width < 2**31:
        return 96
    return max(1, round(width_px * 7200 / width))
