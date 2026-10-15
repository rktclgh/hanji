"""OOXML 출처 열거와 표시(스펙 §9). 블록 조립과 따로, 이야기(본문·머리말 파트·주석 하나·글상자 내용)를 명시적 스택으로
훑어 출처 요소(w:t·w:instrText·w:sym·m:t)를 모두 장부에 올리고, 그때 조상과 필드 상태를 보고 출처마다 표시를 단다:
보통(보이는 글자), 숨김(w:vanish), 범위 밖(고르지 않은 mc 쪽·무시한 요소·필드 명령 구간·w:moveFrom·w:del). 블록 조립은
표시가 보통인 출처만 넣는다. 숨김·범위 밖 출처는 장부에 숨은 출처로 올라 살리지 않고, 블록에 들어가면 불일치다.
처리 이력 재료(Note)도 여기서 센다: alternate_content_skipped·tracked_deletion_skipped(문서), field_unterminated(이야기),
hidden_text·symbol_char·equation_text(문단)."""

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..flowledger import Ledger, ink
from ..zipxml import Prefixes
from .docx.styles import Styles
from .package import MC, choose, ignorable

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
T, INSTR, SYM, MT = f"{W}t", f"{W}instrText", f"{W}sym", f"{M}t"
SOURCES = frozenset({T, INSTR, SYM, MT})
STORY = f"{W}txbxContent"  # 그 안은 이야기 하나(필드 상태가 처음부터)
DELETED = frozenset({f"{W}moveFrom", f"{W}del"})  # 안의 출처는 범위 밖(변경 추적 삭제)
DEL_TEXT = frozenset({f"{W}delText", f"{W}delInstrText"})  # 출처가 아니다(글자 수만 처리 이력에)
NOTE_REFS = frozenset({f"{W}footnoteReference", f"{W}endnoteReference"})
NORMAL, HIDDEN, OUT = "normal", "hidden", "out"
ALTERNATE_SKIPPED = "alternate_content_skipped"
FIELD_UNTERMINATED = "field_unterminated"
TRACKED_DELETION = "tracked_deletion_skipped"
HIDDEN_TEXT = "hidden_text"
SYMBOL_CHAR = "symbol_char"
EQUATION_TEXT = "equation_text"
_PARA = f"{W}p"
_RUNS = frozenset({f"{W}r", f"{M}r"})


def sym_char(element: ET.Element) -> str:
    """w:sym@w:char의 16진 코드 포인트 그대로(기호 글꼴의 사용자 영역 값일 수 있다). 풀 수 없으면 빈 글자."""
    try:
        code = int(element.get(f"{W}char", ""), 16)
    except ValueError:
        return ""
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else ""


def source_text(element: ET.Element) -> str:
    """장부가 세는 출처 글자(블록 글자를 만드는 쪽과 따로 만든다)."""
    return sym_char(element) if element.tag == SYM else "".join(element.itertext())


def part_chars(root: ET.Element) -> int:
    """파트 안 출처 요소의 공백 아닌 글자 합(장부 밖 파트의 처리 이력 숫자: 메모 등)."""
    return sum(ink(source_text(e)) for e in root.iter() if e.tag in SOURCES)


@dataclass(frozen=True, slots=True)
class Note:
    """처리 이력 재료 하나: 구역, 닻(None = 문서 단위), 꼬리표, 사유, 근거 숫자 이름(None = 숫자 없음)과 값."""

    section: int
    anchor: int | None
    tag: str
    reason: str
    name: str | None = None
    value: int = 0


@dataclass(slots=True)
class _Field:
    section: int
    anchor: int
    instr: bool = True  # 명령 구간(separate 전)
    blocked: dict[int, int] = field(default_factory=dict)  # 명령 구간이라 보이지 않게 된 출처 id(요소) → n


@dataclass(slots=True)
class Story:
    """이야기 하나의 필드 스택(문단을 넘어 이어진다)."""

    fields: list[_Field] = field(default_factory=list)


@dataclass(slots=True)
class _Para:
    section: int
    anchor: int
    hidden: int = 0
    symbol: bool = False
    equation: bool = False


@dataclass(frozen=True, slots=True)
class _Ctx:
    out: str | None  # None, "alt"(고르지 않은 mc·무시한 요소), "deleted"
    ignored: frozenset[str]
    process: frozenset[tuple[str, str]]
    para: ET.Element | None
    run: ET.Element | None
    story: Story


def _split(tag: str) -> tuple[str, str]:
    uri, _, local = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
    return uri, local


class Walker:
    """문서 하나의 출처를 표시한다. marks는 id(출처 요소) → 표시, notes는 처리 이력 재료(문서 순서), refs는 범위 안
    각주·미주 참조 (구역, 닻, 참조 요소 이름, w:id)(문서 순서)."""

    def __init__(self, styles: Styles) -> None:
        self.styles = styles
        self.marks: dict[int, str] = {}
        self.notes: list[Note] = []
        self.refs: list[tuple[int, int, str, str]] = []
        self.skipped = 0  # 고르지 않은 mc 쪽·무시한 요소의 출처 글자
        self.deleted = 0  # 변경 추적 삭제(moveFrom·del 안 출처, delText) 글자
        self._paras: dict[int, _Para] = {}
        self._empty = ET.Element(_PARA)

    def walk(self, root: ET.Element, prefixes: Prefixes, ledger: Ledger, section: int, anchor: int, story: Story,
             ancestors: Sequence[ET.Element] = ()) -> None:
        """root 아래 출처를 문서 순서로 장부에 올리고 표시한다(재귀 없음). 글상자 내용은 새 이야기다. ancestors는
        root 위 요소들(위에서 아래로: 파트 뿌리·w:body·본문 층 감싼 요소). 위에서부터 훑은 것처럼 그 mc:Ignorable·
        ProcessContent가 root에도 미치고, 무시한 요소·고르지 않은 mc 쪽·변경 추적 삭제 아래면 root도 범위 밖이다."""
        ignored: frozenset[str] = frozenset()
        process: frozenset[tuple[str, str]] = frozenset()
        out: str | None = None
        path = (*ancestors, root)
        for k, element in enumerate(ancestors):
            more, also = ignorable(element, prefixes)
            ignored, process = ignored | more, process | also
            uri, local = _split(element.tag)
            if out is not None:
                continue
            if uri in ignored and (uri, local) not in process and (uri, "*") not in process:
                out = "alt"
            elif element.tag == f"{MC}AlternateContent" and choose(element, prefixes) is not path[k + 1]:
                out = "alt"
            elif element.tag in DELETED:
                out = "deleted"
        stack: list[tuple[ET.Element, _Ctx] | Story] = [(root, _Ctx(out, ignored, process, None, None, story))]
        while stack:
            item = stack.pop()
            if isinstance(item, Story):
                self.close(item)
                continue
            element, ctx = item
            ignored, process = ignorable(element, prefixes)
            if ignored or process:
                ctx = _Ctx(ctx.out, ctx.ignored | ignored, ctx.process | process, ctx.para, ctx.run, ctx.story)
            uri, local = _split(element.tag)
            if ctx.out is None and uri in ctx.ignored:
                if (uri, local) not in ctx.process and (uri, "*") not in ctx.process:
                    ctx = _Ctx("alt", ctx.ignored, ctx.process, ctx.para, ctx.run, ctx.story)
                stack.extend((child, ctx) for child in reversed(element))  # ProcessContent면 자신만 벗긴다
                continue
            if element.tag == f"{MC}AlternateContent":
                chosen = choose(element, prefixes) if ctx.out is None else None
                for child in reversed(element):
                    out = ctx.out or (None if child is chosen else "alt")
                    stack.append((child, _Ctx(out, ctx.ignored, ctx.process, ctx.para, ctx.run, ctx.story)))
                continue
            if element.tag in DELETED and ctx.out is None:
                ctx = _Ctx("deleted", ctx.ignored, ctx.process, ctx.para, ctx.run, ctx.story)
            elif element.tag in DEL_TEXT:
                if ctx.out != "alt":
                    self.deleted += ink("".join(element.itertext()))
                continue
            elif element.tag == f"{W}fldChar" and ctx.out is None:
                self.field(element, ctx.story, section, anchor)
            elif element.tag in NOTE_REFS and ctx.out is None:
                self.refs.append((section, anchor, element.tag, element.get(f"{W}id", "")))
            elif element.tag == _PARA:
                ctx = _Ctx(ctx.out, ctx.ignored, ctx.process, element, None, ctx.story)
            elif element.tag in _RUNS:
                ctx = _Ctx(ctx.out, ctx.ignored, ctx.process, ctx.para, element, ctx.story)
            elif element.tag == STORY:
                inner = Story()
                stack.append(inner)  # 자식을 다 본 뒤 이 이야기를 닫는다
                ctx = _Ctx(ctx.out, ctx.ignored, ctx.process, ctx.para, ctx.run, inner)
            if element.tag in SOURCES:
                self.source(element, ctx, ledger, section, anchor)
                continue
            stack.extend((child, ctx) for child in reversed(element))

    def field(self, element: ET.Element, story: Story, section: int, anchor: int) -> None:
        """begin → 명령 구간 필드를 넣고, separate → 맨 위를 결과 구간으로, end → 꺼낸다(빈 스택이면 무시)."""
        kind = element.get(f"{W}fldCharType")
        if kind == "begin":
            story.fields.append(_Field(section, anchor))
        elif kind == "separate" and story.fields:
            story.fields[-1].instr = False
        elif kind == "end" and story.fields:
            story.fields.pop()

    def source(self, element: ET.Element, ctx: _Ctx, ledger: Ledger, section: int, anchor: int) -> None:
        n = ink(source_text(element))
        para = ctx.para if ctx.para is not None else self._empty
        blockers = [f for f in ctx.story.fields if f.instr]
        if ctx.out is not None:
            mark = OUT
            if ctx.out == "alt":
                self.skipped += n
            else:
                self.deleted += n
        elif blockers:
            mark = OUT  # 열린 필드 가운데 하나라도 명령 구간이면 보이지 않는다(처리 이력은 끝나지 않은 필드만)
            for f in blockers:
                f.blocked[id(element)] = n
        elif self.styles.run(para, ctx.run)[2]:
            mark = HIDDEN
            self._para(para, section, anchor).hidden += n
        else:
            mark = NORMAL
            if element.tag == SYM:
                self._para(para, section, anchor).symbol = True
            elif element.tag == MT:
                self._para(para, section, anchor).equation = True
        self.marks[id(element)] = mark
        ledger.add(element, n, anchor, id(para), mark != NORMAL)

    def _para(self, p: ET.Element, section: int, anchor: int) -> _Para:
        if id(p) not in self._paras:
            self._paras[id(p)] = _Para(section, anchor)
        return self._paras[id(p)]

    def close(self, story: Story) -> None:
        """이야기 끝: 끝나지 않은 필드가 있으면 field_unterminated(chars = 아직 명령 구간인 필드 때문에 보이지 않게 된
        출처 글자: separate를 지난 필드의 명령은 정상 필드도 숨기므로 세지 않는다)."""
        if story.fields:
            blocked: dict[int, int] = {}
            for f in story.fields:
                if f.instr:
                    blocked.update(f.blocked)
            first = story.fields[0]
            self.notes.append(Note(first.section, first.anchor, "field-unterminated", FIELD_UNTERMINATED, "chars",
                                   sum(blocked.values())))
            story.fields.clear()

    def finish(self) -> None:
        """문단 단위 기록(문서 순서)과 문서 단위 기록을 notes에 더한다."""
        for para in self._paras.values():
            if para.hidden:
                self.notes.append(Note(para.section, para.anchor, "hidden-text", HIDDEN_TEXT, "hidden_chars",
                                       para.hidden))
            if para.symbol:
                self.notes.append(Note(para.section, para.anchor, "symbol", SYMBOL_CHAR))
            if para.equation:
                self.notes.append(Note(para.section, para.anchor, "equation", EQUATION_TEXT))
        if self.skipped:
            self.notes.append(Note(0, None, "alternate-content", ALTERNATE_SKIPPED, "chars", self.skipped))
        if self.deleted:
            self.notes.append(Note(0, None, "tracked-deletion", TRACKED_DELETION, "chars", self.deleted))
