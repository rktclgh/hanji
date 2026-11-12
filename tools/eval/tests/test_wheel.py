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
        deps |= {str(Requirement(f'{d}; extra == "{extra}"')) for d in expand(items)}
    return deps


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
