"""hanji-eval 명령. 표준 출력에는 정해진 꼴의 값만 찍는다(문서 글자 없음)."""

import argparse
import datetime
import json
import sys
from pathlib import Path

from hanji.errors import ModelError

from . import bigrun, corpus, datasets, fetch, guard, provenance, workdir

EVAL = Path(__file__).resolve().parents[2]
ROOT = EVAL.parent.parent
DATASETS = EVAL / "datasets.toml"
FETCHED = guard.obj({"command": guard.enum("fetch"), "dataset": guard.enum(*guard.DATASETS), "files": guard.integer()})
_SPLITS = guard.obj({"dev": guard.integer(), "holdout": guard.integer()})
SEALED = guard.obj({"command": guard.enum("seal"), "bundles": _SPLITS, "publishers": _SPLITS})
RAN = guard.obj({"command": guard.enum("bigrun"), "run": guard.integer(), "sealed": guard.integer(),
                 "extra": guard.integer(), "gate": guard.enum("pass", "fail")})


def _fetch(args: argparse.Namespace) -> int:
    dataset = datasets.Registry.load(DATASETS).get(args.name)
    dest = workdir.refuse_lab(Path(args.dest) if args.dest else workdir.work_dir() / "data", ROOT)
    fetch.fetch(dataset, dest)
    guard.emit({"command": "fetch", "dataset": dataset.name, "files": len(dataset.files)}, FETCHED)
    return 0


def _pdf_pages(path: Path) -> int:
    import pypdfium2

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def _seal(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        print("[seal] error: the split is already sealed (refusing to overwrite)", file=sys.stderr)
        return 2
    manifest_path = Path(args.manifest)
    manifest = corpus.load_manifest(manifest_path)
    pages = {b.pdf["id"]: _pdf_pages(manifest_path.parent / b.pdf["file"]) for b in corpus.bundles(manifest)}
    split = corpus.make_split(manifest, datasets.file_sha256(manifest_path), pages, args.date)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out.open("x", encoding="utf-8") as f:  # 위 검사와 쓰기 사이에 생긴 파일도 덮어쓰지 않는다
            f.write(json.dumps(split, ensure_ascii=True, indent=1) + "\n")
    except FileExistsError:
        print("[seal] error: the split is already sealed (refusing to overwrite)", file=sys.stderr)
        return 2
    publishers = {k: len(v) for k, v in split["publishers"].items()}
    guard.emit({"command": "seal", "bundles": split["counts"]["bundles"], "publishers": publishers}, SEALED)
    return 0


def _bigrun(args: argparse.Namespace) -> int:
    name = args.name or f"{args.date}-bigrun-dev"
    out = workdir.refuse_lab(Path(args.out) if args.out else workdir.work_dir() / "bigrun" / name, ROOT)
    registry = datasets.Registry.load(DATASETS)
    summary = bigrun.run(Path(args.manifest), Path(args.split), out, registry, name, args.date, jobs=args.jobs,
                         timeout=args.timeout, twice=args.twice, extra_dirs=[Path(p) for p in args.extra],
                         allow_no_models=args.allow_no_models)
    c, g = summary["counts"], summary["gate"]
    guard.emit({"command": "bigrun", "run": c["run"], "sealed": c["sealed"], "extra": c["extra"],
                "gate": "pass" if g["passed"] else "fail"}, RAN)
    return 0 if g["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hanji-eval")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="download a pinned dataset into a new empty folder")
    p.add_argument("name")
    p.add_argument("--dest", default=None, help="parent folder (default <work>/data, never under lab/)")
    p.set_defaults(run=_fetch)
    p = sub.add_parser("seal", help="seal the hanji-ko-v1 holdout publishers from the manifest (once)")
    p.add_argument("--manifest", default=str(ROOT / "lab" / "corpus-big" / "manifest.json"))
    p.add_argument("--out", default=str(EVAL / "splits" / "hanji-ko-v1.json"))
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.set_defaults(run=_seal)
    p = sub.add_parser("bigrun", help="run the large corpus (development run set only) document by document")
    p.add_argument("--manifest", default=str(ROOT / "lab" / "corpus-big" / "manifest.json"))
    p.add_argument("--split", default=str(EVAL / "splits" / "hanji-ko-v1.json"))
    p.add_argument("--out", default=None, help="new work folder (default <work>/bigrun/<name>, never under lab/)")
    p.add_argument("--name", default=None)
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--twice", action="store_true", help="run every document twice and compare the hashes")
    p.add_argument("--extra", action="append", default=[], help="extra folder outside hanji-ko (repeatable)")
    p.add_argument("--allow-no-models", action="store_true", help="run even if the OCR or layout models are missing")
    p.set_defaults(run=_bigrun)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (datasets.RegistryError, fetch.FetchError, workdir.WorkDirError, guard.GuardError, corpus.SealError,
            provenance.ProvenanceError, bigrun.BigrunError, ModelError, OSError) as error:  # 네트워크·파일 오류도 이름만
        print(f"[hanji-eval] error: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
