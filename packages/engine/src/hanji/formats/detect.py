"""형식 판별: 확장자(소문자)로 파서를 고른다. 확장자가 모호한 형식이 붙으면 매직바이트 판별을 더한다."""

from collections.abc import Sequence
from pathlib import PurePath

from ..errors import UnsupportedFormat
from .base import Parser
from .hwp import HwpParser
from .hwpx import HwpxParser
from .markdown import MarkdownParser
from .ooxml.docx.parser import DocxParser
from .ooxml.pptx.parser import PptxParser
from .pdf import PdfParser


def default_parsers(ocr: bool | None = None, layout: bool | None = None) -> tuple[Parser, ...]:
    """기본 파서 묶음. 형식이 붙을 때마다 여기에 더한다. ocr·layout은 PdfParser의 스캔 쪽 OCR·레이아웃 모델 설정
    (None = 설치돼 있으면 켬)."""
    return (MarkdownParser(), PdfParser(ocr=ocr, layout=layout), HwpxParser(), DocxParser(), PptxParser(), HwpParser())


def detect_parser(name: str, parsers: Sequence[Parser]) -> Parser:
    suffix = PurePath(name).suffix.lower()
    for parser in parsers:
        if suffix in parser.extensions:
            return parser
    raise UnsupportedFormat(f"unsupported format: {name}")
