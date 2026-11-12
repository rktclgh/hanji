"""hanji-eval 명령. 표준 출력에는 정해진 꼴의 값만 찍는다(문서 글자 없음)."""

import argparse
import datetime
import json
import sys
from pathlib import Path

from . import corpus, datasets, fetch, guard, workdir

EVAL = Path(__file__).resolve().parents[2]
ROOT = EVAL.parent.parent
DATASETS = EVAL / "datasets.toml"
FETCHED = guard.obj({"command": guard.enum("fetch"), "dataset": guard.enum(*guard.DATASETS), "files": guard.integer()})
_SPLITS = guard.obj({"dev": guard.integer(), "holdout": guard.integer()})
SEALED = guard.obj({"command": guard.enum("seal"), "bundles": _SPLITS, "publishers": _SPLITS})


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
    out.write_text(json.dumps(split, ensure_ascii=True, indent=1) + "\n", encoding="utf-8")
    publishers = {k: len(v) for k, v in split["publishers"].items()}
    guard.emit({"command": "seal", "bundles": split["counts"]["bundles"], "publishers": publishers}, SEALED)
    return 0


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
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (datasets.RegistryError, fetch.FetchError, workdir.WorkDirError, guard.GuardError,
            corpus.SealError) as error:
        print(f"[hanji-eval] error: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
