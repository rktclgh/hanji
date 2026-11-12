"""lab/corpus-big manifest 읽기, 짝 묶음, 보류 발행처 봉인(스펙 §5·§6.4). 분할은 manifest(묶음·발행처·sha256)와 PDF 쪽
수만으로 결과를 보기 전에 정해 커밋한다. 발행처 번호 pub-NNN은 manifest 전체 발행처 이름(NFC)을 코드 포인트 차례로
놓은 1부터의 순번이다(이름은 커밋하지 않는다)."""

import json
import math
import random
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import guard

ORIGINALS = ("hwpx", "hwp")
DOC_REF = guard.obj({"doc": guard.pattern(guard.DOC), "sha256": guard.pattern(guard.SHA256)})
SPLIT_SCHEMA = guard.obj({
    "schema": guard.enum("hanji-eval-split/1"),
    "name": guard.enum(*guard.SPLIT_FILES),
    "created": guard.pattern(guard.DATE),
    "seed": guard.integer(),
    "holdout_fraction": guard.number(0, 1),
    "manifest_sha256": guard.pattern(guard.SHA256),
    "largest_publisher": guard.pattern(guard.PUBLISHER),
    "publishers": guard.obj({"dev": guard.array(guard.pattern(guard.PUBLISHER)),
                             "holdout": guard.array(guard.pattern(guard.PUBLISHER))}),
    "bundles": guard.array(guard.obj({
        "bundle": guard.pattern(guard.BUNDLE), "split": guard.enum("dev", "holdout"),
        "publisher": guard.pattern(guard.PUBLISHER), "pages": guard.integer(1), "pdf": DOC_REF,
        "originals": guard.array(DOC_REF, 1),
    })),
    "counts": guard.obj({"bundles": guard.obj({"dev": guard.integer(), "holdout": guard.integer()}),
                         "pages": guard.obj({"dev": guard.integer(), "holdout": guard.integer()})}),
    "resize_log": guard.array(guard.obj({"date": guard.pattern(guard.DATE),
                                         "moved_to_dev": guard.array(guard.pattern(guard.PUBLISHER))})),
})


class SealError(RuntimeError):
    """봉인 규칙 위반(보류 발행처 문서가 개발 입력에 들었거나, 분할과 manifest가 맞지 않는다)."""


@dataclass(frozen=True, slots=True)
class Bundle:
    bundle: str
    publisher: str
    pdf: dict[str, Any]
    originals: tuple[dict[str, Any], ...]


def load_manifest(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def publisher_ids(manifest: list[dict[str, Any]]) -> dict[str, str]:
    names = sorted({unicodedata.normalize("NFC", d["publisher"]) for d in manifest})
    return {name: f"pub-{i:03d}" for i, name in enumerate(names, 1)}


def publisher_of(doc: dict[str, Any], ids: dict[str, str]) -> str:
    return ids[unicodedata.normalize("NFC", doc["publisher"])]


def bundles(manifest: list[dict[str, Any]]) -> list[Bundle]:
    """같은 group에 PDF 하나와 원본(HWPX·HWP) 하나 이상이 있는 묶음. group 번호 차례."""
    ids = publisher_ids(manifest)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in manifest:
        if d.get("group"):
            groups[d["group"]].append(d)
    out = []
    for group in sorted(groups):
        docs = groups[group]
        pdfs = [d for d in docs if d["format"] == "pdf"]
        originals = tuple(sorted((d for d in docs if d["format"] in ORIGINALS),
                                 key=lambda d: (ORIGINALS.index(d["format"]), d["id"])))
        if len(pdfs) != 1 or not originals:
            continue
        publishers = {publisher_of(d, ids) for d in docs}
        if len(publishers) != 1:
            raise SealError(f"{group}: members have different publishers")
        out.append(Bundle(group, publishers.pop(), pdfs[0], originals))
    return out


def holdout_cap(n_bundles: int, fraction: float) -> int:
    """⌊전체 × fraction⌋을 정수로(부동소수 곱이 하나 아래로 떨어지지 않게 소수 표기 그대로 분수로 바꾼다)."""
    return math.floor(n_bundles * Fraction(repr(fraction)))


def make_split(manifest: list[dict[str, Any]], manifest_sha256: str, pages: dict[str, int], created: str,
               seed: int = 0, fraction: float = 0.4) -> dict[str, Any]:
    """가장 큰 발행처(짝 PDF 쪽 수, 같으면 묶음 수, 그다음 번호)는 개발에 고정한다. 나머지 발행처 번호를 정렬해
    random.Random(seed)로 섞고 차례로 보며, 넣어도 보류 묶음 수가 ⌊전체 × fraction⌋을 넘지 않는 발행처만 보류에 넣는다."""
    found = bundles(manifest)
    by_pub: dict[str, list[Bundle]] = defaultdict(list)
    for b in found:
        by_pub[b.publisher].append(b)
    size = {p: (sum(pages[b.pdf["id"]] for b in bs), len(bs)) for p, bs in by_pub.items()}
    largest = min(by_pub, key=lambda p: (-size[p][0], -size[p][1], p))
    others = sorted(p for p in by_pub if p != largest)
    random.Random(seed).shuffle(others)
    cap = holdout_cap(len(found), fraction)
    holdout: list[str] = []
    taken = 0
    for p in others:
        if taken + size[p][1] <= cap:
            holdout.append(p)
            taken += size[p][1]
    sealed = set(holdout)
    rows = []
    for b in found:
        rows.append({
            "bundle": b.bundle, "split": "holdout" if b.publisher in sealed else "dev", "publisher": b.publisher,
            "pages": pages[b.pdf["id"]], "pdf": {"doc": b.pdf["id"], "sha256": b.pdf["sha256"]},
            "originals": [{"doc": d["id"], "sha256": d["sha256"]} for d in b.originals],
        })
    count = {s: [r for r in rows if r["split"] == s] for s in ("dev", "holdout")}
    split = {
        "schema": "hanji-eval-split/1", "name": "hanji-ko-v1", "created": created, "seed": seed,
        "holdout_fraction": fraction, "manifest_sha256": manifest_sha256, "largest_publisher": largest,
        "publishers": {"dev": sorted(set(by_pub) - sealed), "holdout": sorted(sealed)},
        "bundles": rows,
        "counts": {"bundles": {s: len(v) for s, v in count.items()},
                   "pages": {s: sum(r["pages"] for r in v) for s, v in count.items()}},
        "resize_log": [],
    }
    check_split(split)
    return split


def check_split(split: dict[str, Any]) -> None:
    """스키마와 안쪽 일관성: 개발·보류 발행처가 겹치지 않고, 묶음마다 분할이 제 발행처의 목록과 맞고, 가장 큰
    발행처가 개발에 있고, 수가 묶음 목록과 같다. 하나라도 어긋나면 SealError."""
    guard.check(split, SPLIT_SCHEMA)
    dev, held = set(split["publishers"]["dev"]), set(split["publishers"]["holdout"])
    if dev & held or split["largest_publisher"] not in dev:
        raise SealError("split publishers overlap or the largest publisher is not in dev")
    rows = split["bundles"]
    if len({r["bundle"] for r in rows}) != len(rows):
        raise SealError("split lists a bundle twice")
    if sum(r["split"] == "holdout" for r in rows) > holdout_cap(len(rows), split["holdout_fraction"]):
        raise SealError("split holdout has more bundles than the cap")
    for r in rows:
        if r["publisher"] not in (held if r["split"] == "holdout" else dev):
            raise SealError(f"split bundle {r['bundle']} does not follow its publisher")
    for s in ("dev", "holdout"):
        mine = [r for r in rows if r["split"] == s]
        if split["counts"]["bundles"][s] != len(mine) or split["counts"]["pages"][s] != sum(r["pages"] for r in mine):
            raise SealError("split counts differ from its bundles")


def load_split(path: Path) -> dict[str, Any]:
    split = json.loads(path.read_text(encoding="utf-8"))
    check_split(split)
    return split


def dev_run_set(manifest: list[dict[str, Any]], manifest_sha256: str,
                split: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(개발 실행 세트, 봉인한 문서). 봉인 = 보류 발행처의 모든 문서(짝이든 아니든)."""
    if manifest_sha256 != split["manifest_sha256"]:
        raise SealError("manifest does not match the sealed split (publisher numbers would differ)")
    ids = publisher_ids(manifest)
    sealed = set(split["publishers"]["holdout"])
    run = [d for d in manifest if publisher_of(d, ids) not in sealed]
    held = [d for d in manifest if publisher_of(d, ids) in sealed]
    return run, held


def assert_unsealed(docs: list[dict[str, Any]], manifest: list[dict[str, Any]], split: dict[str, Any]) -> None:
    """고르기·장부·메모리 입력에 보류 발행처 문서가 있으면 SealError(문서 번호만 알린다). 발행처로 다시 셈과 따로,
    sha256으로도 대조한다: manifest에서 보류 발행처의 모든 문서(짝이든 아니든)와 분할에 적힌 보류 묶음 문서."""
    ids = publisher_ids(manifest)
    sealed = set(split["publishers"]["holdout"])
    held_sha = {d["sha256"] for d in manifest if publisher_of(d, ids) in sealed}
    held_sha |= {d["sha256"] for r in split["bundles"] if r["split"] == "holdout" for d in [r["pdf"], *r["originals"]]}
    bad = sorted(d["id"] for d in docs if publisher_of(d, ids) in sealed or d["sha256"] in held_sha)
    if bad:
        raise SealError(f"sealed holdout documents in a tuning input: {', '.join(bad[:10])}")
