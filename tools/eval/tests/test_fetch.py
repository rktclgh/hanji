import hashlib
from pathlib import Path

import pytest

from hanji_eval import cli
from hanji_eval import fetch as fetch_module
from hanji_eval.datasets import Registry, RegistryError
from hanji_eval.fetch import FetchError, fetch
from hanji_eval.workdir import WorkDirError


def _served(tmp_path: Path, files: dict[str, bytes], wrong: str | None = None) -> Registry:
    served = tmp_path / "served" / "0123abc"
    served.mkdir(parents=True)
    lines = ["[[dataset]]", 'name = "tiny"', 'version = "1"', 'revision = "0123abc"', "publishable = true",
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
    (tmp_path / "served" / "0123abc" / "b.json").unlink()  # 두 번째 주소에서 끊긴다(sha256 확인 전)
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


def test_fetch_itself_refuses_a_destination_under_lab(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]"})
    (tmp_path / "lab").mkdir()
    for dest in (tmp_path / "lab" / "eval-data", tmp_path / "LAB" / "eval-data"):
        with pytest.raises(WorkDirError):
            fetch(registry.get("tiny"), dest, repo=tmp_path)
    assert list((tmp_path / "lab").iterdir()) == []


def test_fetch_refuses_a_link_where_the_new_folder_should_be(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]"})
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "eval-data").mkdir()
    (tmp_path / "eval-data" / "tiny").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(FetchError, match="destination is a link"):
        fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert list((tmp_path / "elsewhere").iterdir()) == []
    (tmp_path / "lab").mkdir()
    (tmp_path / "eval-data" / "tiny").unlink()
    (tmp_path / "eval-data" / "tiny").symlink_to(tmp_path / "lab")  # lab/ 안을 가리키는 링크
    with pytest.raises((FetchError, WorkDirError)):
        fetch(registry.get("tiny"), tmp_path / "eval-data", repo=tmp_path)
    assert list((tmp_path / "lab").iterdir()) == []


def test_a_network_failure_prints_only_the_error_class(tmp_path, monkeypatch, capsys):
    registry_file = tmp_path / "datasets.toml"
    served = tmp_path / "served" / "0123abc"
    served.mkdir(parents=True)
    registry_file.write_text(
        '[[dataset]]\nname = "tiny"\nversion = "1"\nrevision = "0123abc"\npublishable = true\nlicense = "MIT"\n'
        'license_basis = "LICENSE"\nuse = "synthetic"\nroles = ["diagnostic"]\nsplits = ["all"]\n'
        f'[[dataset.files]]\npath = "a.json"\nsha256 = "{"0" * 64}"\nurl = "{(served / "gone.json").as_uri()}"\n',
        encoding="utf-8")
    monkeypatch.setattr(cli, "DATASETS", registry_file)
    assert cli.main(["fetch", "tiny", "--dest", str(tmp_path / "eval-data")]) == 2
    assert capsys.readouterr().err == "[hanji-eval] error: URLError\n"
    assert not (tmp_path / "eval-data" / "tiny").exists()


class _Endless:
    """끝나지 않는 응답(지어낸 바이트를 계속 준다). 상한이 듣지 않으면 디스크를 채우기 전에 1 MiB에서 테스트를 멈춘다."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.given = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int) -> bytes:
        self.given += min(n, 1024)
        if self.given > 1 << 20:
            raise AssertionError("the download was not cut off")
        return b"x" * min(n, 1024)


def _sized(tmp_path: Path, size: int | None) -> Registry:
    served = tmp_path / "served" / "0123abc"
    lines = ["[[dataset]]", 'name = "tiny"', 'version = "1"', 'revision = "0123abc"', "publishable = true",
             'license = "MIT"', 'license_basis = "LICENSE"', 'use = "synthetic"', 'roles = ["diagnostic"]',
             'splits = ["all"]', "[[dataset.files]]", 'path = "a.bin"', f'sha256 = "{"0" * 64}"',
             f'url = "{(served / "a.bin").as_uri()}"'] + ([f"size = {size}"] if size is not None else [])
    return Registry.from_text("\n".join(lines) + "\n")


def test_a_response_longer_than_the_recorded_size_is_cut_off_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", lambda request, timeout: _Endless(request.full_url))
    with pytest.raises(FetchError, match="more than 4096 bytes"):
        fetch(_sized(tmp_path, 4096).get("tiny"), tmp_path / "eval-data")
    assert not (tmp_path / "eval-data").exists()


def test_without_a_recorded_size_the_default_byte_cap_applies(tmp_path, monkeypatch):
    assert fetch_module.MAX_BYTES == 2 * 1024 ** 3 and fetch_module.DEADLINE == 30 * 60
    monkeypatch.setattr(fetch_module, "MAX_BYTES", 10_000)
    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", lambda request, timeout: _Endless(request.full_url))
    with pytest.raises(FetchError, match="more than 10000 bytes"):
        fetch(_sized(tmp_path, None).get("tiny"), tmp_path / "eval-data")
    assert not (tmp_path / "eval-data").exists()


def test_a_download_past_the_total_deadline_is_removed(tmp_path, monkeypatch):
    ticks = iter(range(0, 10 ** 6, 60))  # 읽을 때마다 60초가 지난 것으로 센다
    monkeypatch.setattr(fetch_module, "_clock", lambda: next(ticks))
    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", lambda request, timeout: _Endless(request.full_url))
    with pytest.raises(FetchError, match="deadline"):
        fetch(_sized(tmp_path, None).get("tiny"), tmp_path / "eval-data")
    assert not (tmp_path / "eval-data").exists()


def test_a_bad_recorded_size_is_refused():
    for bad in ("-1", '"4096"', "true", "1.5"):
        with pytest.raises(RegistryError, match="size"):
            Registry.from_text(
                '[[dataset]]\nname = "tiny"\nversion = "1"\nrevision = "0123abc"\npublishable = true\n'
                'license = "MIT"\nlicense_basis = "LICENSE"\nuse = "synthetic"\nroles = ["diagnostic"]\n'
                f'splits = ["all"]\n[[dataset.files]]\npath = "a.bin"\nsha256 = "{"0" * 64}"\n'
                f'url = "file:///x/0123abc/a.bin"\nsize = {bad}\n')


def test_a_second_fetch_into_the_same_destination_fails_and_leaves_the_first_intact(tmp_path, monkeypatch):
    registry = _served(tmp_path, {"a.json": b"[1]"})
    real = fetch_module.urllib.request.urlopen
    second: list[BaseException] = []

    def during_first(request, timeout):
        if not second:  # 첫 받기가 파일을 연 뒤, 같은 곳으로 두 번째 받기를 시작한다
            try:
                fetch(registry.get("tiny"), tmp_path / "eval-data")
            except FetchError as error:
                second.append(error)
        return real(request, timeout=timeout)

    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", during_first)
    target = fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert len(second) == 1  # 뒤의 받기는 실패하고 앞의 받기가 만든 파일을 지우지 않는다
    assert (target / "sub" / "a.json").read_bytes() == b"[1]"
    assert sorted(p.name for p in (tmp_path / "eval-data").iterdir()) == ["tiny"]  # 잠금 파일은 풀렸다


def test_a_destination_held_by_another_fetch_is_refused_before_anything_is_written(tmp_path):
    registry = _served(tmp_path, {"a.json": b"[1]"})
    (tmp_path / "eval-data").mkdir()
    lock = tmp_path / "eval-data" / ".tiny.fetch-lock"
    lock.write_bytes(b"")  # 다른 받기가 잡았고 아직 아무것도 쓰지 않았다
    with pytest.raises(FetchError, match="another fetch is writing this destination"):
        fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert sorted(p.name for p in (tmp_path / "eval-data").iterdir()) == [".tiny.fetch-lock"]
    lock.unlink()
    assert (fetch(registry.get("tiny"), tmp_path / "eval-data") / "sub" / "a.json").read_bytes() == b"[1]"


def test_a_file_that_this_fetch_did_not_create_is_never_removed(tmp_path, monkeypatch):
    registry = _served(tmp_path, {"a.json": b"[1]", "b.json": b"[2]"})
    real = fetch_module.urllib.request.urlopen

    def foreign_writer(request, timeout):
        if request.full_url.endswith("a.json"):  # 받기 도중 다른 무엇이 b.json을 먼저 만든다
            (tmp_path / "eval-data" / "tiny" / "sub" / "b.json").write_bytes(b"theirs")
        return real(request, timeout=timeout)

    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", foreign_writer)
    with pytest.raises(FileExistsError):
        fetch(registry.get("tiny"), tmp_path / "eval-data")
    assert (tmp_path / "eval-data" / "tiny" / "sub" / "b.json").read_bytes() == b"theirs"
    assert not (tmp_path / "eval-data" / "tiny" / "sub" / "a.json").exists()
    assert not (tmp_path / "eval-data" / ".tiny.fetch-lock").exists()
