"""숫자 정확 일치(NUM_V1, 스펙 §7.5). 토큰은 정규화 1~4단계 뒤·공백 지우기 전 글자에서 뽑은 (부호, 숫자 글자열, 단위)
셋이고 셋 다 같아야 같은 토큰이다. 표는 구조가 맞을 때만 같은 (행, 열) 칸끼리 견주고, 아니면 unverified로 센다."""

import re
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from ..blocks import Table, structure_match
from .text import multiset_pr

NUM = "NUM_V1"
SIGNS = "+-△▲▽▼"
# 스펙 §7.5 목록(NFKC 뒤 꼴: ㎡ → m2, ㎢ → km2, ㎞ → km, ㎏ → kg)과 개발 분할 짝 102건에서 숫자 뒤 30번 이상 나온
# 여러 글자 후보(2026-11-12 실측: 천명 182, 만명 118, 시간 65, 개월 58, 개사 46)는 숫자 뒤 어떤 공백(빈칸·줄바꿈)을
# 사이에 두어도 붙는다. 실측으로 더한 한 글자 후보(만 310, 차 194, 분 135, 대 126, 천 110, 회 81, 조 64, 시 54, 억 54,
# 주 48, 호 36, 종 34)는 다음 낱말의 첫 글자와 겹치므로 숫자에 바로 붙을 때만 단위다(컨트롤러 판정 R5). 긴 것부터 맞춘다
SPACED_UNITS = (
    "%p", "%", "‰", "m2", "km2", "km", "kg", "원", "천원", "만원", "억원", "조원", "명", "개", "건", "배", "세", "년", "월", "일",
    "천명", "만명", "시간", "개월", "개사",
)
ADJACENT_UNITS = ("만", "차", "분", "대", "천", "회", "조", "시", "억", "주", "호", "종")
UNITS = SPACED_UNITS + ADJACENT_UNITS


def _alt(units: tuple[str, ...]) -> str:
    return "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))


_TOKEN = re.compile(rf"(?P<sign>[{re.escape(SIGNS)}])?(?P<num>[0-9]+(?:,[0-9]{{3}})*(?:\.[0-9]+)?)"
                    rf"(?:\s*(?P<unit>{_alt(SPACED_UNITS)})|(?P<adjacent>{_alt(ADJACENT_UNITS)}))?")


class Token(NamedTuple):
    sign: str
    number: str
    unit: str


def tokens(text: str) -> list[Token]:
    """[부호]? 숫자 [단위]? 토큰 열. 부호는 숫자 바로 앞. 단위는 SPACED_UNITS면 숫자 뒤 공백을 넘어, ADJACENT_UNITS면
    숫자에 바로 붙을 때만."""
    return [Token(m["sign"] or "", m["num"], m["unit"] or m["adjacent"] or "") for m in _TOKEN.finditer(text)]


def token_pr(pred: list[Token], truth: list[Token]) -> tuple[float | None, float | None]:
    """토큰 다중집합 정밀도·재현율(기록용)."""
    return multiset_pr(Counter(pred), Counter(truth))


@dataclass(frozen=True, slots=True)
class CellScore:
    """표 하나의 숫자 칸: numeric = 원본에서 숫자가 든 칸 수, exact = 그 가운데 토큰 열이 같은 칸, unverified = 구조가
    맞지 않거나 짝이 없어 채점하지 않은 숫자 칸."""

    numeric: int
    exact: int
    unverified: int


def table_cells(truth: Table, pred: Table | None) -> CellScore:
    numeric = [c for c in truth.cells if tokens(c.text)]
    if pred is None or not structure_match(truth, pred):
        return CellScore(len(numeric), 0, len(numeric))
    by_pos = {(c.row, c.col): c.text for c in pred.cells}
    exact = sum(tokens(by_pos[(c.row, c.col)]) == tokens(c.text) for c in numeric)
    return CellScore(len(numeric), exact, 0)


def block_exact(pred: str, truth: str) -> bool | None:
    """글자 블록 하나: 원본(truth, 짝 묶음 조각들이 소비한 범위를 원본 차례로 이은 것과 견준다)에 숫자가 없으면 None,
    있으면 토큰 열(차례 포함)이 정확히 같은가. 짝이 없는 원본 블록은 pred를 빈 글자로 준다(모두 틀림)."""
    want = tokens(truth)
    return None if not want else tokens(pred) == want


def exact_rate(results: list[bool | None]) -> float | None:
    """숫자가 있는 단위 가운데 정확히 같은 비율(주 지표). 숫자가 있는 단위가 없으면 None."""
    scored = [r for r in results if r is not None]
    return sum(scored) / len(scored) if scored else None
