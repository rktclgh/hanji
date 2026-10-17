"""HWP 5.0 파서(한컴 옛 바이너리: OLE 복합 파일 + 레코드 스트림). 레코드를 HWPX와 같은 모양의 요소 트리로 옮겨 쓴 뒤
HWPX 구역 처리부를 그대로 돌린다.

본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
"""

from .parser import HwpParser

__all__ = ["HwpParser"]
