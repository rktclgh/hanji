from pathlib import Path

import pytest

from hanji_eval import workdir

ROOT = Path(__file__).resolve().parents[3]


def test_the_environment_variable_wins(tmp_path):
    assert workdir.work_dir({"HANJI_EVAL_WORK": str(tmp_path / "w")}) == tmp_path / "w"
    assert workdir.work_dir({"HANJI_EVAL_WORK": "~/w"}) == Path.home() / "w"


def test_the_default_work_dir_is_outside_the_repository():
    default = workdir.work_dir({}).resolve()
    assert default.name == "work" and "hanji-eval" in default.parts
    assert not default.is_relative_to(ROOT.resolve())


def test_paths_under_lab_are_refused_and_other_paths_are_resolved(tmp_path):
    (tmp_path / "lab").mkdir()
    for bad in (tmp_path / "lab", tmp_path / "lab" / "eval-work" / "x", tmp_path / "other" / ".." / "lab" / "y"):
        with pytest.raises(workdir.WorkDirError, match="must not be under lab"):
            workdir.refuse_lab(bad, tmp_path)
    assert workdir.refuse_lab(tmp_path / "work" / "x", tmp_path) == (tmp_path / "work" / "x").resolve()
    assert workdir.refuse_lab(tmp_path / "laboratory", tmp_path) == (tmp_path / "laboratory").resolve()


def test_a_link_into_lab_is_refused(tmp_path):
    (tmp_path / "lab").mkdir()
    (tmp_path / "shortcut").symlink_to(tmp_path / "lab")
    with pytest.raises(workdir.WorkDirError):
        workdir.refuse_lab(tmp_path / "shortcut" / "x", tmp_path)


def test_the_main_checkout_lab_counts_when_this_is_a_worktree():
    roots = workdir.lab_roots(ROOT)
    assert ROOT / "lab" in roots and all(r.name == "lab" for r in roots)


def test_another_spelling_of_lab_is_refused(tmp_path):
    # macOS 기본 APFS는 대소문자를 가리지 않는다: LAB/x는 lab/x에 쓴다. 가리는 파일 시스템에서도 막는다(닫힌 쪽)
    (tmp_path / "lab").mkdir()
    for bad in (tmp_path / "LAB" / "x", tmp_path / "Lab", tmp_path / "lAb" / "new" / "deeper"):
        with pytest.raises(workdir.WorkDirError, match="must not be under lab"):
            workdir.refuse_lab(bad, tmp_path)


def test_a_path_that_does_not_exist_yet_is_checked_through_its_existing_parents(tmp_path):
    (tmp_path / "lab").mkdir()
    with pytest.raises(workdir.WorkDirError):
        workdir.refuse_lab(tmp_path / "lab" / "a" / "b" / "c", tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "LAB")  # 대소문자만 다른 링크
    with pytest.raises(workdir.WorkDirError):
        workdir.refuse_lab(tmp_path / "alias" / "new", tmp_path)
    assert workdir.refuse_lab(tmp_path / "w" / "a" / "b", tmp_path) == (tmp_path / "w" / "a" / "b").resolve()


def test_the_repo_constant_is_the_checkout_root():
    assert workdir.REPO == ROOT
