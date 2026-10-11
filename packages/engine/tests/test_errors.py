import pytest

import hanji
from hanji import errors
from hanji.errors import (
    AssetNotFound, DocumentNotFound, HanjiError, ModelError, OcrUnavailable, ParseError, StoreConflict,
    UnsupportedFormat, VersionNotFound, VlmUnavailable,
)


@pytest.mark.parametrize("kind", [UnsupportedFormat, ParseError, DocumentNotFound, VersionNotFound, StoreConflict,
                                  VlmUnavailable, ModelError])
def test_all_errors_share_base(kind):
    assert issubclass(kind, HanjiError)


def test_package_exports_the_model_error():
    """models fetch·resolve의 오류도 다른 오류처럼 패키지에서 바로 가져온다."""
    assert hanji.ModelError is ModelError and "ModelError" in hanji.__all__


def test_parse_error_keeps_reason_and_location():
    err = ParseError("invalid table", "보고서.md:3-5")
    assert (err.reason, err.location) == ("invalid table", "보고서.md:3-5")
    assert str(err) == "invalid table (보고서.md:3-5)"
    assert str(ParseError("empty")) == "empty" and ParseError("empty").location is None


@pytest.mark.parametrize(("exc", "code"), [
    (UnsupportedFormat("x"), 3), (ParseError("x", "a.md:1"), 4), (DocumentNotFound("d"), 5), (VersionNotFound("d"), 5),
    (AssetNotFound("sha256:0"), 5), (StoreConflict("x"), 1), (OcrUnavailable("x"), 1), (HanjiError("x"), 1),
    (FileNotFoundError("x"), 1), (MemoryError(), 1),
])
def test_exit_code_maps_errors_like_the_cli(exc, code):
    """parse의 종료 코드와 ingest의 문서별 error.code가 같은 대응을 쓴다: 3 형식, 4 파싱, 5 없음, 그 밖 1."""
    assert errors.exit_code(exc) == code
