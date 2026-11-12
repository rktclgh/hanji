"""표 지표 중 들여오지 않은 것: 완벽(구조 일치 + 칸 글자 일치, research/tableeval의 perfect)과 찾기 정밀도·재현율."""

import re
import unicodedata

from ..blocks import Table, structure_match


def _norm(text: str) -> str:
    """research/tableeval common.norm과 같다: NFC 뒤 모든 유니코드 공백(\\s)을 지운다(컨트롤러 판정 R7)."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def perfect(pred: Table | None, truth: Table) -> bool:
    """행·열 수와 칸 배치가 같고, 펼친 격자의 모든 자리에서 _norm한 칸 글자가 같다."""
    if pred is None or not structure_match(truth, pred):
        return False
    pred_grid = pred.grid()
    return all(_norm(pred_grid[pos]) == _norm(text) for pos, text in truth.grid().items())


def detection_pr(matched: int, n_pred: int, n_truth: int) -> tuple[float | None, float | None]:
    """표 찾기 (정밀도, 재현율). 분모가 0이면 None."""
    if not 0 <= matched <= min(n_pred, n_truth):
        raise ValueError("matched tables must be between 0 and both counts")
    return (matched / n_pred if n_pred else None, matched / n_truth if n_truth else None)
