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


def _table(tmp_path: Path, rows: list[tuple[str, str, str]], bundled: list[dict] | None = None,
           entry_tags: list[str] | None = None, no_bundled: str | None = None) -> Path:
    body = ""
    for n, v, s in rows:
        body += f'[[package]]\nname = "{n}"\nversion = "{v}"\ndeclared = "{s}"\nbasis = "metadata"\n'
        body += f"no_bundled = {json.dumps(no_bundled)}\n" if no_bundled is not None else ""
        body += (f"wheel_tags = {json.dumps(entry_tags)}\n" if entry_tags is not None else "") + "\n"
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


def _tags(dist) -> list[str]:
    return licenses.wheel_tags(dist.read_text("WHEEL") or "")


def test_every_installed_platform_wheel_was_recorded_for_its_tag():
    # 설치된 플랫폼 휠은 모두(묶인 줄이 없어도) 그 휠에서 적은 것이어야 한다: 적지 않은 플랫폼은 건너뛰지 않고 실패한다
    checked = 0
    for name, entry in TABLE.items():
        if name in INSTALLED and licenses.platform_specific(_tags(INSTALLED[name])):
            assert licenses.recorded_for(entry, _tags(INSTALLED[name])), (name, _tags(INSTALLED[name]))
            assert entry.bundled or entry.no_bundled, name
            checked += 1
    assert checked >= 8


def test_wheels_recorded_without_bundled_components_ship_no_vendored_libraries():
    checked = 0
    for name, entry in TABLE.items():
        if entry.no_bundled and name in INSTALLED:
            files = [str(f) for f in INSTALLED[name].files or []]
            vendored = [f for f in files if f.split("/")[0].endswith((".libs", ".dylibs")) or "/.dylibs/" in f
                        or "/sboms/" in f or f.endswith(".dylib")]
            assert vendored == [], name
            checked += 1
    assert checked >= 2


def test_every_installed_sbom_component_is_recorded():
    for name in ("pillow", "pydantic-core"):
        dist = INSTALLED[name]
        sboms = [f for f in dist.files if "/sboms/" in str(f)]
        assert sboms, name
        components = []
        for sbom in sboms:
            data = json.loads(dist.locate_file(sbom).read_text(encoding="utf-8"))
            own = data.get("metadata", {}).get("component", {}).get("bom-ref")  # auditwheel SBOM은 휠 자신도 적는다
            components += [c for c in data["components"] if own is None or c.get("bom-ref") != own]
        assert licenses.sbom_problems(TABLE[name], _tags(dist), components) == []


def _component(name: str, *licences: str) -> dict:
    return {"name": name, "licenses": [{"expression": lic} for lic in licences]}


def _entry(rows: list[dict], tags: tuple[str, ...] = ("*-macosx_*_arm64",)) -> licenses.Entry:
    return licenses.Entry("alpha", "1.0", "MIT", "metadata", tuple(licenses._bundled(r) for r in rows), tags)


MAC = ["cp312-cp312-macosx_11_0_arm64"]
LINUX = ["cp312-cp312-manylinux_2_17_x86_64", "cp312-cp312-manylinux2014_x86_64"]
ROWS = [
    {"component": "core", "spdx": "MIT", "status": "allowed", "evidence": "x", "sbom_name": "core"},
    {"component": "2 crates", "spdx": "MIT OR Apache-2.0", "status": "allowed", "evidence": "x", "sbom_group": 2},
    {"component": "xau", "spdx": "MIT-open-group", "status": "allowed", "evidence": "x", "sbom_name": "libXau",
     "wheel_tags": ["*-manylinux_*_x86_64"]},
]
SBOM = [_component("core", "MIT"), _component("a", "MIT OR Apache-2.0"), _component("b", "MIT OR Apache-2.0")]


def test_sbom_rows_scoped_to_another_platform_are_left_out():
    tags = ("*-macosx_*_arm64", "*-manylinux_*_x86_64")
    assert licenses.sbom_problems(_entry(ROWS, tags), MAC, SBOM) == []
    assert licenses.sbom_problems(_entry(ROWS, tags), LINUX, SBOM + [{"name": "libXau"}]) == []
    assert licenses.sbom_problems(_entry(ROWS, tags), LINUX, SBOM) == ["alpha: sbom component libXau not found"]


def test_an_sbom_on_a_platform_no_row_was_recorded_for_fails():
    assert licenses.sbom_problems(_entry(ROWS[:2]), LINUX, SBOM) == [
        "alpha: no bundled rows recorded for wheel tags cp312-cp312-manylinux_2_17_x86_64, "
        "cp312-cp312-manylinux2014_x86_64"]
    assert not licenses.recorded_for(_entry(ROWS[:2]), ["cp312-cp312-win_amd64"])
    assert licenses.recorded_for(_entry(ROWS[:2], ()), ["py3-none-any"])


def test_an_sbom_licence_or_count_that_differs_from_the_table_fails():
    assert licenses.sbom_problems(_entry(ROWS[:2]), MAC, [_component("core", "IJG", "BSD-3-Clause")] + SBOM[1:]) == [
        "alpha: sbom component core is IJG AND BSD-3-Clause, table says MIT"]
    assert licenses.sbom_problems(_entry(ROWS[:2]), MAC, SBOM[:2]) == [
        "alpha: sbom licence groups {'MIT OR Apache-2.0': 1} != table {'MIT OR Apache-2.0': 2}"]
    assert licenses.sbom_problems(_entry(ROWS[:2]), MAC, SBOM + [{"name": "c"}]) == [
        "alpha: sbom licence groups {'': 1, 'MIT OR Apache-2.0': 2} != table {'MIT OR Apache-2.0': 2}"]


def test_wheel_tags_are_read_from_the_wheel_file():
    text = "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp312-cp312-manylinux_2_17_x86_64\nTag: cp312-cp312-x\n"
    assert licenses.wheel_tags(text) == ["cp312-cp312-manylinux_2_17_x86_64", "cp312-cp312-x"]
    assert not licenses.platform_specific(["py3-none-any"])
    assert licenses.platform_specific(["py3-none-manylinux_2_17_x86_64"])


def _fixed_parent(pattern: str) -> Path:
    parts = Path(pattern).parts
    return Path(*parts[:next(i for i, p in enumerate(parts) if any(c in p for c in "*?["))])


def test_optional_components_are_really_absent_from_the_installed_files():
    checked = 0
    for name, entry in TABLE.items():
        for b in entry.bundled:
            if b.status == "optional-not-shipped" and name in INSTALLED and licenses.applies(b, _tags(INSTALLED[name])):
                site = _site(INSTALLED[name])
                for pattern in b.absent:
                    assert glob.glob(str(site / pattern), recursive=True) == [], (name, b.component, pattern)
                # 양성 대조: 패턴 하나는 이 플랫폼에 실제로 있는 폴더를 가리켜야 한다(헛짚은 패턴은 늘 비어 통과한다)
                assert any((site / _fixed_parent(p)).is_dir() for p in b.absent), (name, b.component)
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
    ({"component": "mix", "spdx": "MIT AND AGPL-3.0-only OR GPL-3.0-only", "status": "or-alternative",
      "evidence": "x", "branch": "MIT"}, "branch MIT is not an allowed alternative"),
    ({"component": "mix", "spdx": "(AGPL-3.0-only AND MIT) OR GPL-3.0-only", "status": "or-alternative",
      "evidence": "x", "branch": "MIT"}, "branch MIT is not an allowed alternative"),
    ({"component": "mix", "spdx": "MIT AND (Apache-2.0 OR GPL-2.0-only)", "status": "or-alternative",
      "evidence": "x", "branch": "Apache-2.0"}, "or-alternative needs an OR expression and the chosen branch"),
    ({"component": "mix", "spdx": "MIT OR GPL-2.0-only", "status": "or-alternative", "evidence": "x"},
     "or-alternative needs an OR expression and the chosen branch"),
    ({"component": "gpl lib", "spdx": "AGPL-3.0-only WITH GCC-exception-3.1", "status": "runtime-exception",
      "evidence": "x"}, "is not a copyleft license with a runtime library exception"),
    ({"component": "gpl lib", "spdx": "GPL-3.0-only WITH Classpath-exception-2.0", "status": "runtime-exception",
      "evidence": "x"}, "is not a copyleft license with a runtime library exception"),
    ({"component": "quant", "spdx": "UNKNOWN", "status": "build-only", "evidence": "x"},
     "license UNKNOWN is not a known license"),
    ({"component": "quant", "spdx": "NOASSERTION", "status": "optional-not-shipped", "evidence": "x",
      "absent": ["pkg/*quant*"]}, "license NOASSERTION is not a known license"),
    ({"component": "quant", "spdx": "not ; parseable", "status": "optional-not-shipped", "evidence": "x",
      "absent": ["pkg/*quant*"]}, "unreadable SPDX expression"),
    ({"component": "x", "spdx": "MIT", "status": "allowed", "evidence": "x", "wheel_tags": ["*-win_amd64"]},
     "wheel_tags *-win_amd64 not among the wheel_tags the package was recorded from"),
], ids=["unconditional-gpl", "gpl-as-weak", "agpl-without-exception", "copyleft-branch", "unknown",
        "optional-without-evidence", "build-only-without-evidence", "branch-inside-an-and", "branch-inside-a-group",
        "or-only-inside-a-group", "or-without-branch", "agpl-with-gcc-exception", "classpath-on-gpl3",
        "build-only-unknown", "optional-noassertion", "optional-unreadable", "row-platform-not-recorded"])
def test_bundled_rules_reject_copyleft_without_a_fixed_reason(tmp_path, bad, message):
    lock = _lock(tmp_path, {"alpha": "1.0"})
    (problem,) = licenses.check(lock, _table(tmp_path, [("alpha", "1.0", "MIT")], [bad]))
    assert problem.startswith(f"alpha: bundled {bad['component']}: ") and message in problem


@pytest.mark.parametrize(("spdx", "branch"), [
    ("Apache-2.0 OR GPL-2.0-or-later", "Apache-2.0"),
    ("MIT OR Apache-2.0 OR LGPL-2.1-or-later", "MIT"),
    ("(Apache-2.0 AND BSD-3-Clause) OR GPL-2.0-only", "Apache-2.0 AND BSD-3-Clause"),
    ("(GPL-2.0-only OR (MIT AND Zlib))", "(MIT AND Zlib)"),
    ("GPL-2.0-only WITH Classpath-exception-2.0 OR MIT", "MIT"),
])
def test_an_or_branch_must_be_one_whole_top_level_alternative(tmp_path, spdx, branch):
    row = {"component": "mix", "spdx": spdx, "status": "or-alternative", "evidence": "x", "branch": branch}
    assert licenses.check(_lock(tmp_path, {"alpha": "1.0"}), _table(tmp_path, [("alpha", "1.0", "MIT")], [row])) == []


def test_alternatives_split_only_at_the_top_level_or():
    assert licenses.alternatives("MIT AND AGPL-3.0-only OR GPL-3.0-only") == ["MIT AND AGPL-3.0-only", "GPL-3.0-only"]
    assert licenses.alternatives("((MIT OR Zlib))") == ["MIT", "Zlib"]
    assert licenses.alternatives("(MIT OR Zlib) AND CC0-1.0") == ["( MIT OR Zlib ) AND CC0-1.0"]


def test_a_gpl2_classpath_exception_is_a_runtime_exception(tmp_path):
    row = {"component": "rt", "spdx": "GPL-2.0-only WITH Classpath-exception-2.0", "status": "runtime-exception",
           "evidence": "x"}
    assert licenses.check(_lock(tmp_path, {"alpha": "1.0"}), _table(tmp_path, [("alpha", "1.0", "MIT")], [row])) == []


def test_a_platform_scoped_row_passes_when_the_package_was_recorded_for_that_platform(tmp_path):
    row = {"component": "x", "spdx": "MIT", "status": "allowed", "evidence": "x", "wheel_tags": ["*-win_amd64"]}
    table = _table(tmp_path, [("alpha", "1.0", "MIT")], [row], entry_tags=["*-macosx_*_arm64", "*-win_amd64"])
    assert licenses.check(_lock(tmp_path, {"alpha": "1.0"}), table) == []
    bad = _table(tmp_path, [("alpha", "1.0", "MIT")], [], entry_tags=["", 3])
    assert licenses.check(_lock(tmp_path, {"alpha": "1.0"}), bad) == ["alpha: wheel_tags must be non-empty strings"]


def test_a_package_listed_twice_or_without_a_basis_fails(tmp_path):
    lock = _lock(tmp_path, {"alpha": "1.0"})
    assert licenses.check(lock, _table(tmp_path, [("alpha", "1.0", "MIT"), ("alpha", "1.0", "MIT")])) == [
        "alpha: listed more than once"]
    table = tmp_path / "licenses.toml"
    table.write_text('[[package]]\nname = "alpha"\nversion = "1.0"\ndeclared = "MIT"\nbasis = " "\n',
                     encoding="utf-8")
    assert licenses.check(lock, table) == ["alpha: no license basis"]


def test_a_package_locked_at_two_versions_fails(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text('version = 1\n\n[[package]]\nname = "alpha"\nversion = "1.0"\n\n'
                    '[[package]]\nname = "Alpha"\nversion = "2.0"\n', encoding="utf-8")
    problems = licenses.check(lock, _table(tmp_path, [("alpha", "2.0", "MIT")]))
    assert problems == ["alpha: locked more than once (1.0, 2.0)"]


def test_an_unreadable_bundled_row_does_not_hide_the_rows_after_it(tmp_path):
    rows = [{"component": "one", "spdx": "MIT AND (", "status": "allowed", "evidence": "x"},
            {"component": "two", "spdx": "GPL-3.0-only", "status": "allowed", "evidence": "x"}]
    assert licenses.check(_lock(tmp_path, {"alpha": "1.0"}), _table(tmp_path, [("alpha", "1.0", "MIT")], rows)) == [
        "alpha: bundled one: unexpected end of SPDX expression: 'MIT AND ('",
        "alpha: bundled two: license GPL-3.0-only not allowed"]


@pytest.mark.parametrize(("no_bundled", "tags", "bundled", "message"), [
    ("no .libs folder", ["*-manylinux_*_x86_64"], None, None),
    (" ", ["*-manylinux_*_x86_64"], None, "alpha: no_bundled must state its evidence"),
    ("no .libs folder", None, None, "alpha: no_bundled needs the wheel_tags it was recorded from"),
    ("no .libs folder", ["*-manylinux_*_x86_64"],
     [{"component": "c", "spdx": "MIT", "status": "allowed", "evidence": "x"}],
     "alpha: no_bundled together with bundled rows"),
], ids=["ok", "empty", "no-tags", "with-rows"])
def test_a_wheel_without_bundled_components_states_its_evidence_and_tags(tmp_path, no_bundled, tags, bundled,
                                                                         message):
    lock = _lock(tmp_path, {"alpha": "1.0"})
    table = _table(tmp_path, [("alpha", "1.0", "MIT")], bundled, entry_tags=tags, no_bundled=no_bundled)
    assert licenses.check(lock, table) == ([message] if message else [])


def test_numpy_is_recorded_only_for_the_wheels_its_rows_were_read_from():
    # 잠금의 macosx_11_0_arm64 휠은 OpenBLAS·gfortran을 묶는데 그 휠에서 적은 줄이 없다: 설치되면 시험이 실패해야 한다
    numpy = TABLE["numpy"]
    assert licenses.recorded_for(numpy, ["cp312-cp312-macosx_14_0_arm64"])
    assert licenses.recorded_for(numpy, ["cp312-cp312-manylinux_2_27_x86_64", "cp312-cp312-manylinux_2_28_x86_64"])
    assert not licenses.recorded_for(numpy, ["cp312-cp312-macosx_11_0_arm64"])


@pytest.mark.parametrize("where", ["entry", "bundled-wheel_tags", "bundled-absent"])
def test_a_scalar_where_a_list_of_strings_belongs_is_refused_not_split_into_characters(tmp_path, where):
    row = {"component": "one", "spdx": "MIT", "status": "optional-not-shipped", "evidence": "x", "absent": ["*.so"]}
    if where == "bundled-wheel_tags":
        row["wheel_tags"] = "*-manylinux_*_x86_64"
    if where == "bundled-absent":
        row["absent"] = "*.so"
    table = tmp_path / "licenses.toml"
    _table(tmp_path, [("alpha", "1.0", "MIT")], [row])
    if where == "entry":
        table.write_text(table.read_text(encoding="utf-8").replace(
            'basis = "metadata"\n', 'basis = "metadata"\nwheel_tags = "*-manylinux_*_x86_64"\n', 1), encoding="utf-8")
    with pytest.raises(licenses.TableError, match="must be a list of strings"):
        licenses.load_table(table)
    with pytest.raises(licenses.TableError):
        licenses.check(_lock(tmp_path, {"alpha": "1.0"}), table)
