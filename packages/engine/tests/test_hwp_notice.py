"""한컴 공개 문서 이용 조건의 고지 문구(HWP 스펙 §2.1): README·NOTICE·소스(formats/hwp/__init__.py)에 같은 문장."""

from pathlib import Path

import hanji.formats.hwp

REPO = Path(__file__).resolve().parents[3]
SENTENCE = "본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다."


def test_the_hancom_notice_is_in_the_readmes_the_notice_file_and_the_source():
    for path in (REPO / "README.md", REPO / "packages" / "engine" / "README.md", REPO / "NOTICE"):
        assert SENTENCE in path.read_text(encoding="utf-8"), path.name
    assert SENTENCE in hanji.formats.hwp.__doc__
