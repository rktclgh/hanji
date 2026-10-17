"""HWP 5.0 → ParsedSource(HWP 스펙 §3). PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다(모델·OCR 없음).

복합 파일(cfb.py)을 열고 FileHeader(암호·DRM은 ParseError)와 DocInfo → Styles(docinfo.py)를 읽은 뒤, BodyText/Section{n}
(0부터 빈틈 없이)마다 레코드 트리를 OWPML 요소 트리로 옮겨 쓰고(transcode.py, 옮겨 쓰기 장부) HWPX 구역 처리부
(hwpx/owpml.py)를 그대로 돌린다. 트리는 구역마다 버린다. 배포용 문서는 본문을 읽지 않고 암호화되지 않은 미리보기 글
(PrvText)만 부분 복원으로 낸다(§9). 형식 엿보기: zip이면 HWPX 파서로 넘기고(실제로 HWPX인 .hwp), HWP 3.x 서명은
ParseError. 그림 바이트는 DocInfo BIN_DATA가 가리키는 BinData 스트림에서 읽는다(pictures.py)."""

import re

from hanji_contracts import GateResult, RegionRecord

from ...errors import ParseError
from ..base import ParsedSource
from ..flowdoc import Region, finish, gate
from ..flowledger import ink
from ..hwpx import HwpxParser
from ..hwpx.owpml import _Doc, _finish, _Section
from ..pdf.group import RESCUED_CONFIDENCE
from . import transcode
from .cfb import Compound
from .docinfo import flatten, read_styles
from .pictures import BinData
from .stream import HWP3_SIGNATURE, Reader, file_header
from .transcode import INVALID_UTF16, Audit, Nodes, utf16, wchars

MIME = "application/x-hwp"  # IANA 등록 값이 없다(2026-10-17 application 목록 1,813개에 hwp·hancom 없음)
SECTION = re.compile(r"BodyText/Section(0|[1-9][0-9]*)")
PRVTEXT_BYTES = 1024 * 1024  # 배포용 미리보기 글은 이만큼만 읽는다
DISTRIBUTION = "distribution_preview_only"
LINES = re.compile(r"\r\n|\n")


def _sections(cfb: Compound) -> list[str]:
    """BodyText/Section{n} 경로들(n 차례). 0부터 빈틈 없어야 하고 BodyText 아래 다른 이름은 없어야 한다(건너뛰거나 두 번
    읽으면 장부가 놓친다. 실문서 20건 모두 그렇다)."""
    odd = sorted(path for path in cfb.streams if path.startswith("BodyText/") and not SECTION.fullmatch(path))
    if odd:
        raise ParseError("unexpected stream under BodyText", f"{cfb.name}:{odd[0]}")
    found = sorted((int(m.group(1)), path) for path in cfb.streams if (m := SECTION.fullmatch(path)))
    if not found:
        raise ParseError("no BodyText sections", cfb.name)
    if [n for n, _ in found] != list(range(len(found))):
        raise ParseError("BodyText sections are not numbered 0..n-1", cfb.name)
    return [path for _, path in found]


def _distribution(cfb: Compound) -> tuple[tuple[dict, ...], tuple[RegionRecord, ...]]:
    """배포용 문서(§9): PrvText(UTF-16LE)를 PRVTEXT_BYTES까지 읽어 U+FFFD·공백뿐이 아닌 줄마다 구조 문단(신뢰도 0.3)과
    처리 이력 distribution_preview_only 하나(preview_chars, truncated). 홀수 바이트는 버리고 짝 없는 대리 문자는
    U+FFFD, 둘 다 invalid_utf16. 한도에서 잘린 끝(홀수 바이트·높은 대리 문자)은 잘림이지 오류가 아니다."""
    location = f"{cfb.name}:PrvText"
    raw = cfb.read("PrvText", PRVTEXT_BYTES) if cfb.has("PrvText") else b""
    truncated = len(raw) > PRVTEXT_BYTES  # read는 한도 + 1바이트까지 준다
    raw = raw[:PRVTEXT_BYTES]
    bad = 0
    if len(raw) % 2:
        raw = raw[:-1]  # 홀수 바이트의 끝은 버린다
        bad += not truncated
    units = wchars(raw)
    if truncated and units and 0xD800 <= units[-1] <= 0xDBFF:  # 한도에 잘린 대리쌍의 앞쪽
        del units[-1]
    text, lone = utf16(units)
    bad += lone
    if not text.replace("�", "").strip():
        raise ParseError("distribution document: content is encrypted (preview text unavailable)", location)
    specs = [{"kind": "paragraph", "text": line.strip(), "confidence": RESCUED_CONFIDENCE, "state": "det",
              "text_source": "native", "_anchor": k, "_late": False}
             for k, line in enumerate(LINES.split(text)) if line.replace("�", "").strip()]
    shown = gate("preview_chars", ink(text), None)
    flag = gate("truncated", int(truncated), None)
    regions = [Region("distribution", "paragraph", DISTRIBUTION, 0, 0,
                      GateResult(passed=shown.passed and flag.passed, checks=shown.checks + flag.checks),
                      "distribution")]
    if bad:
        regions.append(Region("invalid-utf16", "paragraph", INVALID_UTF16, 0, 0, gate("chars", bad, None)))
    return finish(specs, regions, True)


class HwpParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".hwp",)

    def parse(self, data: bytes, name: str) -> ParsedSource:
        if data[:2] == b"PK":  # 실제로는 HWPX(zip)인 .hwp
            return HwpxParser().parse(data, name)
        return self._parse(data, name)[0]

    def _parse(self, data: bytes, name: str) -> tuple[ParsedSource, tuple, tuple[Audit, ...]]:
        """ParsedSource, 구역별 HWPX 장부 숫자, 구역별 옮겨 쓰기 장부 숫자(테스트가 본다)."""
        if data.startswith(HWP3_SIGNATURE):
            raise ParseError("HWP 3.x documents are not supported", name)
        cfb = Compound(data, name)
        header = file_header(cfb)
        reader = Reader(cfb, header.compressed)
        if not cfb.has("DocInfo"):
            raise ParseError("missing DocInfo stream", name)
        records = flatten(reader.tree("DocInfo"))
        styles = read_styles(records, f"{name}:DocInfo")
        if header.distribution:
            blocks, regions = _distribution(cfb)
            return ParsedSource(mime=MIME, blocks=blocks, regions=regions), (), ()
        doc = _Doc(name, styles, BinData(cfb, records, header.compressed, f"{name}:DocInfo"))
        nodes = Nodes()
        audits: list[Audit] = []
        base = 0
        for index, path in enumerate(_sections(cfb)):
            result = transcode.section(reader.tree(path), index, base, f"{name}:{path}", nodes)
            doc.regions.extend(result.regions)
            audits.append(result.audit)
            section = _Section(doc, index, result.root, base)
            section.build()
            base += len(result.root)
            del section, result  # 다음 구역 전에 트리를 버린다
        blocks, regions = _finish(doc)
        return (ParsedSource(mime=MIME, blocks=blocks, assets=doc.budget.assets, regions=regions), tuple(doc.counts),
                tuple(audits))
