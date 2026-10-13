"""HWPX(한컴 OWPML, zip + XML) → ParsedSource. PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다(모델·OCR 없음).

구역(spine 순서)마다 트리를 만들고 블록 명세로 바꾼 뒤 버린다. 최상위 문단(hs:sec 바로 아래 hp:p)마다 문단 글자
블록(개요 수준 → 제목, 번호·글머리 → 목록 항목, 그 밖 문단)을 낸다. 위치는 FlowLocator(제목 경로, 최상위 문단 순번).
글자 장부(ledger.py)가 모든 hp:t·글맵시 글자를 세고, 한 번도 넣지 않은 글자는 구역 끝 구조 문단(신뢰도 0.3)으로
살린다(처리 이력 unassigned_text). 장부가 맞지 않으면 처리 이력 coverage_mismatch(파싱은 실패하지 않는다)."""

import bisect
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hanji_contracts import Attempt, FlowLocator, GateCheck, GateResult, RegionRecord

from ..base import ParsedSource
from ..pdf.group import MAX_LEVEL, RESCUED_CONFIDENCE
from ..pdf.parser import COVERAGE_MISMATCH, UNASSIGNED_TEXT
from .container import HH, HP, HS, MIME, Package
from .header import Styles, read_styles
from .ledger import INLINE, T, Bundle, Counts, Ledger, ink, source_chars

P, RUN, SUBLIST = f"{HP}p", f"{HP}run", f"{HP}subList"
TEXTART, HIDDEN = f"{HP}textart", f"{HP}hiddenComment"
UNKNOWN_INLINE = "unknown_inline_element"  # hp:t 안 모르는 자식 요소의 글자를 그 자리에 넣었다
HIDDEN_COMMENT = "hidden_comment"  # 숨은 설명(인쇄되지 않음): 블록에 넣지 않았다
MASTERPAGE_TEXT = "masterpage_text_skipped"  # 마스터 쪽 글자(v1은 읽지 않는다)


@dataclass(slots=True)
class _Region:
    """처리 이력 한 줄(제목 경로는 문서를 다 읽은 뒤 닻 문단의 것으로 채운다)."""

    tag: str
    kind: str
    reason: str
    section: int
    anchor: int
    gate: GateResult | None = None
    region_id: str | None = None  # 정해진 id(구역 단위 기록)


@dataclass(slots=True)
class _Doc:
    """문서 전체에서 모으는 것: 블록 명세(내부 키 _anchor 등은 _finish가 지운다), 처리 이력, 장부 숫자."""

    name: str
    styles: Styles
    specs: list[dict[str, Any]] = field(default_factory=list)
    regions: list[_Region] = field(default_factory=list)
    counts: list[Counts] = field(default_factory=list)


def _t_text(t: ET.Element) -> tuple[str, bool]:
    """hp:t의 글자(스펙 §5.1)와 모르는 자식 요소에 글자가 있었는지."""
    parts = [t.text or ""]
    unknown = False
    for child in t:
        # 아는 인라인 요소는 대치 글자를 넣고, 안에 글자가 있으면(모르는 자식처럼) 그 자리에 잇고 기록한다
        inner = "".join(child.itertext())
        parts.append(INLINE.get(child.tag, "") + inner)
        unknown = unknown or bool(inner.strip())
        parts.append(child.tail or "")
    return "".join(parts), unknown


def _join(bundles: list[Bundle]) -> str:
    return "".join(text for _, text in bundles)


class _Section:
    """구역 하나를 블록 명세로 바꾼다. base는 이 구역 첫 최상위 문단의 문서 전체 순번."""

    def __init__(self, doc: _Doc, index: int, root: ET.Element, base: int) -> None:
        self.doc = doc
        self.index = index
        self.root = root
        self.base = base
        self.ledger = Ledger()
        self.late = False  # 구역 끝 블록을 내는 중(닻 문단 자리의 제목 경로를 쓴다)
        self.unknown: set[int] = set()  # 모르는 인라인 요소 기록을 남긴 문단(id)
        self._sources()

    def _sources(self) -> None:
        """장부 출처를 문서 순서로 센다(스택 순회, 재귀 없음). 숨은 설명마다 처리 이력을 남긴다."""
        anchor = self.base - 1
        for top in self.root:
            if top.tag == P:
                anchor += 1
            stack: list[tuple[ET.Element, bool, int]] = [(top, False, id(top))]
            while stack:
                element, hidden, para = stack.pop()
                if element.tag == P:
                    para = id(element)
                if element.tag == T:
                    self.ledger.add(element, source_chars(element), max(anchor, self.base), para, hidden)
                    continue
                if element.tag == TEXTART:
                    self.ledger.add(element, ink(element.get("text", "")), max(anchor, self.base), para, hidden)
                if element.tag == HIDDEN and not hidden:
                    chars = sum(source_chars(t) for t in element.iter(T))
                    self.region("hidden-comment", "paragraph", HIDDEN_COMMENT, max(anchor, self.base),
                                _gate("hidden_chars", chars, None))
                    hidden = True
                stack.extend((child, hidden, para) for child in reversed(element))

    def region(self, tag: str, kind: str, reason: str, anchor: int, gate: GateResult | None = None) -> None:
        self.doc.regions.append(_Region(tag, kind, reason, self.index, anchor, gate))

    def bundles(self, p: ET.Element, anchor: int) -> list[Bundle]:
        """문단 hp:run들의 hp:t 글자를 순서대로(개체 안 글자는 들지 않는다). 출처마다 묶음 하나."""
        out: list[Bundle] = []
        for run in p.iterfind(RUN):
            for t in run.iterfind(T):
                out.append(self.source_bundle(t, anchor, p))
        return out

    def source_bundle(self, t: ET.Element, anchor: int, p: ET.Element) -> Bundle:
        text, unknown = _t_text(t)
        if unknown and id(p) not in self.unknown:
            self.unknown.add(id(p))
            self.region("unknown-inline", "paragraph", UNKNOWN_INLINE, anchor)
        return self.ledger.source_of(t), text

    def emit(self, kind: str, bundles: list[Bundle], anchor: int, **extra: Any) -> bool:
        """블록 하나를 낸다(공백만이면 내지 않는다). 낸 묶음을 장부에 넣는다."""
        text = _join(bundles).strip()
        if not text:
            return False
        self.doc.specs.append({"kind": kind, "text": text, "confidence": extra.pop("confidence", 1.0), "state": "det",
                               "text_source": "native", "_anchor": anchor, "_late": self.late, **extra})
        self.ledger.put(bundles)
        return True

    def paragraph(self, p: ET.Element, anchor: int) -> None:
        """최상위 문단: 개요 수준 → 제목, 번호·글머리 → 목록 항목, 그 밖 문단."""
        bundles = self.bundles(p, anchor)
        kind, level = self.doc.styles.headings.get(p.get("paraPrIDRef", ""), ("NONE", 0))
        if kind == "OUTLINE":
            self.emit("heading", bundles, anchor, level=max(1, min(level + 1, MAX_LEVEL)))  # 음수 수준도 1~6 안으로
        elif kind in ("NUMBER", "BULLET"):
            self.emit("list_item", bundles, anchor)
        else:
            self.emit("paragraph", bundles, anchor)

    def build(self) -> None:
        anchor = self.base - 1
        for top in self.root:
            if top.tag != P:
                continue
            anchor += 1
            self.paragraph(top, anchor)
        self.late = True
        self.rescue()
        counts = self.ledger.count()
        self.doc.counts.append(counts)
        if not counts.ok:
            self.doc.regions.append(_Region("coverage", "paragraph", COVERAGE_MISMATCH, self.index, self.base,
                                            _ledger_gate(counts), f"s{self.index}-coverage"))

    def rescue(self) -> None:
        """한 번도 넣지 않은 출처를 닻 문단별로 모아 구조 문단으로(문서 순서). 같은 문단 출처는 잇고 문단은 \\n."""
        groups: dict[int, list[int]] = {}
        for k in self.ledger.unplaced():
            groups.setdefault(self.ledger.sources[k].anchor, []).append(k)
        rescued = 0
        for anchor, keys in groups.items():
            bundles: list[Bundle] = []
            for i, k in enumerate(keys):
                source = self.ledger.sources[k]
                if i and source.para != self.ledger.sources[keys[i - 1]].para:
                    bundles.append((None, "\n"))
                if source.element.tag == TEXTART:
                    bundles.append((k, source.element.get("text", "")))
                else:
                    bundles.append((k, _t_text(source.element)[0]))
            if self.emit("paragraph", bundles, anchor, confidence=RESCUED_CONFIDENCE):
                rescued += sum(ink(text) for _, text in bundles)
        if rescued:
            self.ledger.rescued = rescued
            first = next(iter(groups))
            self.region("unassigned-text", "paragraph", UNASSIGNED_TEXT, first, _gate("rescued", rescued, "==0"))


def _gate(name: str, value: int, threshold: str | None) -> GateResult:
    """근거 숫자 하나. 기준이 있으면 실패 검사(버그 신호), 없으면 기록용 통과 검사."""
    passed = threshold is None
    return GateResult(passed=passed, checks=(GateCheck(name=name, passed=passed, value=value, threshold=threshold),))


def _ledger_gate(counts: Counts) -> GateResult:
    """coverage_mismatch 근거: in_blocks(기준 source − hidden), doubled·partial(기준 0), hidden(기록)."""
    expected = counts.source - counts.hidden
    checks = (GateCheck(name="in_blocks", passed=counts.in_blocks == expected, value=counts.in_blocks,
                        threshold=f"=={expected}"),
              GateCheck(name="doubled", passed=counts.doubled == 0, value=counts.doubled, threshold="==0"),
              GateCheck(name="partial", passed=counts.partial == 0, value=counts.partial, threshold="==0"),
              GateCheck(name="hidden", passed=True, value=counts.hidden))
    return GateResult(passed=all(c.passed for c in checks), checks=checks)


def _finish(doc: _Doc) -> tuple[tuple[dict[str, Any], ...], tuple[RegionRecord, ...]]:
    """제목 경로를 채운다. 구역 끝 블록(_late)과 처리 이력은 닻 문단 자리의 제목 경로를 쓴다."""
    stack: list[tuple[int, str]] = []
    marks: list[tuple[int, tuple[str, ...]]] = []  # (닻, 그 닻까지 처리한 뒤 제목 경로)
    blocks: list[dict[str, Any]] = []
    for spec in doc.specs:
        anchor = spec["_anchor"]
        if spec["_late"]:
            path = _path_at(marks, anchor)
        else:
            if spec["kind"] == "heading":
                while stack and stack[-1][0] >= spec["level"]:
                    stack.pop()
            path = tuple(text for _, text in stack)
            if spec["kind"] == "heading":
                stack.append((spec["level"], spec["text"]))
            marks.append((anchor, tuple(text for _, text in stack)))
        block = {k: v for k, v in spec.items() if not k.startswith("_")}
        block["section_path"] = path
        block["locator"] = FlowLocator(section_path=path, paragraph_index=anchor)
        blocks.append(block)
    seen = Counter[tuple[int, int, str]]()
    records = []
    for region in doc.regions:
        region_id = region.region_id
        if region_id is None:
            key = (region.section, region.anchor, region.tag)
            region_id = f"s{region.section}-p{region.anchor}-{region.tag}-{seen[key]}"
            seen[key] += 1
        records.append(RegionRecord(region_id=region_id, kind=region.kind, chosen="det",
                                    attempts=(Attempt(layer="det"),), fallback_reason=region.reason, gate=region.gate,
                                    locator=FlowLocator(section_path=_path_at(marks, region.anchor),
                                                        paragraph_index=region.anchor)))
    return tuple(blocks), tuple(records)


def _path_at(marks: list[tuple[int, tuple[str, ...]]], anchor: int) -> tuple[str, ...]:
    """닻 문단까지 처리한 뒤의 제목 경로(그 닻 이하의 마지막 표시)."""
    k = bisect.bisect_right(marks, anchor, key=lambda m: m[0])
    return marks[k - 1][1] if k else ()


class HwpxParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".hwpx",)

    def parse(self, data: bytes, name: str) -> ParsedSource:
        return self._parse(data, name)[0]

    def _parse(self, data: bytes, name: str) -> tuple[ParsedSource, tuple[Counts, ...]]:
        """ParsedSource와 구역별 장부 숫자(테스트가 본다)."""
        package = Package(data, name)
        doc = _Doc(name, read_styles(package.xml("Contents/header.xml", f"{HH}head")))
        base = 0
        for index, member in enumerate(package.sections):
            root = package.xml(member, f"{HS}sec")
            section = _Section(doc, index, root, base)
            section.build()
            base += sum(1 for child in root if child.tag == P)
            del section, root  # 다음 구역 전에 트리를 버린다
        for k, member in enumerate(package.masterpages):
            chars = sum(source_chars(t) for t in package.xml(member, None).iter(T))
            if chars:
                doc.regions.append(_Region("masterpage-text", "paragraph", MASTERPAGE_TEXT, 0, 0,
                                           _gate("chars", chars, None), f"masterpage-{k}"))
        blocks, regions = _finish(doc)
        return ParsedSource(mime=MIME, blocks=blocks, regions=regions), tuple(doc.counts)
