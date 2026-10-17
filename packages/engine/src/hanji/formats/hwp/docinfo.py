"""DocInfo 레코드 → HWPX header.py와 같은 형의 Styles(HWP 스펙 §7.5). id는 그 종류 레코드의 DocInfo 안 순번이다:
글자 모양·문단 모양은 0부터, 테두리 채움은 1부터(HWPX header.xml의 id와 같다, 짝 5건 실측).

- 문단 모양(PARA_SHAPE) 속성 1의 bit 23–24 문단 머리 종류(0 없음·1 개요·2 번호·3 글머리), bit 25–27 수준(0부터)
- 글자 모양(CHAR_SHAPE) 기준 크기(오프셋 42, INT32, 1/100pt) × 한글 상대 크기(오프셋 28, UINT8, %), 속성(오프셋 46)
  bit 1 굵게
- 테두리 채움(BORDER_FILL) 채우기 종류(오프셋 32): 단색(bit 0)이면 배경색(오프셋 36, COLORREF 0x00BBGGRR)이 흰색도
  없음(상위 바이트 0xFF, HWPX faceColor="none")도 아닐 때, 이미지(bit 1)·그러데이션(bit 2)이면 채움

필요한 필드보다 짧은 레코드는 손상이다(이 필드들은 5.0.0부터 있다): ParseError."""

import struct

from ...errors import ParseError
from ..hwpx.header import BOUND, CharShape, Styles
from .stream import Record

BORDER_FILL, CHAR_SHAPE, PARA_SHAPE = 0x10 + 4, 0x10 + 5, 0x10 + 9
HEADS = ("NONE", "OUTLINE", "NUMBER", "BULLET")
WHITE = 0xFFFFFF


def _filled(body: bytes) -> bool:
    (kind,) = struct.unpack_from("<I", body, 32)
    if kind & 0b110:  # 이미지·그러데이션
        return True
    if kind & 1:
        (back,) = struct.unpack_from("<I", body, 36)
        return back >> 24 != 0xFF and back & 0xFFFFFF != WHITE
    return False


def flatten(top: list[Record]) -> list[Record]:
    """레코드 트리를 문서 차례(앞 순회)로 편다(명시적 스택)."""
    out: list[Record] = []
    stack = list(reversed(top))
    while stack:
        record = stack.pop()
        out.append(record)
        stack.extend(reversed(record.children))
    return out


def read_styles(records: list[Record], location: str) -> Styles:
    styles = Styles()
    counts = {BORDER_FILL: 0, CHAR_SHAPE: 0, PARA_SHAPE: 0}
    need = {BORDER_FILL: 36, CHAR_SHAPE: 50, PARA_SHAPE: 4}
    names = {BORDER_FILL: "BORDER_FILL", CHAR_SHAPE: "CHAR_SHAPE", PARA_SHAPE: "PARA_SHAPE"}
    for record in records:
        if record.tag not in counts:
            continue
        body = record.body
        short = len(body) < need[record.tag] or (record.tag == BORDER_FILL and struct.unpack_from(
            "<I", body, 32)[0] & 1 and len(body) < 40)
        if short:
            raise ParseError(f"DocInfo {names[record.tag]} record too short", f"{location}#{record.index}")
        k = counts[record.tag]
        counts[record.tag] += 1
        if record.tag == PARA_SHAPE:
            (attr,) = struct.unpack_from("<I", body, 0)
            styles.headings[str(k)] = (HEADS[attr >> 23 & 3], attr >> 25 & 7)
        elif record.tag == CHAR_SHAPE:
            (height,) = struct.unpack_from("<i", body, 42)
            (attr,) = struct.unpack_from("<I", body, 46)
            size = max(-BOUND, min(BOUND, height)) / 100 * body[28] / 100  # HWPX header.py와 같은 계산
            styles.chars[str(k)] = CharShape(size, bool(attr & 2))
        else:
            styles.fills[str(k + 1)] = _filled(body)
    return styles
