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
