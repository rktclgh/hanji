"""글자 장부(스펙 §10). 출처는 구역 XML의 모든 hp:t, hp:textart@text, 덧말 hp:mainText·hp:subText,
hp:compose@composeText(원 모양이면 원문자)다. 블록 글자는 묶음 (출처, 글자)들을 이어 만들고, 장부는 같은 묶음 목록에서 출처마다 넣은 횟수와
넣은 공백 아닌 글자 수를 더한다. 묶음 하나는 출처 하나를 한 번 낸 것이다(<t>A<tab/>B</t>는 묶음 하나). 출처 없는
묶음(None)은 끼운 구분자다."""

import xml.etree.ElementTree as ET

from ..flowledger import Bundle, Counts, Ledger, Source, ink  # noqa: F401  (옮긴 이름도 여기서 내보낸다)
from .container import HP

T = f"{HP}t"
INLINE = {f"{HP}tab": "\t", f"{HP}lineBreak": "\n", f"{HP}nbSpace": " ", f"{HP}fwSpace": " ",
          f"{HP}titleMark": ""}  # hp:t 안 요소 → 대치 글자(모두 공백이거나 없음)


TEXTART, MAIN_TEXT, SUB_TEXT, COMPOSE = f"{HP}textart", f"{HP}mainText", f"{HP}subText", f"{HP}compose"
CIRCLE = "SHAPE_CIRCLE"  # 원 모양 글자 겹침의 circleType(실문서 값)


def _circled_table() -> dict[str, str]:
    """원 모양으로 감싼 글자 → 유니코드 원문자(분해가 '<circle> 그 글자'인 BMP 글자만): ⓪ 0, ①–⑳ 1–20,
    ㉑–㉟ 21–35, ㊱–㊿ 36–50, ⓐ–ⓩ, Ⓐ–Ⓩ. 원문자 한글·가타카나·한자는 넣지 않는다(HWP 쪽 실측이 없다)."""
    table = {"0": "\u24ea"}
    for first, low, high in ((0x2460, 1, 20), (0x3251, 21, 35), (0x32B1, 36, 50)):
        table.update({str(n): chr(first + n - low) for n in range(low, high + 1)})
    for k in range(26):
        table[chr(0x61 + k)] = chr(0x24D0 + k)
        table[chr(0x41 + k)] = chr(0x24B6 + k)
    return table


CIRCLED = _circled_table()


def circled(text: str, circle_type: str | None) -> str:
    """글자 겹침 글자: 원 모양이고 글자 전체가 표에 있으면 그 원문자 하나(\"12\" → ⑫), 아니면 그대로(손실 없음).
    HWP는 겹친 결과(①)를 저장하고 HWPX는 바탕 글자와 모양을 저장한다(대량 코퍼스 짝)."""
    return CIRCLED.get(text, text) if circle_type == CIRCLE else text


def source_chars(t: ET.Element) -> int:
    """hp:t 출처의 n(x): t.text + 자식 요소 안 글자(문서 순서) + 자식 뒤 글자에서 공백 아닌 코드 포인트 수.
    대치 글자는 모두 공백이라 들지 않는다. 블록 글자를 만드는 쪽과 따로 센다(같은 함수를 쓰면 빠뜨림이 감춰진다)."""
    count = ink(t.text or "")
    for child in t:
        count += ink("".join(child.itertext()))  # 아는 인라인 요소 안 글자도 센다(대치 글자는 공백이라 들지 않는다)
        count += ink(child.tail or "")
    return count


def textart_chars(element: ET.Element) -> int:
    """글맵시 출처의 n(x): text 속성."""
    return ink(element.get("text", ""))


def ruby_chars(element: ET.Element) -> int:
    """덧말 본말·덧말 출처의 n(x): 요소 안 글자(문서 순서)."""
    return sum(ink(part) for part in element.itertext())


def compose_chars(element: ET.Element) -> int:
    """글자 겹침 출처의 n(x): composeText를 circled로 바꾼 글자(처리부가 내는 글자와 따로 센다)."""
    return ink(circled(element.get("composeText", ""), element.get("circleType")))


# hp:t 밖 출처의 n(x). 처리부의 블록 글자(_source_text)와 따로 센다
SOURCE_CHARS = {TEXTART: textart_chars, MAIN_TEXT: ruby_chars, SUB_TEXT: ruby_chars, COMPOSE: compose_chars}
