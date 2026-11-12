"""hanji-eval 명령. 표준 출력에는 정해진 꼴의 값만 찍는다(문서 글자 없음)."""

import argparse
import sys
from pathlib import Path

from . import datasets, fetch, guard, workdir

EVAL = Path(__file__).resolve().parents[2]
ROOT = EVAL.parent.parent
DATASETS = EVAL / "datasets.toml"
FETCHED = guard.obj({"command": guard.enum("fetch"), "dataset": guard.enum(*guard.DATASETS), "files": guard.integer()})


def _fetch(args: argparse.Namespace) -> int:
    dataset = datasets.Registry.load(DATASETS).get(args.name)
    dest = workdir.refuse_lab(Path(args.dest) if args.dest else workdir.work_dir() / "data", ROOT)
    fetch.fetch(dataset, dest)
    guard.emit({"command": "fetch", "dataset": dataset.name, "files": len(dataset.files)}, FETCHED)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hanji-eval")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="download a pinned dataset into a new empty folder")
    p.add_argument("name")
    p.add_argument("--dest", default=None, help="parent folder (default <work>/data, never under lab/)")
    p.set_defaults(run=_fetch)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (datasets.RegistryError, fetch.FetchError, workdir.WorkDirError, guard.GuardError) as error:
        print(f"[hanji-eval] error: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
