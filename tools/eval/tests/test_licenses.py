import collections
import glob
import importlib.metadata
import json
from pathlib import Path

import pytest

from hanji_eval import licenses

HERE = Path(__file__).resolve().parent
EVAL = HERE.parent
ROOT = EVAL.parent.parent
TABLE = {e.name: e for e in licenses.load_table(EVAL / "licenses.toml")}
INSTALLED = {licenses.normalize_name(d.metadata["Name"]): d for d in importlib.metadata.distributions()}


def _lock(tmp_path: Path, packages: dict[str, str]) -> Path:
    body = "".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n\n' for n, v in packages.items())
    path = tmp_path / "uv.lock"
    path.write_text("version = 1\n\n" + body, encoding="utf-8")
    return path


def _table(tmp_path: Path, rows: list[tuple[str, str, str]], bundled: list[dict] | None = None) -> Path:
    body = ""
    for n, v, s in rows:
        body += f'[[package]]\nname = "{n}"\nversion = "{v}"\ndeclared = "{s}"\nbasis = "metadata"\n\n'
        for b in bundled or []:
            body += "[[package.bundled]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in b.items()) + "\n"
    path = tmp_path / "licenses.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _site(dist) -> Path:
    return Path(dist.locate_file(""))


def test_the_committed_table_matches_the_eval_lock_and_every_license_is_allowed():
    assert licenses.check(EVAL / "uv.lock", EVAL / "licenses.toml") == []


def test_shared_distributions_have_the_same_version_as_the_root_lock():
    eval_lock = licenses.lock_packages(EVAL / "uv.lock")
    root_lock = licenses.lock_packages(ROOT / "uv.lock")
    shared = eval_lock.keys() & root_lock.keys()
    assert {"pypdfium2", "pillow", "onnxruntime", "pydantic", "hanji-fonts"} <= shared
    assert {n: eval_lock[n] for n in shared} == {n: root_lock[n] for n in shared}


def test_installed_license_expressions_agree_with_the_table():
    checked = 0
    for name, dist in INSTALLED.items():
        expr = dist.metadata.get("License-Expression")
        if name in TABLE and expr:
            assert TABLE[name].declared == expr, name
            checked += 1
    assert checked >= 10


def test_distributions_that_ship_component_notices_record_their_bundled_components():
    for name, dist in INSTALLED.items():
        files = [str(f) for f in dist.files or []]
        nested = [f for f in dist.metadata.get_all("License-File") or [] if "/" in f]  # 하위 폴더의 고지 = 묶인 것
        notices = (any("/sboms/" in f for f in files) or any(f.endswith("ThirdPartyNotices.txt") for f in files)
                   or bool(nested))
        if name in TABLE and notices:
            assert TABLE[name].bundled, name
    assert {"numpy", "pillow", "pypdfium2", "onnxruntime", "pydantic-core", "pyclipper", "hanji-fonts"} <= {
        n for n, e in TABLE.items() if e.bundled}


def test_every_installed_sbom_component_is_recorded():
    for name in ("pillow", "pydantic-core"):
        dist = INSTALLED[name]
        (sbom,) = [f for f in dist.files if "/sboms/" in str(f)]
        components = json.loads(dist.locate_file(sbom).read_text(encoding="utf-8"))["components"]
        named = {b.sbom_name for b in TABLE[name].bundled if b.sbom_name}
        rest = collections.Counter(c["licenses"][0].get("expression") or c["licenses"][0]["license"]["id"]
                                   for c in components if c["name"] not in named)
        assert {c["name"] for c in components} & named == named
        assert rest == collections.Counter({b.spdx: b.sbom_group for b in TABLE[name].bundled if b.sbom_group})


def test_optional_components_are_really_absent_from_the_installed_files():
    checked = 0
    for name, entry in TABLE.items():
        for b in entry.bundled:
            if b.status == "optional-not-shipped" and name in INSTALLED:
                site = _site(INSTALLED[name])
                for pattern in b.absent:
                    assert glob.glob(str(site / pattern), recursive=True) == [], (name, b.component, pattern)
                checked += 1
    assert checked >= 3


def test_a_version_that_differs_from_the_lock_fails(tmp_path):
    lock = _lock(tmp_path, {"alpha": "1.0", "beta": "2.0"})
    table = _table(tmp_path, [("alpha", "1.0", "MIT"), ("beta", "2.1", "MIT")])
    assert licenses.check(lock, table) == ["beta: table version 2.1 != lock version 2.0"]


def test_a_distribution_missing_from_either_side_fails(tmp_path):
    lock = _lock(tmp_path, {"alpha": "1.0", "Beta_Pkg": "2.0"})
    table = _table(tmp_path, [("alpha", "1.0", "MIT"), ("gamma", "3.0", "MIT")])
    assert licenses.check(lock, table) == [
        "beta-pkg: in lock but not in licenses table", "gamma: in licenses table but not in lock",
    ]


@pytest.mark.parametrize("expr", ["GPL-3.0-only", "AGPL-3.0-or-later", "LGPL-2.1-only", "UNKNOWN"])
def test_copyleft_and_unknown_declared_licenses_fail(tmp_path, expr):
    lock = _lock(tmp_path, {"alpha": "1.0"})
    table = _table(tmp_path, [("alpha", "1.0", expr)])
    assert licenses.check(lock, table) == [f"alpha: license {expr} not allowed"]


@pytest.mark.parametrize(("expr", "message"), [
    ("MIT AND (BSD-3-Clause", "unexpected end of SPDX expression: 'MIT AND (BSD-3-Clause'"),
    ("MIT; BSD-3-Clause", "unreadable SPDX expression: 'MIT; BSD-3-Clause'"),
    ("MIT BSD-3-Clause", "trailing tokens in SPDX expression: 'MIT BSD-3-Clause'"),
])
def test_an_unreadable_expression_fails(tmp_path, expr, message):
    lock = _lock(tmp_path, {"alpha": "1.0"})
    table = _table(tmp_path, [("alpha", "1.0", expr)])
    assert licenses.check(lock, table) == [f"alpha: {message}"]


@pytest.mark.parametrize(("expr", "ok"), [
    ("MIT", True),
    ("MIT OR GPL-3.0-only", True),
    ("GPL-3.0-only OR LGPL-3.0-only", False),
    ("MIT AND GPL-3.0-only", False),
    ("MIT AND (Apache-2.0 OR GPL-2.0-only)", True),
    ("(MIT AND GPL-2.0-only) OR BSD-3-Clause", True),
    ("Apache-2.0 WITH LLVM-exception", True),
    ("GPL-3.0-or-later WITH GCC-exception-3.1", False),
    ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", True),
])
def test_or_needs_one_allowed_side_and_and_needs_both(expr, ok):
    assert licenses.allowed(expr) is ok


GOOD = {
    "allowed": {"component": "zlib", "spdx": "Zlib", "status": "allowed", "evidence": "LICENSE section ZLIB"},
    "or-alternative": {"component": "tls", "spdx": "Apache-2.0 OR GPL-2.0-or-later", "status": "or-alternative",
                       "evidence": "notices", "branch": "Apache-2.0"},
    "runtime-exception": {"component": "libgcc", "spdx": "GPL-3.0-or-later WITH GCC-exception-3.1",
                          "status": "runtime-exception", "evidence": "LICENSE.txt"},
    "weak-copyleft-library": {"component": "shim", "spdx": "LGPL-2.1-or-later", "status": "weak-copyleft-library",
                              "evidence": "sbom"},
    "optional-not-shipped": {"component": "quant", "spdx": "GPL-3.0-or-later", "status": "optional-not-shipped",
                             "evidence": "sbom scope optional", "absent": ["pkg/*quant*"]},
    "build-only": {"component": "config.guess", "spdx": "GPL-3.0-or-later", "status": "build-only",
                   "evidence": "build script, not compiled"},
}


@pytest.mark.parametrize("status", licenses.STATUSES)
def test_each_fixed_status_passes_when_its_rule_holds(tmp_path, status):
    assert set(GOOD) == set(licenses.STATUSES)
    lock = _lock(tmp_path, {"alpha": "1.0"})
    assert licenses.check(lock, _table(tmp_path, [("alpha", "1.0", "MIT")], [GOOD[status]])) == []


@pytest.mark.parametrize(("bad", "message"), [
    ({"component": "gpl lib", "spdx": "GPL-3.0-only", "status": "allowed", "evidence": "x"},
     "license GPL-3.0-only not allowed"),
    ({"component": "gpl lib", "spdx": "GPL-2.0-or-later", "status": "weak-copyleft-library", "evidence": "x"},
     "is not a weak copyleft"),
    ({"component": "gpl lib", "spdx": "AGPL-3.0-only", "status": "runtime-exception", "evidence": "x"},
     "is not a copyleft license with a runtime library exception"),
    ({"component": "gpl lib", "spdx": "MIT OR GPL-2.0-only", "status": "or-alternative", "evidence": "x",
      "branch": "GPL-2.0-only"}, "branch GPL-2.0-only is not an allowed alternative"),
    ({"component": "x", "spdx": "MIT", "status": "unknown", "evidence": "x"}, "is not one of the fixed statuses"),
    ({"component": "quant", "spdx": "GPL-3.0-or-later", "status": "optional-not-shipped", "evidence": "x"},
     "needs absent patterns"),
    ({"component": "quant", "spdx": "GPL-3.0-or-later", "status": "build-only", "evidence": ""},
     "component and evidence are required"),
], ids=["unconditional-gpl", "gpl-as-weak", "agpl-without-exception", "copyleft-branch", "unknown",
        "optional-without-evidence", "build-only-without-evidence"])
def test_bundled_rules_reject_copyleft_without_a_fixed_reason(tmp_path, bad, message):
    lock = _lock(tmp_path, {"alpha": "1.0"})
    (problem,) = licenses.check(lock, _table(tmp_path, [("alpha", "1.0", "MIT")], [bad]))
    assert problem.startswith(f"alpha: bundled {bad['component']}: ") and message in problem
