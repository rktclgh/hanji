"""PDF → ParsedSource. 쪽마다 판정(text_layer·text_stats)을 붙인다. digital·scanned 쪽은 보이는 글자(렌더 모드 3
제외)로 선 있는 표(table 블록)와 나머지 블록을 만들고, scanned 쪽은 OCR을 켰으면 그림 속 글자를 OCR 문단 블록
(text_source="ocr")으로 더한다. layer 모드의 바로 선 쪽은 선 없는 표(borderless.settle: 글자 정렬, 신뢰도
group.BORDERLESS_CONFIDENCE, 처리 이력 borderless_table)도 table 블록으로 낸다. 그림은 digital 쪽 이미지 객체(사진)와
레이아웃 모델(선·도형 그림·스캔 쪽 그림·캡션)로 찾아 figure·caption 블록과 잘라 낸 PNG(ParsedSource.assets)로 낸다.
unreliable 쪽(글자층이 깨진 쪽)은 OCR을 쓸 수 있으면 scanned처럼 쪽을 그려 OCR 문단으로 읽고(깨진 글자층 글자는 블록에
넣지 않고 글자 장부의 replaced로 센다. 표는 만들지 않는다), OCR을 쓸 수 없거나 받아들인 OCR 글자가 없으면(보이는
글자층이 있을 때) digital과 같은 경로로 깨진 글자층에서 블록을 만든다(신뢰도 상한 group.UNRELIABLE_CONFIDENCE).
어느 쪽이든 처리 이력에 한 줄 남긴다.
쪽마다 글자 장부(PageInfo.coverage)를 센다. 어느 블록에도 들지 않은 보이는 글자는 구조 문단으로 살려(rescued) 처리
이력에 한 줄 남기고, 장부가 맞지 않으면(이중 배정 등) 그 쪽 coverage는 None이고 처리 이력에 한 줄 남긴다.
쪽 렌더는 필요한 쪽만 한 번(PDFIUM_LOCK 안), OCR·모델·PNG 인코딩은 잠금 밖에서 한다."""

from dataclasses import dataclass, field
from typing import Any

from hanji_contracts import (
    Attempt, BBox, GateCheck, GateResult, PageInfo, PageLocator, RegionRecord, TextCoverage, TextLayerStats,
)
from PIL import Image

from ..base import ParsedSource
from . import borderless, figures, scan
from . import layout as layout_runtime
from . import ocr as ocr_runtime
from .extract import PageText, extract_pages
from .figures import Region
from .group import FigureBlock, Ledger, build_page_specs, unit_box
from .scan import OcrParagraph
from .tables import TableSpec, find_tables
from .triage import PageMode, classify, hidden_chars, page_mode, page_stats

MIME = "application/pdf"
RENDER_DPI = 144
TABLE_IN_FIGURE = "ruled table inside a figure was dropped (figure wins)"
LAYOUT_TABLE = "layout table box (kept for borderless-table detection, not a block)"
NO_IMAGE_MODEL = ("layout-model figure image not stored "
                  "(empty crop or document figure bytes over MAX_DOCUMENT_ASSET_BYTES)")
NO_IMAGE_PHOTO = ("image-object figure image not stored "
                  "(empty crop or document figure bytes over MAX_DOCUMENT_ASSET_BYTES)")
UNRELIABLE_KEPT = "unreliable_text_layer_kept"  # unreliable 쪽을 OCR 없이 깨진 글자층으로 블록을 만들었다
UNRELIABLE_OCR = "unreliable_text_layer_ocr"  # unreliable 쪽을 깨진 글자층 대신 OCR로 읽었다(글자층 글자는 replaced)
# OCR로 읽으려던 unreliable 쪽에서 받아들인 OCR 글자가 없어 깨진 글자층으로 돌아왔다(UNRELIABLE_KEPT 기록에 붙인다)
NO_OCR_TEXT = GateResult(passed=False, checks=(GateCheck(name="ocr_text", passed=False, value=0, threshold=">0"),))
COVERAGE_MISMATCH = "coverage_mismatch"  # 글자 장부가 맞지 않는다(보이는 글자가 블록에 정확히 한 번씩 들지 않았다: 버그)
UNASSIGNED_TEXT = "unassigned_text"  # 어느 블록에도 들지 않은 글자를 구조 문단으로 살렸다(버그 신호, 원문은 블록에 있다)
FULL_PAGE = (0.0, 0.0, 1.0, 1.0)  # 쪽 단위 처리 이력의 상자(보이는 쪽 0~1)


@dataclass(slots=True)
class _PageResult:
    """쪽 하나의 남긴 표·OCR 문단·그림 블록·처리 이력."""

    tables: list[TableSpec]
    paras: list[OcrParagraph] = field(default_factory=list)
    figures: list[FigureBlock] = field(default_factory=list)
    regions: list[RegionRecord] = field(default_factory=list)
    layout_tables: tuple[Region, ...] = ()  # 모델 table 상자(선 없는 표 settle 입력)


class PdfParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".pdf",)

    def __init__(self, ocr: bool | None = None, layout: bool | None = None) -> None:
        """ocr·layout: None이면 추가 설치가 있을 때 쓰고(깔렸는데 깨졌으면 쓸 쪽을 만날 때 OcrUnavailable·
        LayoutUnavailable), False면 쓰지 않는다. True인데 추가 설치가 없거나 깨졌으면 여기서 그 오류(문서 파싱 실패가
        아니라 설정 오류). layout이 False이거나 설치가 없어도 digital 쪽 사진(이미지 객체)은 그림 블록이 된다."""
        if ocr:
            ocr_runtime.get_reader()
        if layout:
            layout_runtime.get_detector()
        self.ocr = ocr
        self.layout = layout

    def parse(self, data: bytes, name: str) -> ParsedSource:
        """암호화·손상 PDF는 ParseError."""
        try:
            return self._parse(data, name)
        finally:
            borderless.forget()  # 선 없는 표의 한 쪽 캐시가 파싱 뒤 쪽 글자를 붙들지 않게(오류로 끝나도)

    def _parse(self, data: bytes, name: str) -> ParsedSource:
        pages = extract_pages(data, name)
        stats = [page_stats(page) for page in pages]
        states = [classify(s) for s in stats]
        use_ocr = (("scanned" in states or "unreliable" in states)
                   and bool(self.ocr or (self.ocr is None and ocr_runtime.available())))
        if use_ocr:
            ocr_runtime.get_reader()  # 깨진 설치는 쪽을 그리기 전에 알린다
        use_layout: bool | None = None  # 모델을 돌릴 첫 쪽에서 정한다(그런 쪽이 없으면 설치를 확인하지 않는다)
        budget = figures.AssetBudget()
        modes = [page_mode(s, use_ocr) for s in states]  # 쪽 상태와 따로: 이 쪽을 어떻게 읽나(블록 명세에도 넘긴다)
        # unreliable 쪽은 다른 쪽을 다 돈 뒤에 돈다: digital·scanned 쪽 그림이 문서 자산 예산을 지금과 똑같이 먼저 쓰고,
        # unreliable 쪽 그림은 남은 예산만 쓴다(앞 unreliable 쪽 때문에 뒤 digital 쪽 그림 이미지·처리 이력이 바뀌지 않게)
        results: dict[int, _PageResult] = {}
        for index in sorted(range(len(pages)), key=lambda k: states[k] == "unreliable"):
            page, state, mode = pages[index], states[index], modes[index]
            image, lines, gate = None, None, None
            if mode == "ocr":  # 깨진 글자층 대신 OCR로 읽을 쪽: 먼저 그려 겹침 거르기 없이 읽는다
                image = scan.render(data, name, index)
                lines = scan.page_lines(image, page, layer=False)
                if stats[index].chars and not any(t.text.strip() for t in lines):
                    # 받아들인 OCR 글자가 없는데 보이는 글자층이 있으면 TC-A 유지 경로로 돌아간다(원문을 결과물에
                    # 남긴다). 표·그림·블록 명세·장부가 모두 이 최종 모드를 보고, 그린 그림은 다시 쓴다
                    mode = modes[index] = "layer"
                    lines, gate = None, NO_OCR_TEXT
            # 선 있는 표를 처리 모드를 정한 뒤 한 번 찾고, 그 결과로 모델 게이트를 정한다(표 밖 긴 가로선). OCR로 대신
            # 읽는 쪽(ocr)은 깨진 글자층으로 표를 만들지 않는다
            found = find_tables(page) if mode != "ocr" else []
            want = figures.wants_layout(page, mode, found)  # 쪽 루프 안에서: 이미지 객체 상자를 쪽마다 한 번만 구한다
            if want and use_layout is None:
                use_layout = bool(self.layout or (self.layout is None and layout_runtime.available()))
                if use_layout:
                    layout_runtime.get_detector()  # 깨진 설치는 쪽을 그리기 전에 알린다(렌더하는 쪽은 모두 want)
            result = _page(data, name, index, page, mode, found, use_ocr and mode != "layer",
                           bool(use_layout) and want, budget, image, lines)
            # 선 없는 표: 렌더 여부와 상관없이 쪽마다 한 번(그림 정리 뒤 남은 글자로. scan·ocr 쪽은 시도하지 않는다)
            result.tables, notes = borderless.settle(page, mode, result.tables, result.layout_tables,
                                                     [f.figure for f in result.figures])
            result.regions += notes
            if state == "unreliable":
                result.regions.insert(0, _region(page, "unreliable-text-layer", FULL_PAGE, "paragraph",
                                                 UNRELIABLE_OCR if mode == "ocr" else UNRELIABLE_KEPT, False, gate))
            results[index] = result
        done = [results[k] for k in range(len(pages))]
        blocks, ledgers = build_page_specs(pages, states, [d.tables for d in done], [d.paras for d in done],
                                           [d.figures for d in done], modes=modes)
        infos = []
        for page, s, state, mode, result in zip(pages, stats, states, modes, done, strict=True):
            if ledgers[page.page].rescued:
                result.regions.append(_region(page, "unassigned-text", FULL_PAGE, "paragraph", UNASSIGNED_TEXT, False,
                                              _rescued_gate(ledgers[page.page])))
            coverage = _coverage(page, s, ledgers[page.page], mode)
            if coverage is None:
                result.regions.append(_region(page, "coverage", FULL_PAGE, "paragraph", COVERAGE_MISMATCH, False,
                                              _ledger_gate(s, ledgers[page.page], mode)))
            infos.append(PageInfo(page=page.page, width_pt=page.width_pt, height_pt=page.height_pt,
                                  rotation=page.rotation, render_dpi=RENDER_DPI, text_layer=state, text_stats=s,
                                  coverage=coverage))
        return ParsedSource(mime=MIME, pages=tuple(infos), blocks=tuple(blocks), assets=budget.assets,
                            regions=tuple(r for d in done for r in d.regions))


def _placed(stats: TextLayerStats, mode: PageMode) -> int:
    """블록에 들어야 할 글자 수: 보이는 공백 아닌 글자(stats.chars) 모두. ocr 쪽(깨진 글자층 대신 OCR로 읽은 쪽)은 0이다
    (그 글자는 모두 replaced)."""
    return 0 if mode == "ocr" else stats.chars


def _coverage(page: PageText, stats: TextLayerStats, ledger: Ledger, mode: PageMode) -> TextCoverage | None:
    """쪽 글자 장부. 블록에 들 글자(_placed)가 블록에 정확히 한 번씩 들었을 때만, 아니면 None(버그 신호: 파싱을
    실패시키지 않고 처리 이력에 coverage_mismatch를 남긴다). ocr 쪽의 보이는 글자는 replaced다. layer·scan 쪽은 블록에
    들지 않은 글자를 구조 문단이 살리므로(rescued, in_blocks에 든다) None은 이중 배정·쪽에 없는 글자 순번(doubled)이거나
    구조 문단으로도 줄을 만들지 못한 글자(살리기까지 실패: in_blocks가 모자란다)다. ocr 쪽은 텍스트 레이어 글자가
    블록에 샌 것(in_blocks > 0)도 None이다."""
    placed = _placed(stats, mode)
    if ledger.in_blocks != placed or ledger.doubled:
        return None
    hidden = hidden_chars(page)
    return TextCoverage(layer_chars=stats.chars + hidden, in_blocks=ledger.in_blocks, hidden=hidden,
                        replaced=stats.chars - placed, rescued=ledger.rescued)


def _rescued_gate(ledger: Ledger) -> GateResult:
    """unassigned_text 처리 이력에 붙일 숫자: 구조 문단으로 살린 글자 수(기준 0)."""
    return GateResult(passed=False, checks=(GateCheck(name="rescued", passed=False, value=ledger.rescued,
                                                      threshold="==0"),))


def _ledger_gate(stats: TextLayerStats, ledger: Ledger, mode: PageMode) -> GateResult:
    """coverage_mismatch 처리 이력에 붙일 장부 숫자: in_blocks(기준은 블록에 들 글자 수 _placed)와 doubled(기준 0)."""
    placed = _placed(stats, mode)
    checks = (GateCheck(name="in_blocks", passed=ledger.in_blocks == placed, value=ledger.in_blocks,
                        threshold=f"=={placed}"),
              GateCheck(name="doubled", passed=ledger.doubled == 0, value=ledger.doubled, threshold="==0"))
    return GateResult(passed=all(c.passed for c in checks), checks=checks)


def _page(data: bytes, name: str, index: int, page: PageText, mode: PageMode, tables: list[TableSpec],
          ocr_here: bool, layout_here: bool, budget: figures.AssetBudget, image: Image.Image | None = None,
          lines: list[scan.OcrText] | None = None) -> _PageResult:
    """쪽 하나: (필요할 때만) 렌더(잠금 안) → OCR 줄·레이아웃 상자(잠금 밖) → 그림 정리 → PNG → 남은 OCR 줄은 문단.
    렌더가 필요 없는 쪽(글자만 있는 layer 모드 쪽 등)은 지금과 같은 경로(표만). image·lines는 쪽 루프가 이미 그리고
    읽은 것(ocr 쪽, 또는 OCR 글자가 없어 유지 경로로 돌아온 쪽의 그림): 다시 그리거나 읽지 않는다."""
    if not (ocr_here or layout_here or (mode == "layer" and figures.photo_boxes(page))):
        return _PageResult(tables=list(tables))
    if image is None:
        image = scan.render(data, name, index)
    if lines is None:
        lines = scan.page_lines(image, page) if ocr_here else []
    regions = figures.page_regions(layout_runtime.detect(image), image.size, page) if layout_here else []
    plan = figures.arrange(page, mode, regions, tables, lines)
    out = _PageResult(tables=list(plan.tables), layout_tables=plan.layout_tables)
    source = "ocr" if ocr_here else "text_layer"
    for k, figure in enumerate(plan.figures, 1):
        fields = _store(image, figure, page, budget)
        out.figures.append(FigureBlock(figure, source, fields))
        if fields is None:
            reason = NO_IMAGE_MODEL if figure.model else NO_IMAGE_PHOTO  # 모델 이름은 모델이 찾은 그림에만
            out.regions.append(_region(page, f"figure-{k}", _unit(figure.box, page), "figure", reason, figure.model))
    rest = [line for i, line in enumerate(lines) if i not in plan.used_lines]
    out.paras = scan.paragraphs(scan.reading_order(rest), page)
    out.regions += [_region(page, f"table-in-figure-{k}", t.bbox, "table", TABLE_IN_FIGURE, layout_here)
                    for k, t in enumerate(plan.dropped, 1)]  # 표를 버리는 것은 모델이 찾은 그림뿐(리뷰 I1)
    if not borderless.applies(page, mode):  # 선 없는 표를 시도하지 않는 쪽(scan·ocr 모드·회전 쪽)의 모델 표 상자만
        out.regions += [_region(page, f"layout-table-{k}", _unit(r.box, page), "table", LAYOUT_TABLE, True)
                        for k, r in enumerate(plan.layout_tables, 1)]
    return out


def _store(image: Image.Image, figure: figures.Figure, page: PageText,
           budget: figures.AssetBudget) -> dict[str, Any] | None:
    """그림 PNG를 잘라 문서 자산에 담고 FigureImage 필드를 돌려준다. 자를 것이 없거나 바이트 상한을 넘으면 None."""
    crop = figures.crop_png(image, figure.box, page)
    if crop is None:
        return None
    png, width, height, dpi = crop
    asset = budget.add(png)
    if asset is None:
        return None
    return {"asset": asset, "mime": "image/png", "width_px": width, "height_px": height, "dpi": dpi,
            "category": figure.category}


def _unit(box: figures.Box, page: PageText) -> tuple[float, float, float, float]:
    """보이는 쪽 pt → 0~1."""
    return (box[0] / page.width_pt, box[1] / page.height_pt, box[2] / page.width_pt, box[3] / page.height_pt)


def _region(page: PageText, tag: str, box: tuple[float, float, float, float], kind: str, reason: str,
            model: bool, gate: GateResult | None = None) -> RegionRecord:
    """처리 이력 한 줄(블록이 되지 않았거나 이미지 없이 된 영역). box는 보이는 쪽 0~1. gate는 근거 숫자(있을 때만)."""
    return RegionRecord(region_id=f"p{page.page}-{tag}", kind=kind, chosen="det", fallback_reason=reason, gate=gate,
                        locator=PageLocator(page=page.page, bbox=BBox(**unit_box(*box))),
                        attempts=(Attempt(layer="det", model_id=layout_runtime.MODEL_ID if model else None),))
