"""DrawingML·VML 개체 도우미(스펙 §7.1, §8.4, DOCX·PPTX 함께). 그림 관계 → 미디어 파트 → PNG 자산. 변환·상한·화소·사유는
hwpx/images.py의 to_png를 그대로 쓰고(옮기지 않는다, 스펙 §3), 같은 미디어 파트는 한 번만 바꾼다. 자산 예산은 PDF·HWPX와
같은 AssetBudget이다. 차트는 풍부한 글자(a:t)만 블록으로 내고 캐시 문자열은 글자 수만 센다. SmartArt는 데이터 파트의
확장(dsp:dataModelExt@relId)을 호스트 파트 관계로 풀어 그려진 파트를 찾는다."""

import xml.etree.ElementTree as ET
from typing import Any

from ...errors import ParseError
from ..hwpx import images
from ..hwpx.parser import FIGURE_BUDGET, FIGURE_LINKED, FIGURE_MISSING
from ..pdf.figures import AssetBudget
from ..flowledger import ink
from ..zipxml import OverBudget, TooLarge
from .package import R, Package

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
FRAMES = frozenset({f"{WP}inline", f"{WP}anchor"})  # w:drawing 아래 그림 틀
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
DSP = "{http://schemas.microsoft.com/office/drawing/2008/diagram}"
R_EMBED, R_LINK, R_ID, R_DM = f"{{{R}}}embed", f"{{{R}}}link", f"{{{R}}}id", f"{{{R}}}dm"
A_T, A_P, A_BR = f"{A}t", f"{A}p", f"{A}br"
CHART, SMARTART = f"{C}chart", f"{DGM}relIds"  # a:graphicData 아래 차트·SmartArt 참조(관계는 호스트 파트의 것)
CHART_ROOT, DATA_ROOT, DRAWN_ROOT = f"{C}chartSpace", f"{DGM}dataModel", f"{DSP}drawing"
DIAGRAM_EXT = "http://schemas.microsoft.com/office/drawing/2008/diagram"  # 데이터 파트 확장의 a:ext@uri
CHART_TEXT = "chart_text"  # 차트의 풍부한 글자를 문단으로 냈다(캐시 문자열은 cache_chars로 수만)
SMARTART_TEXT = "smartart_text"  # SmartArt 도형 글자를 문단으로 냈다
EMU_PER_INCH = 914_400


def emu(value: str | None) -> int | None:
    """EMU 정수(양수만, 아니면 None)."""
    try:
        number = int(value) if value is not None else 0
    except ValueError:
        return None
    return number if 0 < number < 2**63 else None


def dpi(width_px: int, cx: int | None) -> int:
    """dpi = 그림 화소 너비 ÷ (표시 너비 EMU ÷ 914400). 표시 너비가 없으면 96."""
    return max(1, round(width_px * EMU_PER_INCH / cx)) if cx else 96


def hidden_frame(frame: ET.Element) -> bool:
    """wp:inline·wp:anchor의 wp:docPr@hidden이 참이면 그 개체를 내지 않는다(스펙 §7.1)."""
    pr = frame.find(f"{WP}docPr")
    return pr is not None and pr.get("hidden", "").lower() in ("1", "true")


def blip(pic: ET.Element) -> tuple[str | None, str | None]:
    """pic:pic의 (r:embed, r:link). SVG 확장(asvg:svgBlip)은 보지 않는다: r:embed가 PNG 대체 그림이다."""
    found = pic.find(f"{PIC}blipFill/{A}blip")
    return (found.get(R_EMBED), found.get(R_LINK)) if found is not None else (None, None)


class Images:
    """문서 하나의 그림: 미디어 멤버 → PNG 또는 이미지 없음 사유(한 번만 바꾼다), 자산 예산."""

    def __init__(self, package: Package) -> None:
        self.package = package
        self.budget = AssetBudget()
        self.pictures: dict[str, images.Png | str] = {}

    def figure(self, part: str, embed: str | None, link: str | None, cx: int | None) -> dict[str, Any] | str:
        """그림 블록의 figure 값 또는 이미지 없음 사유. embed·link는 part의 관계 id다. 외부 관계와 r:link만 있는
        그림은 figure_image_linked(따라가지 않는다), 관계·멤버가 없으면 figure_image_missing."""
        rels = self.package.rels(part)
        rel = rels.get(embed) if embed else None
        if rel is None:
            return FIGURE_LINKED if link and not embed else FIGURE_MISSING
        if rel.target is None:
            return FIGURE_LINKED
        if not self.package.has(rel.target):
            return FIGURE_MISSING
        result = self.pictures.get(rel.target)
        if result is None:
            try:
                result = images.to_png(self.package.binary(rel.target))
            except OverBudget:
                result = FIGURE_BUDGET
            except TooLarge:
                result = images.TOO_LARGE
            except ParseError:  # 그림 멤버의 zip 오류(CRC 등): 그 그림만 잃는다
                result = images.UNREADABLE
            self.pictures[rel.target] = result
        if isinstance(result, str):
            return result
        key = self.budget.add(result.data)
        if key is None:
            self.pictures[rel.target] = FIGURE_BUDGET  # 담지 못한 PNG 바이트는 들고 있지 않는다
            return FIGURE_BUDGET
        return {"asset": key, "mime": "image/png", "width_px": result.width, "height_px": result.height,
                "dpi": dpi(result.width, cx), "category": "image"}


def cache_chars(chart: ET.Element) -> int:
    """차트 파트의 캐시 문자열(c:strCache·c:numCache의 c:v) 공백 아닌 글자 수(범위 밖: 수만 남긴다)."""
    return sum(ink(v.text or "") for tag in (f"{C}strCache", f"{C}numCache") for cache in chart.iter(tag)
               for v in cache.iter(f"{C}v"))


def drawn_rel(data: ET.Element) -> str | None:
    """SmartArt 데이터 파트의 그려진 파트 관계 id(dgm:extLst/a:ext[@uri]/dsp:dataModelExt@relId, 호스트 파트의 관계)."""
    for ext in data.iterfind(f"{DGM}extLst/{A}ext"):
        found = ext.find(f"{DSP}dataModelExt")
        if ext.get("uri") == DIAGRAM_EXT and found is not None and found.get("relId"):
            return found.get("relId")
    return None


def smartart_shapes(root: ET.Element) -> list[ET.Element]:
    """SmartArt 글 몸통들(문서 순서): 그려진 파트면 dsp:sp/dsp:txBody, 데이터 파트면 dgm:pt/dgm:t. 안의 a:p가 문단이다."""
    if root.tag == DRAWN_ROOT:
        return [body for sp in root.iter(f"{DSP}sp") if (body := sp.find(f"{DSP}txBody")) is not None]
    return [body for pt in root.iter(f"{DGM}pt") if (body := pt.find(f"{DGM}t")) is not None]
