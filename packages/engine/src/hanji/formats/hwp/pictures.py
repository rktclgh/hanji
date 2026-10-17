"""BinData → 공유 처리부의 그림 원천(HWP 스펙 §7.4, hwpx/owpml.py Pictures 프로토콜).

DocInfo BIN_DATA 레코드를 차례대로 읽는다(순번 1부터: 그림 레코드의 BinData 참조는 저장 id가 아니라 이 순번이다, 실측 329개
가운데 5개는 둘이 다르다). 속성(UINT16) bit 0–3 종류(0 연결·1 포함·2 OLE 저장소), bit 4–5 압축(0 문서 따름·1 압축·2 비압축).
포함이면 저장 id(UINT16)와 확장자(WORD 길이 + WCHAR) → 스트림 BinData/BIN{저장 id:04X}.{확장자}(디렉터리의 정확한 이름,
없으면 대소문자만 다른 스트림 하나). 연결은 경로를 따라가지 않는다(embedded=False → figure_image_linked). OLE 저장소와
모르는 종류는 그림 스트림이 없다(figure_image_missing). 바이트 읽기 상한은 HWPX와 같은 값(container.MAX_BINARY_*·
MAX_RATIO, 읽을 때마다 읽는다): 하나가 넘으면 TooLarge, 합이 넘으면 OverBudget, 깨진 압축은 ParseError(그 그림만 잃는다).
압축 결정은 항목마다 따로다: 같은 저장 id인데 결정이 다른 항목은 href에 #packed·#raw를 달아 구별한다(처리부가 href로 캐시)."""

import struct
import zlib
from collections.abc import Mapping

from ...errors import ParseError
from ..hwpx import container
from ..hwpx.container import Item, OverBudget, TooLarge
from .cfb import Compound
from .stream import Record

BIN_DATA = 0x10 + 2
LINK, EMBEDDING = 0, 1
FOLLOW, ALWAYS, NEVER = 0, 1, 2  # 압축(bit 4–5)


class BinData:
    """그림 원천. items는 BIN_DATA 순번(문자열) → Item(스트림 경로, "", 포함 여부)."""

    def __init__(self, cfb: Compound, records: list[Record], compressed: bool, location: str) -> None:
        self.cfb = cfb
        self.items: Mapping[str, Item] = {}
        self._streams: dict[str, tuple[str, bool]] = {}  # href → (스트림 경로, 압축을 풀지)
        self.read = 0  # 실제로 읽은 그림 스트림 바이트 합(압축이면 푼 바이트)
        folded: dict[str, list[str]] = {}
        for path in cfb.streams:
            folded.setdefault(path.casefold(), []).append(path)
        items: dict[str, Item] = {}
        for record in records:
            if record.tag != BIN_DATA:
                continue
            body = record.body
            where = f"{location}#{record.index}"
            if len(body) < 2:
                raise ParseError("DocInfo BIN_DATA record too short", where)
            (attr,) = struct.unpack_from("<H", body, 0)
            key = str(len(items) + 1)
            if attr & 0xF == LINK:
                items[key] = Item("", "", False)
                continue
            if attr & 0xF != EMBEDDING:  # OLE 저장소·모르는 종류: 그림 스트림이 없다
                items[key] = Item("", "", True)
                continue
            if len(body) < 6:
                raise ParseError("DocInfo BIN_DATA record too short", where)
            sid, n = struct.unpack_from("<HH", body, 2)
            if len(body) < 6 + 2 * n:
                raise ParseError("DocInfo BIN_DATA record too short", where)
            path = f"BinData/BIN{sid:04X}.{body[6:6 + 2 * n].decode('utf-16-le', 'replace')}"
            if path not in cfb.streams and len(folded.get(path.casefold(), [])) == 1:
                path = folded[path.casefold()][0]  # 확장자 대소문자만 다른 스트림 하나
            mode = attr >> 4 & 3
            packed = mode == ALWAYS or (mode not in (ALWAYS, NEVER) and compressed)
            href = path
            if self._streams.get(href, (path, packed)) != (path, packed):  # 같은 저장 id인데 압축 결정이 다른 항목
                href = f"{path}#{'packed' if packed else 'raw'}"
            self._streams[href] = (path, packed)
            items[key] = Item(href, "", True)
        self.items = items

    def has(self, member: str) -> bool:
        return member in self._streams and self.cfb.has(self._streams[member][0])

    def binary(self, member: str) -> bytes:
        """그림 스트림 바이트(압축이면 푼 것). 읽기는 min(하나 상한, 합의 남은 몫) + 1바이트까지만 한다."""
        path, packed = self._streams[member]
        location = f"{self.cfb.name}:{path}"
        each, total = container.MAX_BINARY_BYTES, container.MAX_BINARY_TOTAL
        left = total - self.read
        if left <= 0:
            raise OverBudget(f"picture streams larger than {total} bytes in total")
        limit = min(each, left)
        raw = self.cfb.read(path, limit)
        if packed and len(raw) <= limit:  # 압축 바이트가 이미 한도를 넘으면 풀지 않고 크기로 거른다
            inflater = zlib.decompressobj(-15)
            cap = max(container.RATIO_FLOOR, container.MAX_RATIO * len(raw))
            try:
                data = inflater.decompress(raw, min(limit, cap) + 1)
            except zlib.error as exc:
                raise ParseError(f"invalid compressed picture stream: {exc}", location) from None
            if len(data) > cap:
                raise TooLarge(f"picture stream compression ratio over {container.MAX_RATIO}")
            if len(data) <= limit and not inflater.eof:
                raise ParseError("truncated compressed picture stream", location)
        else:
            data = raw
        self.read += len(data)
        if len(data) > each:
            raise TooLarge(f"picture stream larger than {each} bytes")
        if len(data) > left:
            raise OverBudget(f"picture streams larger than {total} bytes in total")
        return data
