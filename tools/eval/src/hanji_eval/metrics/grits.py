# 이 파일은 microsoft/table-transformer의 GriTS 구현 일부를 들여와 고친 것이다.
#   원본: https://github.com/microsoft/table-transformer/blob/16d124f616109746b7785f03085100f1f6247575/src/grits.py
#   (그 파일의 마지막 변경 커밋 ef21069f2a42254bbd049609f7d4bebc25abcf09, 원본 sha256 75c13031…43d7)
#   원 머리: Copyright (C) 2021 Microsoft Corporation. MIT License(전문은 tools/eval/NOTICE).
# 바꾼 점: (1) numpy 배열 대신 파이썬 리스트(같은 float64 셈), (2) PyMuPDF(fitz) Rect 대신 같은 셈의 iou(교집합 넓이 ÷
#   두 상자를 감싸는 상자 넓이), (3) GriTS-Con·Top만 남기고(Loc·HTML 파싱·상자 후처리는 뺐다) 격자는 공통 블록 표에서
#   만든다: 어느 칸도 덮지 않은 자리는 빈 글자의 1×1 칸으로 본다(원본은 0으로 채워 실패한다), (4) 점수는 늘 float으로
#   돌려준다(원본은 0·1을 int로 돌려줄 때가 있다).
"""GriTS-Con(칸 글자 LCS 유사도)·GriTS-Top(칸의 상대 범위 IoU): factored 2D-MSS의 F-score. 짝 표마다(스펙 §7.1)."""

import itertools
from difflib import SequenceMatcher
from typing import NamedTuple

from ..blocks import Table


class GritsScore(NamedTuple):
    fscore: float
    precision: float
    recall: float


def compute_fscore(num_true_positives, num_true, num_positives):
    """
    Compute the f-score or f-measure for a collection of predictions.

    Conventions:
    - precision is 1 when there are no predicted instances
    - recall is 1 when there are no true instances
    - fscore is 0 when recall or precision is 0
    """
    if num_positives > 0:
        precision = num_true_positives / num_positives
    else:
        precision = 1
    if num_true > 0:
        recall = num_true_positives / num_true
    else:
        recall = 1

    if precision + recall > 0:
        fscore = 2 * precision * recall / (precision + recall)
    else:
        fscore = 0

    return fscore, precision, recall


def initialize_DP(sequence1_length, sequence2_length):
    """
    Helper function to initialize dynamic programming data structures.
    """
    scores = [[0.0] * (sequence2_length + 1) for _ in range(sequence1_length + 1)]
    pointers = [[0] * (sequence2_length + 1) for _ in range(sequence1_length + 1)]
    for seq1_idx in range(1, sequence1_length + 1):
        pointers[seq1_idx][0] = -1
    for seq2_idx in range(1, sequence2_length + 1):
        pointers[0][seq2_idx] = 1
    return scores, pointers


def traceback(pointers):
    """
    Dynamic programming traceback to determine the aligned indices
    between the two sequences.

    Traceback convention: -1 = up, 1 = left, 0 = diag up-left
    """
    seq1_idx = len(pointers) - 1
    seq2_idx = len(pointers[0]) - 1
    aligned_sequence1_indices = []
    aligned_sequence2_indices = []
    while not (seq1_idx == 0 and seq2_idx == 0):
        if pointers[seq1_idx][seq2_idx] == -1:
            seq1_idx -= 1
        elif pointers[seq1_idx][seq2_idx] == 1:
            seq2_idx -= 1
        else:
            seq1_idx -= 1
            seq2_idx -= 1
            aligned_sequence1_indices.append(seq1_idx)
            aligned_sequence2_indices.append(seq2_idx)
    return aligned_sequence1_indices[::-1], aligned_sequence2_indices[::-1]


def align_1d(sequence1, sequence2, reward_lookup):
    """
    Dynamic programming alignment between two sequences,
    with memoized rewards.

    Sequences are represented as indices into the rewards lookup table.
    """
    scores, _ = initialize_DP(len(sequence1), len(sequence2))
    for seq1_idx in range(1, len(sequence1) + 1):
        for seq2_idx in range(1, len(sequence2) + 1):
            reward = reward_lookup[sequence1[seq1_idx - 1] + sequence2[seq2_idx - 1]]
            diag_score = scores[seq1_idx - 1][seq2_idx - 1] + reward
            skip_seq2_score = scores[seq1_idx][seq2_idx - 1]
            skip_seq1_score = scores[seq1_idx - 1][seq2_idx]
            scores[seq1_idx][seq2_idx] = max(diag_score, skip_seq1_score, skip_seq2_score)
    return scores[-1][-1]


def align_2d_outer(true_shape, pred_shape, reward_lookup):
    """
    Dynamic programming matrix alignment posed as 2D
    sequence-of-sequences alignment:
    Align two outer sequences whose entries are also sequences,
    where the match reward between the inner sequence entries
    is their 1D sequence alignment score.

    Traceback convention: -1 = up, 1 = left, 0 = diag up-left
    """
    scores, pointers = initialize_DP(true_shape[0], pred_shape[0])
    for row_idx in range(1, true_shape[0] + 1):
        for col_idx in range(1, pred_shape[0] + 1):
            reward = align_1d([(row_idx - 1, tcol) for tcol in range(true_shape[1])],
                              [(col_idx - 1, prow) for prow in range(pred_shape[1])],
                              reward_lookup)
            diag_score = scores[row_idx - 1][col_idx - 1] + reward
            same_row_score = scores[row_idx][col_idx - 1]
            same_col_score = scores[row_idx - 1][col_idx]
            max_score = max(diag_score, same_col_score, same_row_score)
            scores[row_idx][col_idx] = max_score
            if diag_score == max_score:
                pointers[row_idx][col_idx] = 0
            elif same_col_score == max_score:
                pointers[row_idx][col_idx] = -1
            else:
                pointers[row_idx][col_idx] = 1
    aligned_true_indices, aligned_pred_indices = traceback(pointers)
    return aligned_true_indices, aligned_pred_indices, scores[-1][-1]


def factored_2dmss(true_cell_grid, pred_cell_grid, reward_function):
    """
    Factored 2D-MSS: Factored two-dimensional most-similar substructures

    This is a polynomial-time heuristic to computing the 2D-MSS of two matrices,
    which is NP hard.
    """
    true_shape = (len(true_cell_grid), len(true_cell_grid[0]))
    pred_shape = (len(pred_cell_grid), len(pred_cell_grid[0]))
    pre_computed_rewards = {}
    transpose_rewards = {}
    for trow, tcol, prow, pcol in itertools.product(range(true_shape[0]), range(true_shape[1]),
                                                    range(pred_shape[0]), range(pred_shape[1])):
        reward = reward_function(true_cell_grid[trow][tcol], pred_cell_grid[prow][pcol])
        pre_computed_rewards[(trow, tcol, prow, pcol)] = reward
        transpose_rewards[(tcol, trow, pcol, prow)] = reward

    num_pos = pred_shape[0] * pred_shape[1]
    num_true = true_shape[0] * true_shape[1]

    true_row_nums, pred_row_nums, _ = align_2d_outer(true_shape, pred_shape, pre_computed_rewards)
    true_column_nums, pred_column_nums, _ = align_2d_outer(true_shape[::-1], pred_shape[::-1], transpose_rewards)

    positive_match_score = 0
    for true_row_num, pred_row_num in zip(true_row_nums, pred_row_nums):
        for true_column_num, pred_column_num in zip(true_column_nums, pred_column_nums):
            positive_match_score += pre_computed_rewards[(true_row_num, true_column_num, pred_row_num, pred_column_num)]

    return GritsScore(*(float(v) for v in compute_fscore(positive_match_score, num_true, num_pos)))


def lcs_similarity(string1, string2):
    if len(string1) == 0 and len(string2) == 0:
        return 1
    s = SequenceMatcher(None, string1, string2)
    lcs = ''.join([string1[block.a:(block.a + block.size)] for block in s.get_matching_blocks()])
    return 2 * len(lcs) / (len(string1) + len(string2))


def iou(bbox1, bbox2):
    """교집합 넓이 ÷ 두 상자를 감싸는 상자의 넓이(fitz Rect intersect·include_rect와 같은 셈)."""
    x0, y0 = max(bbox1[0], bbox2[0]), max(bbox1[1], bbox2[1])
    x1, y1 = min(bbox1[2], bbox2[2]), min(bbox1[3], bbox2[3])
    intersection = (x1 - x0) * (y1 - y0) if x1 > x0 and y1 > y0 else 0
    union = (max(bbox1[2], bbox2[2]) - min(bbox1[0], bbox2[0])) * (max(bbox1[3], bbox2[3]) - min(bbox1[1], bbox2[1]))
    if union > 0:
        return intersection / union
    return 0


def text_grid(table: Table) -> list[list[str]]:
    grid = [[""] * table.n_cols for _ in range(table.n_rows)]
    for (row, col), text in table.grid().items():
        grid[row][col] = text
    return grid


def relspan_grid(table: Table) -> list[list[list[int]]]:
    """자리마다 그 자리가 속한 칸의 상대 범위 [θ−j, p−i, θ−j+β, p−i+α](병합 없는 칸은 [0, 0, 1, 1])."""
    grid = [[[0, 0, 1, 1] for _ in range(table.n_cols)] for _ in range(table.n_rows)]
    for cell in table.cells:
        for row in range(cell.row, cell.row + cell.rowspan):
            for col in range(cell.col, cell.col + cell.colspan):
                grid[row][col] = [cell.col - col, cell.row - row, cell.col + cell.colspan - col,
                                  cell.row + cell.rowspan - row]
    return grid


def grits_con(pred: Table, truth: Table) -> GritsScore:
    return factored_2dmss(text_grid(truth), text_grid(pred), reward_function=lcs_similarity)


def grits_top(pred: Table, truth: Table) -> GritsScore:
    return factored_2dmss(relspan_grid(truth), relspan_grid(pred), reward_function=iou)
