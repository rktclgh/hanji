import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from hanji.errors import ModelError

from hanji_eval import bigrun, cli, corpus, guard, provenance
from hanji_eval.datasets import Registry, RegistryError

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "engine" / "tests" / "fixtures"
SOURCES = {
    "pdf-0001": "pdf/inputs/table.pdf", "hwpx-0001": "hwpx/inputs/table.hwpx", "hwp-0001": "hwp/inputs/table.hwp",
    "docx-0001": "ooxml/inputs/basic.docx", "pdf-0002": "pdf/inputs/report.pdf",
    "hwpx-0002": "hwpx/inputs/basic.hwpx", "docx-0002": "ooxml/inputs/table.docx",
}
GROUPS = {"pdf-0001": "grp-0001", "hwpx-0001": "grp-0001", "hwp-0001": "grp-0001", "pdf-0002": "grp-0002",
          "hwpx-0002": "grp-0002"}
PUBLISHERS = {"pdf-0002": "나", "hwpx-0002": "나", "docx-0002": "나"}  # 나머지는 '가'(지어낸 발행처)


def _corpus(tmp_path: Path) -> tuple[Path, Path, Registry]:
    base = tmp_path / "corpus"
    (base / "files").mkdir(parents=True)
    manifest = []
    for doc in [*SOURCES, "pptx-0001"]:
        fmt = doc.split("-")[0]
        target = base / "files" / f"{doc}.{fmt}"
        if doc in SOURCES:
            shutil.copyfile(FIXTURES / SOURCES[doc], target)
        else:
            target.write_bytes(b"not a zip file")  # 읽지 못하는 문서 → parse_error
        manifest.append({"id": doc, "format": fmt, "file": f"files/{doc}.{fmt}",
                         "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                         "publisher": PUBLISHERS.get(doc, "가"), "group": GROUPS.get(doc)})
    path = base / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    split = corpus.make_split(manifest, sha, {"pdf-0001": 5, "pdf-0002": 1}, "2026-11-12", fraction=1.0)
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps(split), encoding="utf-8")
    return path, split_path, _registry(sha, hashlib.sha256(split_path.read_bytes()).hexdigest())


def _registry(manifest_sha: str, split_sha: str) -> Registry:
    head = 'version = "1"\nlicense = "MIT"\nlicense_basis = "LICENSE"\nuse = "synthetic"\npublishable = true\n'
    return Registry.from_text(
        f'[[dataset]]\nname = "corpus-big"\n{head}roles = ["bigrun", "selection"]\nsplits = ["dev", "holdout"]\n'
        f'sealed_splits = ["holdout"]\n[[dataset.files]]\npath = "corpus/manifest.json"\nsha256 = "{manifest_sha}"\n'
        f'[[dataset]]\nname = "hanji-ko-v1"\n{head}roles = ["selection"]\nsplits = ["dev", "holdout"]\n'
        f'sealed_splits = ["holdout"]\n[[dataset.files]]\npath = "corpus/manifest.json"\nsha256 = "{manifest_sha}"\n'
        f'[[dataset.files]]\npath = "split.json"\nsha256 = "{split_sha}"\n')


def _run(manifest: Path, split: Path, out: Path, registry: Registry, **kw) -> dict:
    return bigrun.run(manifest, split, out, registry, "synthetic", "2026-11-12", allow_no_models=True, **kw)


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bigrun")
    manifest, split, registry = _corpus(tmp)
    summary = _run(manifest, split, tmp / "out", registry, jobs=2, timeout=120)
    return tmp / "out", summary


def test_only_the_development_run_set_is_run_and_the_rest_is_counted_as_sealed(ran):
    out, summary = ran
    assert summary["counts"] == {"manifest": 8, "sealed": 3, "run": 5, "extra": 0, "extra_skipped": 0}
    docs = [json.loads(line) for line in (out / "docs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sorted(d["doc"] for d in docs) == ["docx-0001", "hwp-0001", "hwpx-0001", "pdf-0001", "pptx-0001"]
    assert summary["dev"]["status"] == {"docx": {"created": 1}, "hwp": {"created": 1}, "hwpx": {"created": 1},
                                        "pdf": {"created": 1}, "pptx": {"parse_error": 1}}
    assert summary["gate"] == {"status_complete": True, "native_ledger_clean": True, "pdf_coverage_clean": True,
                               "passed": True}
    assert summary["flagged"] == [{"doc": "pptx-0001", "format": "pptx", "status": "parse_error", "why": ["failed"]}]
    assert summary["dev"]["errors"] == {"ParseError": 1}
    assert summary["env"]["machine"]["threads"]["HANJI_ORT_THREADS"] >= 1
    assert summary["env"]["machine"]["tools"][0]["tool"] == "hanji" and summary["run"]["dirty"] in (True, False)
    bounds = summary["dev"]["bounds"]
    assert bounds["all"]["docs"] == 5 and bounds["pptx"]["failure_upper"] == 1.0
    assert bounds["hwpx"]["coverage_mismatch_upper"] == pytest.approx(0.95)  # 0/1, 1 − 0.05^(1/1)


def test_each_link_of_a_run_bundle_is_compared_and_outputs_carry_no_text(ran):
    out, summary = ran
    pairs = [json.loads(line) for line in (out / "pairs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sorted((p["original"], p["pdf"]) for p in pairs) == [("hwp-0001", "pdf-0001"), ("hwpx-0001", "pdf-0001")]
    assert all(p["status"] in ("measured", "excluded_not_same_text") and p["hangul_original"] > 0 for p in pairs)
    assert summary["pairs"]["links"] == 2
    for name in ("docs.jsonl", "pairs.jsonl", "summary.json"):
        assert (out / name).read_text(encoding="utf-8").isascii()
    assert (out / "summary.md").read_text(encoding="utf-8") == bigrun.render_md(summary)
    assert bigrun.CP_NOTE in bigrun.render_md(summary).splitlines() and "근사" in bigrun.CP_NOTE  # 스펙 §7.6
    md = bigrun.render_md(summary)
    assert not re.search(r"True|False|[{}']", md) and "통과 예" in md and "pptx | parse_error 1 |" in md  # 평문 값
    guard.check(json.loads((out / "summary.json").read_text(encoding="utf-8")), bigrun.SUMMARY)


def test_a_holdout_document_in_the_run_set_fails_before_anything_runs(tmp_path, monkeypatch):
    manifest, split, registry = _corpus(tmp_path)
    everything = corpus.load_manifest(manifest)
    monkeypatch.setattr(corpus, "dev_run_set", lambda m, sha, s: (everything, []))
    with pytest.raises(corpus.SealError, match="sealed holdout documents"):
        _run(manifest, split, tmp_path / "out", registry)
    assert not (tmp_path / "out").exists()


def test_a_manifest_that_differs_from_the_table_is_refused(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(RegistryError, match="manifest sha256 differs from datasets.toml"):
        _run(manifest, split, tmp_path / "out", registry)


def test_a_split_that_differs_from_the_table_is_refused_even_when_it_is_well_formed(tmp_path):
    manifest, split_path, registry = _corpus(tmp_path)
    split = json.loads(split_path.read_text(encoding="utf-8"))
    split["publishers"]["dev"] += split["publishers"]["holdout"]  # 보류 목록을 비운 분할
    split["publishers"]["holdout"] = []
    for b in split["bundles"]:
        b["split"] = "dev"
    split["counts"] = {"bundles": {"dev": 2, "holdout": 0}, "pages": {"dev": 6, "holdout": 0}}
    corpus.check_split(split)
    split_path.write_text(json.dumps(split), encoding="utf-8")
    with pytest.raises(RegistryError, match="split sha256 differs from datasets.toml"):
        _run(manifest, split_path, tmp_path / "out", registry)
    assert not (tmp_path / "out").exists()


NEVER = [sys.executable, "-c", "raise SystemExit('a worker must not start')"]


def _relocated(tmp_path: Path, manifest: Path) -> Path:
    """sha256이 같은 manifest를 다른 폴더로 옮기고 파일은 그대로 베낀다(리뷰 탐침 A1의 바탕)."""
    shutil.copytree(manifest.parent, tmp_path / "copy")
    return tmp_path / "copy" / "manifest.json"


def test_sealed_bytes_under_development_file_names_are_refused_before_anything_runs(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    moved = _relocated(tmp_path, manifest)
    for dev, held in {"pdf-0001": "pdf-0002", "hwpx-0001": "hwpx-0002", "docx-0001": "docx-0002"}.items():
        fmt = dev.split("-")[0]
        shutil.copyfile(manifest.parent / "files" / f"{held}.{fmt}", moved.parent / "files" / f"{dev}.{fmt}")
    with pytest.raises(corpus.SealError, match=r"^run set files hold sealed documents: docx-0001, hwpx-0001, "
                                               r"pdf-0001$"):
        _run(moved, split, tmp_path / "out", registry, worker=NEVER)
    assert not (tmp_path / "out").exists()


def test_a_changed_or_missing_run_set_file_is_refused_before_anything_runs(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    (manifest.parent / "files" / "hwp-0001.hwp").write_bytes(b"truncated")
    (manifest.parent / "files" / "pptx-0001.pptx").unlink()
    with pytest.raises(RegistryError, match=r"^run set files differ from the manifest: hwp-0001, pptx-0001$"):
        _run(manifest, split, tmp_path / "out", registry, worker=NEVER)
    assert not (tmp_path / "out").exists()


def test_a_run_set_file_that_resolves_outside_the_manifest_folder_is_refused(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    moved = _relocated(tmp_path, manifest)
    link = moved.parent / "files" / "docx-0001.docx"
    link.unlink()
    link.symlink_to(manifest.parent / "files" / "docx-0001.docx")  # 바이트는 같지만 manifest 폴더 밖
    with pytest.raises(RegistryError, match=r"^run set files differ from the manifest: docx-0001$"):
        _run(moved, split, tmp_path / "out", registry, worker=NEVER)
    assert not (tmp_path / "out").exists()


def test_an_extra_folder_with_a_copy_of_a_sealed_document_is_refused(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    shutil.copyfile(tmp_path / "corpus" / "files" / "docx-0002.docx", extra / "renamed.docx")
    with pytest.raises(corpus.SealError, match="1 copies of sealed holdout documents"):
        _run(manifest, split, tmp_path / "out", registry, extra_dirs=[extra])
    assert not (tmp_path / "out").exists()


def test_extra_files_already_in_the_run_set_are_skipped_and_extras_stay_out_of_the_summary(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    shutil.copyfile(tmp_path / "corpus" / "files" / "docx-0001.docx", extra / "same.docx")
    shutil.copyfile(FIXTURES / "ooxml" / "inputs" / "headings.docx", extra / "new.docx")
    summary = _run(manifest, split, tmp_path / "out", registry, extra_dirs=[extra])
    assert (summary["counts"]["extra"], summary["counts"]["extra_skipped"]) == (1, 1)
    assert summary["dev"]["status"]["docx"] == {"created": 1}
    docs = [json.loads(line) for line in (tmp_path / "out" / "docs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["set"] for d in docs if d["doc"].startswith("extra-")] == ["extra"]


def test_the_work_folder_must_be_new_and_never_under_lab(tmp_path, capsys):
    manifest, split, registry = _corpus(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "old.txt").write_text("x", encoding="utf-8")
    with pytest.raises(bigrun.BigrunError, match="new or empty"):
        _run(manifest, split, tmp_path / "out", registry)
    target = ROOT / "lab" / "eval-work-test"
    assert cli.main(["bigrun", "--manifest", str(manifest), "--out", str(target)]) == 2
    assert capsys.readouterr().err == "[hanji-eval] error: WorkDirError\n" and not target.exists()


def test_missing_models_stop_the_run_unless_explicitly_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HANJI_MODEL_DIR", str(tmp_path / "no-models"))
    monkeypatch.setenv("HANJI_CACHE_DIR", str(tmp_path / "no-cache"))
    manifest, split, registry = _corpus(tmp_path)
    with pytest.raises(ModelError):
        bigrun.run(manifest, split, tmp_path / "out", registry, "synthetic", "2026-11-12")
    assert bigrun.model_shas(allow_missing=True) == ([], sorted(bigrun.models.names()))


def test_a_hanging_worker_times_out_and_a_dying_worker_is_a_crash(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-")
    slow = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 0.5, False, worker=[sys.executable, "-c",
                                                                              "import time; time.sleep(5)"])
    dead = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 30, False, worker=[sys.executable, "-c",
                                                                             "import os; os._exit(3)"])
    assert (slow["status"], dead["status"]) == ("timeout", "crashed")
    assert slow["error"] is None and dead["tree_sha256"] is None


def test_noise_before_the_json_line_is_ignored_and_never_written(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-")
    line = json.dumps({"status": "parse_error", "error": "ParseError", "seconds": 0.1, "rss_before": None,
                       "rss_peak": None})
    noisy = f"print('\\uac00\\ub098 synthetic noise line'); print({line!r})"
    record = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 30, False, worker=[sys.executable, "-c", noisy])
    assert (record["status"], record["error"]) == ("parse_error", "ParseError")
    assert "noise" not in json.dumps(record) and "\uac00" not in json.dumps(record, ensure_ascii=False)
    broken = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 30, False,
                            worker=[sys.executable, "-c", "print('{\"status\":'); print('not json')"])
    assert broken["status"] == "crashed"


def test_a_missing_file_is_a_parse_error_not_a_harness_crash(tmp_path):
    record = bigrun.run_doc("pdf-0009", "pdf", tmp_path / "missing.pdf", "dev", 60, False)
    assert (record["status"], record["error"], record["bytes"]) == ("parse_error", "FileNotFoundError", 0)


def test_twice_compares_the_hashes_of_two_runs():
    record = bigrun.run_doc("hwpx-0001", "hwpx", FIXTURES / "hwpx" / "inputs" / "table.hwpx", "dev", 60, True)
    assert record["status"] == "created" and record["twice_same"] is True
    assert record["rss_extra_bytes"] is None or record["rss_extra_bytes"] >= 0


def _rec(doc: str, fmt: str, cov: int = 0, un: int = 0, status: str = "created") -> dict:
    return {"doc": doc, "format": fmt, "set": "dev", "status": status, "error": None, "seconds": 1.0, "bytes": 10,
            "pages": None, "rss_peak_bytes": None, "rss_extra_bytes": None, "kinds": {}, "reasons": {},
            "coverage_mismatch": cov, "unassigned_text": un, "tree_sha256": None, "history_sha256": None,
            "assets_sha256": None, "distribution": False, "twice_same": None}


def _summary(records: list[dict], manifest: int, sealed: int, expected: set[str] | None = None) -> dict:
    env = {"machine": guard.machine_env({}) | {"tools": [], "datasets": [], "split": None}, "jobs": 1,
           "timeout_s": 1.0, "twice": False}
    run = {"name": "synthetic", "date": "2026-11-12", "harness_commit": "a" * 40, "engine_commit": "a" * 40,
           "dirty": False, "manifest_sha256": "0" * 64, "split_sha256": "1" * 64}
    counts = {"manifest": manifest, "sealed": sealed, "run": len(records), "extra": 0, "extra_skipped": 0}
    want = {r["doc"] for r in records} if expected is None else expected
    return bigrun.summarize(run, env, [{"dataset": "2" * 64, "split": "dev"}], counts, records, want, [], False)


def test_the_gate_fails_on_native_ledger_or_pdf_coverage_and_lists_only_those_documents():
    clean = _summary([_rec("hwpx-0001", "hwpx"), _rec("pdf-0001", "pdf", un=3)], 3, 1)
    assert clean["gate"]["passed"] and clean["flagged"] == []
    native = _summary([_rec("hwpx-0001", "hwpx", un=1), _rec("pdf-0001", "pdf")], 2, 0)
    assert native["gate"]["native_ledger_clean"] is False and native["gate"]["passed"] is False
    assert native["flagged"] == [{"doc": "hwpx-0001", "format": "hwpx", "status": "created",
                                  "why": ["unassigned_text"]}]
    pdf = _summary([_rec("pdf-0001", "pdf", cov=2)], 1, 0)
    assert pdf["gate"]["pdf_coverage_clean"] is False and pdf["dev"]["ledger"]["pdf"]["coverage_mismatch"] == 2
    missing = _summary([_rec("pdf-0001", "pdf")], 3, 1)
    assert missing["gate"]["status_complete"] is False
    twice = _summary([_rec("pdf-0001", "pdf"), _rec("pdf-0001", "pdf")], 3, 1)
    assert twice["gate"]["status_complete"] is False
    other = _summary([_rec("pdf-0001", "pdf"), _rec("pdf-0002", "pdf")], 3, 1, {"pdf-0001", "pdf-0003"})
    assert other["gate"]["status_complete"] is False


@pytest.mark.parametrize(("numbers", "distribution", "status"), [
    ({"hangul_original": 100, "hangul_pdf": 100, "common": 95, "union": 105, "set_common": 9, "set_union": 10},
     False, "measured"),
    ({"hangul_original": 130, "hangul_pdf": 100, "common": 100, "union": 130, "set_common": 9, "set_union": 9},
     False, "excluded_not_same_text"),
    ({"hangul_original": 100, "hangul_pdf": 100, "common": 100, "union": 100, "set_common": 8, "set_union": 10},
     False, "excluded_not_same_text"),
    ({"hangul_original": 100, "hangul_pdf": 100, "common": 80, "union": 120, "set_common": 9, "set_union": 9},
     False, "measured"),
    ({"hangul_original": 100, "hangul_pdf": 100, "common": 100, "union": 100, "set_common": 9, "set_union": 9},
     True, "excluded_distribution"),
    (None, True, "excluded_distribution"),
    (None, False, "excluded_failed"),
], ids=["same", "ratio", "set", "set-not-multiset", "distribution", "distribution-unrun", "failed"])
def test_pair_rule(numbers, distribution, status):
    original = _rec("hwpx-0001", "hwpx") | {"distribution": distribution}
    record = bigrun.pair_record("grp-0001", original, _rec("pdf-0001", "pdf"), numbers)
    assert record["status"] == status
    if numbers and status == "measured" and numbers["common"] == 95:
        assert (record["recall"], record["precision"], record["ratio"]) == (0.95, 0.95, 1.0)
    guard.check(record, bigrun.PAIR_RECORD)


@pytest.mark.parametrize("output", ["null", "3", "{}", '{"status": "created"}'])
def test_output_outside_the_worker_protocol_is_a_crash_not_a_harness_failure(tmp_path, output):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-")
    record = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 30, False, worker=[sys.executable, "-c",
                                                                             f"print({output!r})"])
    assert (record["status"], record["error"]) == ("crashed", "WorkerProtocolError")


def test_unknown_block_kinds_reasons_and_errors_are_counted_as_other(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-")
    created = {"status": "created", "error": None, "seconds": 0.1, "rss_before": 1, "rss_peak": 3, "pages": 1,
               "kinds": {"paragraph": 2, "sidebar": 1}, "reasons": {"brand_new_reason": 2, "shape_text": 1},
               "tree_sha256": "a" * 64, "history_sha256": "b" * 64, "assets_sha256": "c" * 64,
               "distribution": False}
    record = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 30, False,
                            worker=[sys.executable, "-c", f"print({json.dumps(created)!r})"])
    assert (record["kinds"], record["reasons"]) == ({"other": 1, "paragraph": 2}, {"other": 2, "shape_text": 1})
    failed = {"status": "parse_error", "error": "BrandNewError", "seconds": 0.1, "rss_before": None, "rss_peak": None}
    record = bigrun.run_doc("pdf-0009", "pdf", path, "dev", 30, False,
                            worker=[sys.executable, "-c", f"print({json.dumps(failed)!r})"])
    assert (record["status"], record["error"]) == ("parse_error", "other")


def test_every_engine_reason_constant_is_a_known_reason():
    found = set()
    for path in (ROOT / "packages" / "engine" / "src" / "hanji").rglob("*.py"):
        found |= set(re.findall(r'^[A-Z][A-Z0-9_]* = "([a-z]+(?:_[a-z0-9]+)+)"', path.read_text(encoding="utf-8"),
                                re.M))
    not_reasons = {"caption_ref", "figure_title"}  # 레이아웃 모델 분류 이름·캡션 연결 키
    assert found - not_reasons <= set(guard.REASONS)


@pytest.mark.parametrize(("name", "date"), [("../escape", "2026-11-12"), ("Bad Name", "2026-11-12"),
                                            ("synthetic", "12-11-2026")], ids=["path-name", "space-name", "bad-date"])
def test_a_bad_name_or_date_fails_before_any_document_runs(tmp_path, name, date):
    manifest, split, registry = _corpus(tmp_path)
    with pytest.raises(guard.GuardError):
        bigrun.run(manifest, split, tmp_path / "out", registry, name, date, allow_no_models=True,
                   worker=[sys.executable, "-c", "raise SystemExit('a worker must not start')"])
    assert not (tmp_path / "out").exists()


def test_a_checkout_without_git_fails_before_any_document_runs(tmp_path, monkeypatch):
    manifest, split, registry = _corpus(tmp_path)
    (tmp_path / "not-a-repo").mkdir()
    monkeypatch.setattr(bigrun, "REPO", tmp_path / "not-a-repo")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(bigrun.BigrunError, match="git"):
        _run(manifest, split, tmp_path / "out", registry)
    assert not (tmp_path / "out").exists()


def test_run_itself_refuses_a_work_folder_under_lab(tmp_path, monkeypatch):
    manifest, split, registry = _corpus(tmp_path)
    (tmp_path / "repo" / "lab").mkdir(parents=True)
    monkeypatch.setattr(bigrun, "REPO", tmp_path / "repo")  # 진짜 lab/ 대신 지어낸 저장소의 lab/
    with pytest.raises(bigrun.workdir.WorkDirError):
        _run(manifest, split, tmp_path / "repo" / "LAB" / "w", registry)
    assert list((tmp_path / "repo" / "lab").iterdir()) == []


def test_jobs_below_one_are_refused_by_the_command_line(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["bigrun", "--jobs", "0"])
    assert caught.value.code == 2 and "at least 1" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "x"])
def test_a_timeout_that_is_not_positive_and_finite_is_refused_by_the_command_line(value, capsys, monkeypatch):
    monkeypatch.setattr(bigrun, "run", lambda *a, **k: pytest.fail("the run must not start"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["bigrun", "--timeout", value])
    assert caught.value.code == 2 and "--timeout" in capsys.readouterr().err


def test_the_default_timeout_is_unchanged(tmp_path, monkeypatch):
    seen = {}

    def fake(*args, **kw):
        seen.update(kw)
        raise bigrun.BigrunError("stop")

    monkeypatch.setattr(bigrun, "run", fake)
    assert cli.main(["bigrun", "--out", str(tmp_path / "out")]) == 2
    assert seen["timeout"] == 600.0 and cli._timeout("0.5") == 0.5 and not (tmp_path / "out").exists()


def test_a_worker_that_cannot_start_is_a_crash_of_that_document(tmp_path):
    record = bigrun.run_doc("pdf-0009", "pdf", tmp_path / "a.pdf", "dev", 5, False,
                            worker=[str(tmp_path / "no-such-worker")])
    assert record["status"] == "crashed"


def test_a_missing_extra_folder_fails_and_duplicate_extra_files_run_once(tmp_path):
    manifest, split, registry = _corpus(tmp_path)
    with pytest.raises(bigrun.BigrunError, match="--extra folder does not exist"):
        _run(manifest, split, tmp_path / "out", registry, extra_dirs=[tmp_path / "missing"])
    extra = tmp_path / "extra"
    extra.mkdir()
    shutil.copyfile(FIXTURES / "ooxml" / "inputs" / "headings.docx", extra / "a.docx")
    shutil.copyfile(FIXTURES / "ooxml" / "inputs" / "headings.docx", extra / "b.docx")
    jobs, skipped = bigrun.extras([extra], corpus.load_manifest(manifest), corpus.load_split(split), [])
    assert (len(jobs), skipped) == (1, 1)


RESULTS = sorted((ROOT / "tools" / "eval" / "results").glob("*/summary.json"))


@pytest.mark.parametrize("path", RESULTS, ids=[p.parent.name for p in RESULTS])
def test_every_committed_summary_matches_its_schema_bytes_markdown_and_sources(path):
    # 손으로 고친 커밋 결과를 잡는다: 스키마, 쓴 그대로의 바이트, render_md, 보고서 출구 검사
    raw = path.read_text(encoding="utf-8")
    summary = json.loads(raw)
    guard.check(summary, bigrun.SUMMARY)
    assert raw == json.dumps(summary, ensure_ascii=True, indent=1) + "\n"
    assert (path.parent / "summary.md").read_text(encoding="utf-8") == bigrun.render_md(summary)
    assert max(len(line) for line in bigrun.render_md(summary).splitlines()) <= 120
    provenance.check_report(provenance.sources_of([summary]), Registry.load(cli.DATASETS), holdout_open=False)


def test_the_committed_results_folder_is_not_empty():
    assert RESULTS


def test_untracked_files_under_the_engine_or_the_harness_make_the_run_dirty(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    for folder in ("packages", "tools/eval/src", "notes"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "keep.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    assert bigrun.git_state(tmp_path)[1] is False
    (tmp_path / "notes" / "other.txt").write_text("x", encoding="utf-8")  # 엔진·하네스 밖은 세지 않는다
    (tmp_path / "tools" / "eval" / "src" / "cache.pyc").write_bytes(b"x")  # .gitignore가 빼는 파일
    assert bigrun.git_state(tmp_path)[1] is False
    (tmp_path / "tools" / "eval" / "src" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    assert bigrun.git_state(tmp_path)[1] is True


def _recording_worker(log: Path) -> list[str]:
    """받은 인자를 log에 적고 규약에 맞는 출력만 내는 지어낸 작업자(문서를 열지 않는다)."""
    code = (
        "import json, sys\n"
        f"open({str(log)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'ingest':\n"
        "    print(json.dumps({'status': 'created', 'error': None, 'seconds': 0.1, 'rss_before': None,\n"
        "                      'rss_peak': None, 'pages': 1, 'kinds': {}, 'reasons': {}, 'tree_sha256': '0' * 64,\n"
        "                      'history_sha256': '0' * 64, 'assets_sha256': '0' * 64, 'distribution': False}))\n"
        "else:\n"
        "    print(json.dumps({k: 1 for k in ('hangul_original', 'hangul_pdf', 'common', 'union', 'set_common',\n"
        "                                     'set_union')}))\n")
    return [sys.executable, "-c", code]


def test_relative_manifest_and_extra_paths_reach_both_workers_as_absolute_paths(tmp_path, monkeypatch):
    manifest, split, registry = _corpus(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    shutil.copyfile(FIXTURES / "ooxml" / "inputs" / "headings.docx", extra / "new.docx")
    log = tmp_path / "args.jsonl"
    monkeypatch.chdir(tmp_path)  # 작업자는 홈 폴더에서 돈다: 호출한 쪽의 상대 경로는 거기서 풀리면 안 된다
    _run(Path("corpus") / "manifest.json", split, tmp_path / "out", registry, jobs=1,
         extra_dirs=[Path("extra")], worker=_recording_worker(log))
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    paths = [Path(p) for c in calls for p in (c[1:2] if c[0] == "ingest" else c[1:3])]
    assert {c[0] for c in calls} == {"ingest", "pair"} and len(paths) == 6 + 4
    assert all(p.is_absolute() and p.is_file() for p in paths)


def test_missing_models_keep_the_hashes_of_the_models_that_resolved(monkeypatch):
    def resolve(name: str) -> Path:
        if name == "layout":
            raise ModelError("synthetic missing model")
        return Path("/synthetic") / name

    monkeypatch.setattr(bigrun.models, "resolve", resolve)
    shas, missing = bigrun.model_shas(allow_missing=True)
    assert missing == ["layout"]
    assert shas == sorted(bigrun.models.MANIFEST[n].sha256 for n in bigrun.models.names() if n != "layout")
    with pytest.raises(ModelError):
        bigrun.model_shas(allow_missing=False)


def test_a_summary_records_missing_model_names_and_the_committed_form_still_passes():
    tools = {"tool": "hanji", "version": "0.1.0", "models": ["0" * 64]}
    spec = guard.ENV
    env = {"system": "Linux", "machine": "x86_64", "cpu_model": "other", "cores": 1, "memory_bytes": None,
           "python": "3.12.13", "threads": {}, "datasets": [], "split": None}
    guard.check(env | {"tools": [tools]}, spec)  # 커밋된 요약의 꼴(빠진 모델 없음)
    guard.check(env | {"tools": [tools | {"models_missing": ["layout", "ocr-det"]}]}, spec)
    for bad in (["/abs/path"], ["layout", "layout"], "layout"):
        with pytest.raises(guard.GuardError):
            guard.check(env | {"tools": [tools | {"models_missing": bad}]}, spec)


def test_two_runs_cannot_reserve_the_same_work_folder(tmp_path):
    out = tmp_path / "out"
    lock = bigrun.reserve(out)
    with pytest.raises(bigrun.BigrunError, match="in use"):
        bigrun.reserve(out)
    bigrun.release(lock)
    empty = tmp_path / "empty"
    empty.mkdir()
    lock = bigrun.reserve(empty)  # 이미 있는 빈 폴더도 같은 규칙(잠금 파일 하나로)
    with pytest.raises(bigrun.BigrunError, match="in use"):
        bigrun.reserve(empty)
    bigrun.release(lock)
    (empty / "old.txt").write_text("x", encoding="utf-8")
    with pytest.raises(bigrun.BigrunError, match="new or empty"):
        bigrun.reserve(empty)
    assert sorted(p.name for p in empty.iterdir()) == ["old.txt"]


def test_a_run_holds_the_work_folder_and_releases_it_when_done(tmp_path, monkeypatch):
    manifest, split, registry = _corpus(tmp_path)
    seen = []
    real = bigrun.run_doc

    def spy(*args, **kw):
        seen.append((tmp_path / "out" / bigrun.LOCK).exists())
        return real(*args, **kw)

    monkeypatch.setattr(bigrun, "run_doc", spy)
    _run(manifest, split, tmp_path / "out", registry, jobs=1, worker=_recording_worker(tmp_path / "args.jsonl"))
    assert seen and all(seen) and not (tmp_path / "out" / bigrun.LOCK).exists()
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / bigrun.LOCK).write_bytes(b"")  # 다른 실행이 잡은 폴더
    with pytest.raises(bigrun.BigrunError):
        _run(manifest, split, tmp_path / "busy", registry, worker=NEVER)
