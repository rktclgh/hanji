"""엔진 예외와 종료 코드 대응(exit_code: CLI parse와 ingest의 문서별 오류 코드가 같이 쓴다)."""


class HanjiError(Exception):
    """엔진 예외의 공통 부모."""


class UnsupportedFormat(HanjiError):
    """판별할 수 없거나 파서가 없는 형식."""


class ParseError(HanjiError):
    """파싱 실패. reason은 원인, location은 위치(파일 이름·줄 범위 등)."""

    def __init__(self, reason: str, location: str | None = None) -> None:
        super().__init__(f"{reason} ({location})" if location else reason)
        self.reason = reason
        self.location = location


class DocumentNotFound(HanjiError):
    """저장소에 없는 문서."""


class VersionNotFound(HanjiError):
    """문서는 있지만 그 버전이 없다."""


class StoreConflict(HanjiError):
    """커밋하려는 버전이 저장된 최신 버전 + 1이 아니다."""


class VlmUnavailable(HanjiError):
    """이 엔진에는 VLM 경로가 없다."""


class OcrUnavailable(HanjiError):
    """OCR을 켜라고 했는데 OCR 추가 설치(hanji[ocr])가 없거나 깨졌다. 설정 오류(종료 코드 1)."""


class AssetNotFound(HanjiError):
    """저장소에 없는 그림 자산(sha256). asset은 찾은 해시."""

    def __init__(self, asset: str) -> None:
        super().__init__(f"asset not found: {asset}")
        self.asset = asset


class ModelError(HanjiError):
    """모델 파일을 찾지 못했거나, 고정한 크기·SHA-256과 다르거나, 받지 못했다. 설정 오류(종료 코드 1)."""


class LayoutUnavailable(HanjiError):
    """레이아웃 모델을 켜라고 했는데(또는 자동 모드에서 모델을 돌릴 쪽이 있는데) 레이아웃 추가 설치
    (hanji[layout])나 모델 파일이 없거나 깨졌다. 설정 오류(종료 코드 1)."""


def exit_code(exc: BaseException) -> int:
    """예외 → 종료 코드: 3 지원하지 않는 형식, 4 파싱 실패, 5 문서·버전·그림 이미지 없음, 그 밖(엔진 밖 예외 포함) 1."""
    for kind, code in ((UnsupportedFormat, 3), (ParseError, 4), (DocumentNotFound, 5), (VersionNotFound, 5),
                       (AssetNotFound, 5)):
        if isinstance(exc, kind):
            return code
    return 1
