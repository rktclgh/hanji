# 이 파일은 IBM PubTabNet의 TEDS 구현을 들여와 고친 것이다.
#   원본: https://github.com/ibm-aur-nlp/PubTabNet/blob/8ffde9024bd331f5a61c3c549fdf30dd3c3e43a5/src/metric.py
#   (그 파일의 마지막 변경 커밋 7b03ef8f54f747fa3accf7b9354520a41b30ab40, 원본 sha256 a9aedd31…f9f9)
#   원 머리: Copyright 2020 IBM, Author: peter.zhong@au1.ibm.com. "This is free software; you can redistribute it
#   and/or modify it under the terms of the Apache 2.0 License."
# 바꾼 점(Apache-2.0 §4(b)): (1) GPL distance.levenshtein 대신 rapidfuzz Levenshtein.distance(같은 값),
#   (2) lxml HTML 파싱 대신 공통 블록 표(행·열·병합·글자)에서 table → tbody → tr → td 트리를 바로 만든다(td 내용
#   토큰 = 칸 글자의 글자 하나하나, 원본의 tokenize와 같은 값), (3) 병렬 실행·tqdm·일괄 평가·main을 뺐다,
#   (4) 어느 칸도 덮지 않은 자리는 그 행에 UNCOVERED 노드(1×1, 글자 없음)로 열 차례에 넣는다(칸의 열 자리가 점수에 든다).
"""TEDS·TEDS-S: 1 − 트리 편집 거리 ÷ 큰 쪽 노드 수. 짝 표마다(스펙 §7.1)."""

from collections import deque

from apted import APTED, Config
from apted.helpers import Tree
from rapidfuzz.distance import Levenshtein

from ..blocks import Table

UNCOVERED = "td-uncovered"  # 덮이지 않은 자리의 노드 태그(td와 이름이 달라 바꾸면 1이 든다)


class TableTree(Tree):
    def __init__(self, tag, colspan=None, rowspan=None, content=None, *children):
        self.tag = tag
        self.colspan = colspan
        self.rowspan = rowspan
        self.content = content
        self.children = list(children)

    def bracket(self):
        """Show tree using brackets notation"""
        if self.tag == 'td':
            result = '"tag": %s, "colspan": %d, "rowspan": %d, "text": %s' % \
                     (self.tag, self.colspan, self.rowspan, self.content)
        else:
            result = '"tag": %s' % self.tag
        for child in self.children:
            result += child.bracket()
        return "{{{}}}".format(result)


class CustomConfig(Config):
    @staticmethod
    def maximum(*sequences):
        """Get maximum possible value
        """
        return max(map(len, sequences))

    def normalized_distance(self, *sequences):
        """Get distance from 0 to 1
        """
        return float(Levenshtein.distance(*sequences)) / self.maximum(*sequences)

    def rename(self, node1, node2):
        """Compares attributes of trees"""
        if (node1.tag != node2.tag) or (node1.colspan != node2.colspan) or (node1.rowspan != node2.rowspan):
            return 1.
        if node1.tag == 'td':
            if node1.content or node2.content:
                return self.normalized_distance(node1.content, node2.content)
        return 0.


def table_tree(table: Table, structure_only: bool = False) -> tuple[TableTree, int]:
    """(트리, table 아래 노드 수). 행마다 tr 하나(그 행에서 시작하는 칸이 없어도), 칸은 시작 행의 tr에 열 차례로.
    어느 칸도 덮지 않은 자리는 그 자리 행의 tr에 열 차례로 UNCOVERED 노드(1×1, 글자 없음)를 둔다: 그래야 1×2 표의
    0열 칸과 1열 칸이 같은 트리가 되지 않는다(빽빽한 표는 원본과 같은 트리다)."""
    covered = table.grid()
    holes = [(r, k) for r in range(table.n_rows) for k in range(table.n_cols) if (r, k) not in covered]
    body = TableTree('tbody', None, None, None, *deque())
    for row in range(table.n_rows):
        tr = TableTree('tr', None, None, None, *deque())
        nodes = [(cell.col, TableTree('td', cell.colspan, cell.rowspan, [] if structure_only else list(cell.text),
                                      *deque())) for cell in table.cells if cell.row == row]
        nodes += [(k, TableTree(UNCOVERED, 1, 1, [], *deque())) for r, k in holes if r == row]
        tr.children.extend(node for _, node in sorted(nodes, key=lambda n: n[0]))
        body.children.append(tr)
    root = TableTree('table', None, None, None, body)
    return root, 1 + table.n_rows + len(table.cells) + len(holes)


def teds(pred: Table | None, truth: Table | None, structure_only: bool = False) -> float:
    """짝이 없으면(한쪽 None) 0."""
    if pred is None or truth is None:
        return 0.0
    tree_pred, n_pred = table_tree(pred, structure_only)
    tree_true, n_true = table_tree(truth, structure_only)
    n_nodes = max(n_pred, n_true)
    distance = APTED(tree_pred, tree_true, CustomConfig()).compute_edit_distance()
    return 1.0 - (float(distance) / n_nodes)
