import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

from hanji_eval import cli, corpus, guard
from hanji_eval.datasets import Registry, file_sha256

EVAL = Path(__file__).resolve().parents[1]


def _doc(i: int, fmt: str, publisher: str, group: str | None) -> dict:
    return {"id": f"{fmt}-{i:04d}", "format": fmt, "file": f"files/{fmt}/{fmt}-{i:04d}.{fmt}",
            "sha256": hashlib.sha256(f"{fmt}{i}".encode()).hexdigest(), "publisher": publisher, "group": group}


SIX = [("가", 1, 2), ("나", 2, 1), ("다", 3, 1), ("라", 4, 1), ("마", 2, 40), ("바", 6, 1)]


def _manifest(spec: list[tuple[str, int, int]] = SIX) -> tuple[list[dict], dict[str, int]]:
    """발행처(가나다 이름은 지어낸 것)마다 (묶음 수, 묶음마다 PDF 쪽 수). 기본은 '마'가 쪽이 가장 많다. 묶음 밖 문서도
    섞는다."""
    docs, pages, n = [], {}, 0
    for publisher, count, size in spec:
        for _ in range(count):
            n += 1
            group = f"grp-{n:04d}"
            pdf = _doc(n, "pdf", publisher, group)
            docs += [pdf, _doc(n, "hwpx", publisher, group)]
            if n % 3 == 0:
                docs.append(_doc(n, "hwp", publisher, group))
            pages[pdf["id"]] = size
        docs.append(_doc(100 + n, "docx", publisher, None))
    docs.append(_doc(200, "hwp", "가", "grp-0999"))  # PDF 없는 묶음은 짝이 아니다
    return docs, pages


def test_the_split_is_deterministic_keeps_the_largest_publisher_in_dev_and_stays_under_the_cap():
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    assert split == corpus.make_split(list(reversed(manifest)), "0" * 64, pages, "2026-11-12")
    ids = corpus.publisher_ids(manifest)
    assert split["largest_publisher"] == ids["마"]
    assert ids["마"] in split["publishers"]["dev"]
    assert split["counts"]["bundles"]["holdout"] <= int(18 * 0.4)
    assert split["counts"]["bundles"] == {"dev": 18 - split["counts"]["bundles"]["holdout"],
                                          "holdout": split["counts"]["bundles"]["holdout"]}
    assert split["counts"]["bundles"]["holdout"] > 0
    assert all(b["split"] == ("holdout" if b["publisher"] in split["publishers"]["holdout"] else "dev")
               for b in split["bundles"])
    assert [b["originals"][0]["doc"].split("-")[0] for b in split["bundles"] if len(b["originals"]) == 2] == \
        ["hwpx"] * 6


def test_every_document_of_a_holdout_publisher_is_sealed_paired_or_not():
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    run, held = corpus.dev_run_set(manifest, "0" * 64, split)
    assert len(run) + len(held) == len(manifest)
    ids = corpus.publisher_ids(manifest)
    sealed = set(split["publishers"]["holdout"])
    assert {corpus.publisher_of(d, ids) for d in held} == sealed
    assert any(d["format"] == "docx" for d in held)
    assert not sealed & {corpus.publisher_of(d, ids) for d in run}


def test_a_publisher_written_in_nfd_is_still_the_same_sealed_publisher():
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    ids = corpus.publisher_ids(manifest)
    name = next(n for n, i in sorted(ids.items()) if i in split["publishers"]["holdout"])
    stray = _doc(300, "docx", unicodedata.normalize("NFD", name), None)
    assert stray["publisher"] != name
    run, held = corpus.dev_run_set(manifest + [stray], "0" * 64, split)
    assert stray in held and stray not in run
    assert corpus.publisher_ids(manifest + [stray]) == ids


def test_a_sealed_document_in_a_tuning_input_fails():
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    run, held = corpus.dev_run_set(manifest, "0" * 64, split)
    corpus.assert_unsealed(run, manifest, split)
    with pytest.raises(corpus.SealError, match=held[0]["id"]):
        corpus.assert_unsealed(run + held[:1], manifest, split)


def test_a_different_manifest_is_refused():
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    with pytest.raises(corpus.SealError, match="does not match the sealed split"):
        corpus.dev_run_set(manifest, "1" * 64, split)


def test_seal_writes_once_and_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "split.json"
    out.write_text("{}", encoding="utf-8")
    assert cli.main(["seal", "--manifest", str(tmp_path / "m.json"), "--out", str(out)]) == 2
    assert "already sealed" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "{}"


def test_the_committed_split_is_well_formed_and_registered():
    path = EVAL / "splits" / "hanji-ko-v1.json"
    split = corpus.load_split(path)
    text = path.read_text(encoding="utf-8")
    assert text.isascii()
    registry = Registry.load(EVAL / "datasets.toml")
    files = {f.path: f.sha256 for f in registry.get("hanji-ko-v1").files}
    assert files["tools/eval/splits/hanji-ko-v1.json"] == file_sha256(path)
    assert files["lab/corpus-big/manifest.json"] == split["manifest_sha256"]
    assert split["counts"]["bundles"] == dict(Counter(b["split"] for b in split["bundles"]))
    assert split["counts"] == {"bundles": {"dev": 102, "holdout": 68}, "pages": {"dev": 714, "holdout": 280}}
    assert split["largest_publisher"] in split["publishers"]["dev"]
    assert not set(split["publishers"]["dev"]) & set(split["publishers"]["holdout"])
    guard.check(json.loads(text), corpus.SPLIT_SCHEMA)


def test_the_holdout_follows_the_seeded_shuffle_and_skips_publishers_that_overflow():
    # random.Random(0).shuffle(["pub-001", "pub-002", "pub-003", "pub-004", "pub-006"]) 차례(파이썬 3.12에서 손으로
    # 뽑아 적음): pub-003, pub-002, pub-001, pub-006, pub-004
    order = ["pub-003", "pub-002", "pub-001", "pub-006", "pub-004"]
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    assert split["largest_publisher"] == "pub-005"
    assert split["publishers"]["holdout"] == ["pub-001", "pub-002", "pub-003"]  # 3 + 2 + 1 = 6 ≤ ⌊18 × 0.4⌋ = 7
    # 묶음 수 4·4·2·1·1(차례대로)이고 상한 7: pub-002는 넘쳐 건너뛰고 pub-001·pub-006이 뒤에서 들어온다
    spec = [("가", 2, 1), ("나", 4, 1), ("다", 4, 1), ("라", 1, 1), ("마", 6, 40), ("바", 1, 1)]
    manifest, pages = _manifest(spec)
    skip = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    assert order[:2] == ["pub-003", "pub-002"]
    assert skip["publishers"]["holdout"] == ["pub-001", "pub-003", "pub-006"]
    assert skip["counts"]["bundles"]["holdout"] == 7


@pytest.mark.parametrize("change", ["emptied", "overlap", "moved_bundle", "counts"])
def test_an_edited_split_is_refused(tmp_path, change):
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    if change == "emptied":
        split["publishers"]["dev"] += split["publishers"]["holdout"]
        split["publishers"]["holdout"] = []
    elif change == "overlap":
        split["publishers"]["dev"].append(split["publishers"]["holdout"][0])
    elif change == "moved_bundle":
        next(b for b in split["bundles"] if b["split"] == "holdout")["split"] = "dev"
    else:
        split["counts"]["pages"]["dev"] += 1
    path = tmp_path / "split.json"
    path.write_text(json.dumps(split), encoding="utf-8")
    with pytest.raises(corpus.SealError):
        corpus.load_split(path)


def test_a_copy_of_a_holdout_document_under_another_id_is_still_sealed():
    manifest, pages = _manifest()
    split = corpus.make_split(manifest, "0" * 64, pages, "2026-11-12")
    run, held = corpus.dev_run_set(manifest, "0" * 64, split)
    held_pdf = next(d for d in held if d["format"] == "pdf")
    copy = dict(run[0], id="pdf-0777", sha256=held_pdf["sha256"])  # 개발 발행처로 적힌 보류 문서 사본
    with pytest.raises(corpus.SealError, match="pdf-0777"):
        corpus.assert_unsealed(run + [copy], manifest, split)
