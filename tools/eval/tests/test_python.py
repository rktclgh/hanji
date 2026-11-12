import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]


def test_the_eval_interpreter_is_the_pinned_python():
    # 부트스트랩 고정 표본·라이선스 표(cp312 휠)가 이 판에 묶여 있다: tools/eval/.python-version과 CI가 3.12를 고정한다
    pinned = (EVAL / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned == "3.12"
    assert f"{sys.version_info.major}.{sys.version_info.minor}" == pinned
