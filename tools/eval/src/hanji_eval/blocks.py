"""공통 블록 목록(스펙 §7.2): 어댑터가 도구 출력을 이 꼴로 옮기고 지표는 이것만 본다. 메모리 안에서만 쓰고 파일로는
작업 폴더(workdir)에만 쓴다(글자가 들어 있다)."""

from dataclasses import dataclass

KINDS = ("heading", "paragraph", "list_item", "caption", "page_header", "page_footer", "table", "figure", "other")


@dataclass(frozen=True, slots=True)
class Cell:
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    text: str = ""


@dataclass(frozen=True, slots=True)
class Table:
    n_rows: int
    n_cols: int
    cells: tuple[Cell, ...]

    def __post_init__(self) -> None:
        """행·열은 1 이상, 칸은 격자 안(병합 포함), 칸끼리 겹치지 않는다(계약 Table과 같다, TEDS·GriTS가 이 전제를 쓴다).
        덮이지 않은 자리는 받는다(빈 자리)."""
        if self.n_rows < 1 or self.n_cols < 1:
            raise ValueError("a table needs at least one row and one column")
        starts, covered = set(), set()
        for c in self.cells:
            if c.row < 0 or c.col < 0 or c.rowspan < 1 or c.colspan < 1 or c.row + c.rowspan > self.n_rows \
                    or c.col + c.colspan > self.n_cols:
                raise ValueError("a table cell lies outside the grid")
            if (c.row, c.col) in starts:
                raise ValueError("two table cells start at the same position")
            starts.add((c.row, c.col))
            spots = {(r, k) for r in range(c.row, c.row + c.rowspan) for k in range(c.col, c.col + c.colspan)}
            if spots & covered:
                raise ValueError("two table cells overlap")
            covered |= spots

    def layout(self) -> frozenset[tuple[int, int, int, int]]:
        """칸 배치(시작 행·열, 행·열 병합)의 집합."""
        return frozenset((c.row, c.col, c.rowspan, c.colspan) for c in self.cells)

    def grid(self) -> dict[tuple[int, int], str]:
        """병합 칸 글자를 덮인 자리마다 편 격자. 어느 칸도 덮지 않은 자리는 dict에 없다."""
        out: dict[tuple[int, int], str] = {}
        for c in self.cells:
            for r in range(c.row, c.row + c.rowspan):
                for k in range(c.col, c.col + c.colspan):
                    out[(r, k)] = c.text
        return out


@dataclass(frozen=True, slots=True)
class Block:
    doc: str
    order: int
    page: int | None
    kind: str
    text: str
    table: Table | None = None
    confidence: float | None = None
    text_source: str | None = None
    level: int | None = None  # 제목 단계(제목 단계 일치율, 스펙 §7.1)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown block kind: {self.kind}")
        if (self.kind == "table") != (self.table is not None):
            raise ValueError("table must be set if and only if kind == 'table'")


def structure_match(a: Table, b: Table) -> bool:
    """행 수·열 수가 같고 칸 배치 집합이 같다(TEDS-S의 구조 일치와 같은 뜻, 스펙 §7.5)."""
    return a.n_rows == b.n_rows and a.n_cols == b.n_cols and a.layout() == b.layout()
