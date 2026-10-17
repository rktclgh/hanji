"""HWPX(한컴 OWPML, zip + XML) → ParsedSource. PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다(모델·OCR 없음).
꾸러미를 열고(container.py) header.xml 스타일을 읽은 뒤, 구역(spine 순서)마다 트리를 처리부(owpml.py, HWP 파서와
함께 쓴다)로 블록 명세로 바꾸고 버린다. 마스터 쪽 글자는 읽지 않고 수만 남긴다. 바이트가 OLE 복합 파일(HWP 5.0)이거나
HWP 3.x 서명이면 HWP 파서로 넘긴다(확장자가 틀린 파일)."""

from ..base import ParsedSource
from ..flowdoc import Region, finish, gate  # noqa: F401  (finish는 테스트가 본다)
from .container import HH, HS, MIME, Package
from .header import read_styles
from .ledger import Counts, source_chars
from .owpml import MASTERPAGE_TEXT, P, T, _Doc, _finish, _Section

# HWP로 넘길 서명(hwp/cfb.py·hwp/stream.py와 같은 값. hwp가 hwpx를 import하므로 여기서 hwp를 읽지 않는다)
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
HWP3_SIGNATURE = b"HWP Document File V3.00"


class HwpxParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".hwpx",)

    def parse(self, data: bytes, name: str) -> ParsedSource:
        if data[:8] == OLE_SIGNATURE or data.startswith(HWP3_SIGNATURE):  # 실제로는 HWP(OLE 복합 파일)인 .hwpx
            from ..hwp.parser import HwpParser

            return HwpParser().parse(data, name)
        return self._parse(data, name)[0]

    def _parse(self, data: bytes, name: str) -> tuple[ParsedSource, tuple[Counts, ...]]:
        """ParsedSource와 구역별 장부 숫자(테스트가 본다)."""
        package = Package(data, name)
        doc = _Doc(name, read_styles(package.xml("Contents/header.xml", f"{HH}head")), package)
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
                doc.regions.append(Region("masterpage-text", "paragraph", MASTERPAGE_TEXT, 0, 0,
                                          gate("chars", chars, None), f"masterpage-{k}"))
        blocks, regions = _finish(doc)
        return (ParsedSource(mime=MIME, blocks=blocks, assets=doc.budget.assets, regions=regions),
                tuple(doc.counts))
