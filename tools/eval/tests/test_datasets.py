import hashlib
from pathlib import Path

import pytest

from hanji_eval.datasets import Registry, RegistryError, Source, verify

EVAL = Path(__file__).resolve().parents[1]
A = "a" * 64
B = "b" * 64


def _entry(name: str, files: list[tuple[str, str]], **extra: str) -> str:
    fields = {"version": '"1"', "license": '"MIT"', "license_basis": '"LICENSE"', "use": '"synthetic"',
              "publishable": "true", "roles": '["selection"]', "splits": '["dev", "holdout"]',
              "sealed_splits": '["holdout"]'} | extra
    lines = ["[[dataset]]", f'name = "{name}"'] + [f"{k} = {v}" for k, v in fields.items()]
    for path, sha in files:
        lines += ["[[dataset.files]]", f'path = "{path}"', f'sha256 = "{sha}"']
    return "\n".join(lines) + "\n"


def test_identity_is_the_sorted_file_hashes_not_the_name_or_paths():
    one = Registry.from_text(_entry("one", [("x.json", A), ("y.json", B)])).get("one")
    two = Registry.from_text(_entry("two", [("other/b.json", B), ("a.json", A)])).get("two")
    assert one.identity == two.identity == hashlib.sha256(f"{A}\n{B}".encode()).hexdigest()


def test_the_same_files_under_another_name_are_rejected_as_an_alias():
    text = _entry("one", [("x.json", A)]) + _entry("two", [("copy.json", A)])
    with pytest.raises(RegistryError, match="two: same files as one"):
        Registry.from_text(text)


@pytest.mark.parametrize(("text", "message"), [
    (_entry("one", [("x.json", A)]) * 2, "dataset listed twice: one"),
    (_entry("Bad Name", [("x.json", A)]), "bad dataset name"),
    (_entry("one", [("../x.json", A)]), "file path must be relative"),
    (_entry("one", [("x.json", "c" * 63)]), "bad sha256"),
    (_entry("one", [("x.json", A)], roles='["publish"]'), "unknown roles"),
    (_entry("one", [("x.json", A)], sealed_splits='["test"]'), "sealed splits"),
    (_entry("one", []), "no files"),
    (_entry("one", [("x.json", A)], license='""'), "license is required"),
    (_entry("one", [("x.json", A)], license_basis='" "'), "license_basis is required"),
    (_entry("one", [("x.json", A)], use='""'), "use is required"),
    (_entry("one", [("x.json", A)], version='""'), "version is required"),
], ids=["twice", "name", "path", "sha", "roles", "sealed", "files", "license", "basis", "use", "version"])
def test_rules_are_checked_when_loading(text, message):
    with pytest.raises(RegistryError, match=message):
        Registry.from_text(text)


def test_a_download_url_must_be_pinned_to_the_revision():
    pinned = _entry("one", [], revision='"abc123"') + '[[dataset.files]]\npath = "x"\nsha256 = "' + A + '"\n'
    with pytest.raises(RegistryError, match="not pinned"):
        Registry.from_text(pinned + 'url = "https://example.invalid/main/x"\n')
    ok = Registry.from_text(pinned + 'url = "https://example.invalid/abc123/x"\n').get("one")
    assert ok.files[0].url == "https://example.invalid/abc123/x"


def test_a_source_names_the_identity_and_a_known_split():
    registry = Registry.from_text(_entry("one", [("x.json", A)]))
    assert registry.source("one", "dev") == Source(registry.get("one").identity, "dev")
    with pytest.raises(RegistryError, match="unknown split"):
        registry.source("one", "test")
    with pytest.raises(RegistryError, match="unknown dataset identity"):
        registry.by_identity("0" * 64)


def test_verify_checks_local_file_hashes(tmp_path):
    (tmp_path / "x.json").write_bytes(b"{}")
    good = hashlib.sha256(b"{}").hexdigest()
    verify(Registry.from_text(_entry("one", [("x.json", good)])).get("one"), tmp_path)
    with pytest.raises(RegistryError, match="sha256 mismatch for x.json"):
        verify(Registry.from_text(_entry("one", [("x.json", A)])).get("one"), tmp_path)
    with pytest.raises(RegistryError, match="missing file y.json"):
        verify(Registry.from_text(_entry("one", [("y.json", A)])).get("one"), tmp_path)


def test_the_committed_table_loads():
    registry = Registry.load(EVAL / "datasets.toml")
    assert "corpus-big" in registry.names()
    assert registry.get("corpus-big").sealed_splits == {"holdout"}
