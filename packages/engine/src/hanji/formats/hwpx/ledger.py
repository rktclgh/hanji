"""글자 장부(스펙 §10). 출처는 구역 XML의 모든 hp:t와 hp:textart@text다. 블록 글자는 묶음 (출처, 글자)들을 이어 만들고,
장부는 같은 묶음 목록에서 출처마다 넣은 횟수와 넣은 공백 아닌 글자 수를 더한다. 묶음 하나는 출처 하나를 한 번 낸
것이다(<t>A<tab/>B</t>는 묶음 하나). 출처 없는 묶음(None)은 끼운 구분자다."""

import xml.etree.ElementTree as ET

from ..flowledger import Bundle, Counts, Ledger, Source, ink  # noqa: F401  (옮긴 이름도 여기서 내보낸다)
from .container import HP

T = f"{HP}t"
INLINE = {f"{HP}tab": "\t", f"{HP}lineBreak": "\n", f"{HP}nbSpace": " ", f"{HP}fwSpace": " ",
          f"{HP}titleMark": ""}  # hp:t 안 요소 → 대치 글자(모두 공백이거나 없음)


def source_chars(t: ET.Element) -> int:
    """hp:t 출처의 n(x): t.text + 자식 요소 안 글자(문서 순서) + 자식 뒤 글자에서 공백 아닌 코드 포인트 수.
    대치 글자는 모두 공백이라 들지 않는다. 블록 글자를 만드는 쪽과 따로 센다(같은 함수를 쓰면 빠뜨림이 감춰진다)."""
    count = ink(t.text or "")
    for child in t:
        count += ink("".join(child.itertext()))  # 아는 인라인 요소 안 글자도 센다(대치 글자는 공백이라 들지 않는다)
        count += ink(child.tail or "")
    return count
