import shutil
import subprocess
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[3]
ENGINE = ROOT / "packages" / "engine"


def _expected() -> set[str]:
    project = tomllib.loads((ENGINE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]

    def expand(items: list[str]) -> list[str]:  # hatchling은 자기 extra(hanji[...])를 그 extra의 의존성으로 푼다
        out: list[str] = []
        for item in items:
            req = Requirement(item)
            out += expand([d for e in sorted(req.extras) for d in extras[e]]) if req.name == "hanji" else [item]
        return out

    deps = {str(Requirement(d)) for d in project["dependencies"]}
    for extra, items in extras.items():
        deps |= {_with_extra(d, extra) for d in expand(items)}
    return deps


def _with_extra(item: str, extra: str) -> str:
    """extra 표시를 붙인다: 항목에 이미 표시가 있으면 hatchling처럼 (표시) and extra로 묶는다."""
    req = Requirement(item)
    marker = f"({req.marker}) and " if req.marker else ""
    req.marker = None
    return str(Requirement(f'{req}; {marker}extra == "{extra}"'))


def test_an_extra_entry_with_its_own_marker_keeps_both_markers():
    assert _with_extra("a>=1", "ocr") == 'a>=1; extra == "ocr"'
    assert _with_extra('a>=1; sys_platform != "darwin"', "ocr") == 'a>=1; sys_platform != "darwin" and extra == "ocr"'


def test_the_engine_wheel_has_no_eval_code_and_its_dependencies_are_unchanged(tmp_path):
    uv = shutil.which("uv")
    assert uv is not None, "uv is needed to build the engine wheel"
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(ENGINE)], check=True,
                   capture_output=True)
    (wheel,) = tmp_path.glob("hanji-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        (meta,) = [n for n in names if n.endswith(".dist-info/METADATA")]
        metadata = Parser().parsestr(archive.read(meta).decode("utf-8"))
    assert names and all(n.startswith(("hanji/", "hanji-")) for n in names)
    assert not [n for n in names if "hanji_eval" in n or n.startswith("tools/")]
    assert {str(Requirement(r)) for r in metadata.get_all("Requires-Dist")} == _expected()
