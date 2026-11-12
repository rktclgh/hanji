import hashlib
from pathlib import Path

import pytest

from hanji_eval import cli
from hanji_eval.datasets import Registry
from hanji_eval.fetch import FetchError, fetch


def _served(tmp_path: Path, files: dict[str, bytes], wrong: str | None = None) -> Registry:
    served = tmp_path / "served" / "rev1"
    served.mkdir(parents=True)
    lines = ["[[dataset]]", 'name = "tiny"', 'version = "1"', 'revision = "rev1"', "publishable = true",
             'license = "MIT"', 'license_basis = "LICENSE"', 'use = "synthetic"', 'roles = ["diagnostic"]',
             'splits = ["all"]']
    for name, data in files.items():
        (served / name).write_bytes(data)
        sha = hashlib.sha256(b"other" if name == wrong else data).hexdigest()
        lines += ["[[dataset.files]]", f'path = "sub/{name}"', f'sha256 = "{sha}"',
                  f'url = "{(served / name).as_uri()}"']
    return Registry.from_text("\n".join(lines) + "\n")


def test_fetch_writes_each_file_into_a_new_folder(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]", "b.json": b"[2]"})
    target = fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert target == tmp_path / "eval-data" / "tiny"
    assert (target / "sub" / "a.json").read_bytes() == b"[1]"
    assert (target / "sub" / "b.json").read_bytes() == b"[2]"


def test_a_sha256_mismatch_removes_what_was_fetched_and_fails(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]", "b.json": b"[2]"}, wrong="b.json")
    with pytest.raises(FetchError, match="sha256 mismatch for sub/b.json"):
        fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert not (tmp_path / "eval-data").exists()


def test_an_interrupted_fetch_leaves_nothing_behind(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]", "b.json": b"[2]"})
    (tmp_path / "served" / "rev1" / "b.json").unlink()  # 두 번째 주소에서 끊긴다(sha256 확인 전)
    with pytest.raises(OSError):
        fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert not (tmp_path / "eval-data").exists()


def test_fetch_refuses_a_folder_that_is_not_empty(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]"})
    (tmp_path / "eval-data" / "tiny").mkdir(parents=True)
    (tmp_path / "eval-data" / "tiny" / "old").write_bytes(b"")
    with pytest.raises(FetchError, match="not a new empty folder"):
        fetch(registry.get("tiny"), tmp_path / "eval-data")


def test_local_only_datasets_are_not_fetched(tmp_path, capsys):
    assert cli.main(["fetch", "corpus-big", "--dest", str(tmp_path)]) == 2
    assert capsys.readouterr().err == "[hanji-eval] error: FetchError\n"
    assert list(tmp_path.iterdir()) == []


def test_a_destination_under_lab_is_refused_before_anything_is_written(capsys):
    target = cli.ROOT / "lab" / "eval-data-test"
    assert cli.main(["fetch", "corpus-big", "--dest", str(target)]) == 2
    assert capsys.readouterr().err == "[hanji-eval] error: WorkDirError\n"
    assert not target.exists()
