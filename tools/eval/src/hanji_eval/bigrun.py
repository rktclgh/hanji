"""대규모 수집 실행(스펙 §5): lab/corpus-big의 개발 실행 세트(보류 발행처 문서를 뺀 것)를 문서마다 새 하위 프로세스로
돌려 docs.jsonl·pairs.jsonl(작업 폴더 workdir, 커밋하지 않는다, 문서마다 이어 쓴다)과 summary.json·summary.md(숫자만,
커밋 가능)를 쓴다. 보류 발행처 문서는 돌리지 않고 sealed 수로만 센다."""

import importlib.metadata
import json
import os
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from hanji import models
from hanji.errors import ModelError
from hanji.resources import cpu_count, pool_threads

from . import corpus, guard, provenance, workdir
from .guard import GuardError
from .datasets import Registry, RegistryError, file_sha256
from .metrics.bootstrap import cp_upper, percentile

FORMATS = guard.FORMATS
NATIVE = ("hwp", "hwpx", "docx", "pptx")
WORKER = [sys.executable, "-I", "-m", "hanji_eval.worker"]
PAIR_RULE = {"jaccard": 0.9, "ratio": (0.8, 1.25)}  # BR 규칙(스펙 §2·§5): 한글 음절 집합 Jaccard와 한글 수 비
MiB = 1024 * 1024
KINDS = ("heading", "paragraph", "list_item", "caption", "page_header", "page_footer", "table", "figure", "other")
EXTS = {".pdf": "pdf", ".hwp": "hwp", ".hwpx": "hwpx", ".docx": "docx", ".pptx": "pptx"}
REPO = Path(__file__).resolve().parents[4]
# 스펙 §7.6: 문서 단위 상한은 근사이고 결과에 그 사실을 적는다(summary.md에 늘 같은 글로)
CP_NOTE = ("- 문서 단위 95% 상한(한쪽 Clopper-Pearson)은 전수 집계를 문서 모집단에서 뽑은 표본으로 보는 근사다"
           "(스펙 §7.6).")

_N = guard.optional(guard.number(0))
_I = guard.optional(guard.integer())
# 작업자 출력의 규약(이 꼴이 아니면 그 문서는 crashed, 오류 WorkerProtocolError)
WORKER_FAILED = guard.obj({"status": guard.enum("parse_error"), "error": guard.pattern(guard.ERROR_CLASS),
                           "seconds": guard.number(0), "rss_before": _I, "rss_peak": _I})
WORKER_CREATED = guard.obj({
    "status": guard.enum("created"), "error": guard.optional(guard.pattern(guard.ERROR_CLASS)),
    "seconds": guard.number(0), "rss_before": _I, "rss_peak": _I, "pages": _I,
    "kinds": guard.mapping(guard.pattern(guard.CODE), guard.integer()),
    "reasons": guard.mapping(guard.pattern(guard.CODE), guard.integer()),
    "tree_sha256": guard.pattern(guard.SHA256), "history_sha256": guard.pattern(guard.SHA256),
    "assets_sha256": guard.pattern(guard.SHA256), "distribution": guard.boolean(),
})
WORKER_PAIR = guard.obj({k: guard.integer() for k in ("hangul_original", "hangul_pdf", "common", "union", "set_common",
                                                      "set_union")})
DOC_RECORD = guard.obj({
    "doc": guard.pattern(guard.DOC), "format": guard.enum(*FORMATS), "set": guard.enum("dev", "extra"),
    "status": guard.enum(*guard.STATUSES), "error": guard.optional(guard.enum(*guard.ERRORS)),
    "seconds": guard.number(0), "bytes": guard.integer(), "pages": guard.optional(guard.integer()),
    "rss_peak_bytes": guard.optional(guard.integer()), "rss_extra_bytes": guard.optional(guard.integer()),
    "kinds": guard.mapping(guard.enum(*KINDS), guard.integer()),
    "reasons": guard.mapping(guard.enum(*guard.REASONS), guard.integer()),
    "coverage_mismatch": guard.integer(), "unassigned_text": guard.integer(),
    "tree_sha256": guard.optional(guard.pattern(guard.SHA256)),
    "history_sha256": guard.optional(guard.pattern(guard.SHA256)),
    "assets_sha256": guard.optional(guard.pattern(guard.SHA256)),
    "distribution": guard.boolean(), "twice_same": guard.optional(guard.boolean()),
})
PAIR_RECORD = guard.obj({
    "bundle": guard.pattern(guard.BUNDLE), "original": guard.pattern(guard.DOC), "pdf": guard.pattern(guard.DOC),
    "format": guard.enum("hwp", "hwpx"),
    "status": guard.enum("measured", "excluded_not_same_text", "excluded_distribution", "excluded_failed"),
    "recall": _N, "precision": _N, "jaccard": _N, "jaccard_set": _N, "ratio": _N,
    "hangul_original": guard.integer(), "hangul_pdf": guard.integer(),
})
_DIST = guard.obj({"median": _N, "p95": _N, "max": _N})
_STATS = guard.obj({
    "status": guard.mapping(guard.enum(*FORMATS), guard.mapping(guard.enum(*guard.STATUSES), guard.integer())),
    "ledger": guard.mapping(guard.enum(*FORMATS), guard.obj({
        "coverage_mismatch_docs": guard.integer(), "coverage_mismatch": guard.integer(),
        "unassigned_text_docs": guard.integer(), "unassigned_text": guard.integer()})),
    "errors": guard.mapping(guard.enum(*guard.ERRORS), guard.integer()),
    "seconds": guard.mapping(guard.enum(*FORMATS), _DIST),
    "rss_peak_mib": guard.mapping(guard.enum(*FORMATS), _DIST),
    "factor_max": guard.mapping(guard.enum(*FORMATS), _N),
    "reasons": guard.mapping(guard.enum(*FORMATS), guard.mapping(guard.enum(*guard.REASONS),
                                                                 guard.array(guard.integer(), 2, 2))),
    # 문서 단위 한쪽 Clopper-Pearson 95% 상한(스펙 §7.6): 실패율, coverage_mismatch·unassigned_text 문서율
    "bounds": guard.mapping(guard.enum(*FORMATS, "all"), guard.obj({
        "docs": guard.integer(), "failure_upper": _N, "coverage_mismatch_upper": _N, "unassigned_text_upper": _N})),
    "determinism": guard.optional(guard.obj({"checked": guard.integer(), "same": guard.integer()})),
})
SUMMARY = guard.obj({
    "schema": guard.enum("hanji-eval-bigrun/1"),
    "run": guard.obj({"name": guard.pattern(guard.SLUG), "date": guard.pattern(guard.DATE),
                      "harness_commit": guard.pattern(guard.COMMIT), "engine_commit": guard.pattern(guard.COMMIT),
                      "dirty": guard.boolean(), "manifest_sha256": guard.pattern(guard.SHA256),
                      "split_sha256": guard.pattern(guard.SHA256)}),
    "env": guard.obj({"machine": guard.ENV, "jobs": guard.integer(1), "timeout_s": guard.number(0),
                      "twice": guard.boolean()}),
    "sources": guard.array(guard.SOURCE, 1),
    "counts": guard.obj({"manifest": guard.integer(), "sealed": guard.integer(), "run": guard.integer(),
                         "extra": guard.integer(), "extra_skipped": guard.integer()}),
    "dev": _STATS,
    "pairs": guard.obj({
        "links": guard.integer(), "status": guard.mapping(guard.enum(
            "measured", "excluded_not_same_text", "excluded_distribution", "excluded_failed"), guard.integer()),
        "measured_by_multiset_rule": guard.integer(),
        "by_format": guard.mapping(guard.enum("hwp", "hwpx"), guard.obj({
            "n": guard.integer(), "recall_min": _N, "recall_median": _N, "precision_min": _N,
            "precision_median": _N})),
    }),
    "gate": guard.obj({"status_complete": guard.boolean(), "native_ledger_clean": guard.boolean(),
                       "pdf_coverage_clean": guard.boolean(), "passed": guard.boolean()}),
    "flagged": guard.array(guard.obj({"doc": guard.pattern(guard.DOC), "format": guard.enum(*FORMATS),
                                      "status": guard.enum(*guard.STATUSES),
                                      "why": guard.array(guard.enum("failed", "coverage_mismatch",
                                                                    "unassigned_text"), 1)})),
})


class BigrunError(RuntimeError):
    """실행할 수 없다(작업 폴더가 새 폴더가 아님 등)."""


def _run_worker(args: Sequence[str], timeout: float, worker: Sequence[str],
                env: Mapping[str, str] | None = None) -> tuple[str, Any, float]:
    """(상태, 출력, 걸린 초). 시간 초과 → timeout, 출력 없이 끝남·마지막 줄이 JSON 아님 → crashed. 표준 오류는
    버린다(글자를 담을 수 있다). 표준 출력은 마지막 줄의 JSON만 읽는다(그 밖의 줄은 어디에도 쓰지 않는다)."""
    start = os.times().elapsed
    try:
        done = subprocess.run([*worker, *args], capture_output=True, timeout=timeout, cwd=os.path.expanduser("~"),
                              env=None if env is None else dict(env))
    except subprocess.TimeoutExpired:
        return "timeout", None, os.times().elapsed - start
    except OSError:  # 작업 프로세스를 띄우지 못함(실행 파일 없음·파일 기술자 부족 등): 그 문서만 crashed
        return "crashed", None, os.times().elapsed - start
    elapsed = os.times().elapsed - start
    lines = done.stdout.decode("utf-8", "replace").strip().splitlines()
    if done.returncode != 0 or not lines:
        return "crashed", None, elapsed
    try:
        return "ok", json.loads(lines[-1]), elapsed
    except json.JSONDecodeError:
        return "crashed", None, elapsed


def _valid(out: Any, *specs: guard.Spec) -> bool:
    for spec in specs:
        try:
            guard.check(out, spec)
            return True
        except GuardError:
            continue
    return False


def _known(counts: Mapping[str, int], allowed: tuple[str, ...]) -> dict[str, int]:
    """모르는 이름은 other로 모은다(엔진이 새 블록 종류·처리 이력 사유를 내도 실행이 멈추지 않는다)."""
    out: Counter[str] = Counter()
    for key, n in counts.items():
        out[key if key in allowed else "other"] += n
    return dict(sorted(out.items()))


def run_doc(doc: str, fmt: str, path: Path, which: str, timeout: float, twice: bool,
            worker: Sequence[str] = WORKER, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """문서 하나. 작업자 출력이 규약(WORKER_FAILED·WORKER_CREATED)에 맞지 않으면 crashed + WorkerProtocolError.
    assets_sha256은 그림 자산 id(이미 내용의 sha256)를 정렬해 이은 것의 sha256이다."""
    state, out, elapsed = _run_worker(["ingest", str(path), doc], timeout, worker, env)
    error = None
    if state == "ok" and not _valid(out, WORKER_FAILED, WORKER_CREATED):
        state, out, error = "crashed", None, "WorkerProtocolError"
    record: dict[str, Any] = {
        "doc": doc, "format": fmt, "set": which, "status": state if state != "ok" else out["status"],
        "error": error, "seconds": out["seconds"] if out else elapsed,
        "bytes": path.stat().st_size if path.is_file() else 0, "pages": None,
        "rss_peak_bytes": out["rss_peak"] if out else None, "rss_extra_bytes": None, "kinds": {}, "reasons": {},
        "coverage_mismatch": 0, "unassigned_text": 0,
        "tree_sha256": None, "history_sha256": None, "assets_sha256": None, "distribution": False,
        "twice_same": None,
    }
    if out and out["error"]:
        record["error"] = out["error"] if out["error"] in guard.ERRORS else "other"
    if out and out["rss_peak"] is not None and out["rss_before"] is not None:
        record["rss_extra_bytes"] = max(0, out["rss_peak"] - out["rss_before"])
    if out and out["status"] == "created":
        record.update({k: out[k] for k in ("pages", "tree_sha256", "history_sha256", "assets_sha256",
                                           "distribution")})
        record["kinds"] = _known(out["kinds"], KINDS)
        record["reasons"] = _known(out["reasons"], guard.REASONS)
        record["coverage_mismatch"] = out["reasons"].get("coverage_mismatch", 0)
        record["unassigned_text"] = out["reasons"].get("unassigned_text", 0)
        if twice:
            again_state, again, _ = _run_worker(["ingest", str(path), doc], timeout, worker, env)
            record["twice_same"] = again_state == "ok" and isinstance(again, dict) and all(
                again.get(k) == out[k] for k in ("tree_sha256", "history_sha256", "assets_sha256"))
    guard.check(record, DOC_RECORD)
    return record


def pair_record(bundle: str, original: dict, pdf: dict, numbers: Any) -> dict[str, Any]:
    """BR 규칙: 한글 음절 집합 Jaccard ≥ 0.9이고 한글 수 비(원본 ÷ PDF) 0.8~1.25면 measured. 배포용 원본이 먼저
    excluded_distribution(짝 대조를 돌리지 않는다), 어느 쪽이 실패했거나 대조가 끝나지 않았거나 규약 밖 출력이면
    excluded_failed."""
    record: dict[str, Any] = {"bundle": bundle, "original": original["doc"], "pdf": pdf["doc"],
                              "format": original["format"], "status": "excluded_failed", "recall": None,
                              "precision": None, "jaccard": None, "jaccard_set": None, "ratio": None,
                              "hangul_original": 0, "hangul_pdf": 0}
    if original["distribution"]:
        record["status"] = "excluded_distribution"
    if record["status"] == "excluded_distribution" or original["status"] != "created" or pdf["status"] != "created" \
            or not _valid(numbers, WORKER_PAIR):
        guard.check(record, PAIR_RECORD)
        return record
    h, p = numbers["hangul_original"], numbers["hangul_pdf"]
    record.update({
        "hangul_original": h, "hangul_pdf": p,
        "recall": numbers["common"] / p if p else None, "precision": numbers["common"] / h if h else None,
        "jaccard": numbers["common"] / numbers["union"] if numbers["union"] else None,
        "jaccard_set": numbers["set_common"] / numbers["set_union"] if numbers["set_union"] else None,
        "ratio": h / p if p else None,
    })
    lo, hi = PAIR_RULE["ratio"]
    if (record["jaccard_set"] or 0) >= PAIR_RULE["jaccard"] and lo <= (record["ratio"] or 0) <= hi:
        record["status"] = "measured"
    else:
        record["status"] = "excluded_not_same_text"
    guard.check(record, PAIR_RECORD)
    return record


def _dist(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p95": None, "max": None}
    s = sorted(values)
    return {"median": percentile(s, 0.5), "p95": percentile(s, 0.95), "max": s[-1]}


def _bounds(rs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rs)
    upper = (lambda x: cp_upper(x, n)) if n else (lambda x: None)
    return {"docs": n, "failure_upper": upper(sum(r["status"] != "created" for r in rs)),
            "coverage_mismatch_upper": upper(sum(r["coverage_mismatch"] > 0 for r in rs)),
            "unassigned_text_upper": upper(sum(r["unassigned_text"] > 0 for r in rs))}


def stats(records: list[dict[str, Any]], twice: bool) -> dict[str, Any]:
    by_fmt: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_fmt[r["format"]].append(r)
    out: dict[str, Any] = {k: {} for k in ("status", "ledger", "seconds", "rss_peak_mib", "factor_max", "reasons")}
    out["errors"] = dict(sorted(Counter(r["error"] for r in records if r["error"]).items()))
    out["bounds"] = {"all": _bounds(records)}
    for fmt in sorted(by_fmt):
        rs = by_fmt[fmt]
        ok = [r for r in rs if r["status"] == "created"]
        out["status"][fmt] = dict(sorted(Counter(r["status"] for r in rs).items()))
        out["ledger"][fmt] = {
            "coverage_mismatch_docs": sum(r["coverage_mismatch"] > 0 for r in rs),
            "coverage_mismatch": sum(r["coverage_mismatch"] for r in rs),
            "unassigned_text_docs": sum(r["unassigned_text"] > 0 for r in rs),
            "unassigned_text": sum(r["unassigned_text"] for r in rs)}
        out["bounds"][fmt] = _bounds(rs)
        out["seconds"][fmt] = _dist([r["seconds"] for r in ok])
        out["rss_peak_mib"][fmt] = _dist([r["rss_peak_bytes"] / MiB for r in ok if r["rss_peak_bytes"] is not None])
        factors = [r["rss_extra_bytes"] / r["bytes"] for r in ok if r["rss_extra_bytes"] is not None and r["bytes"]]
        out["factor_max"][fmt] = max(factors) if factors else None
        reasons: dict[str, list[int]] = {}
        for r in rs:
            for reason, n in r["reasons"].items():
                reasons.setdefault(reason, [0, 0])
                reasons[reason][0] += n
                reasons[reason][1] += 1
        out["reasons"][fmt] = dict(sorted(reasons.items()))
    checked = [r for r in records if r["twice_same"] is not None]
    out["determinism"] = {"checked": len(checked), "same": sum(r["twice_same"] for r in checked)} if twice else None
    return out


def _median(values: list[float]) -> float | None:
    return percentile(sorted(values), 0.5) if values else None


def summarize(run: dict[str, Any], env: dict[str, Any], sources: list[dict[str, str]], counts: dict[str, int],
              dev: list[dict[str, Any]], expected: set[str], pairs: list[dict[str, Any]],
              twice: bool) -> dict[str, Any]:
    """게이트(스펙 §5): 모든 문서에 상태(개발 실행 세트의 문서 번호가 겹치지 않고 기대한 세트와 같으며 manifest =
    실행 + sealed), 네이티브 coverage_mismatch·unassigned_text 0, PDF coverage_mismatch 0. 문서 번호는 실패·게이트
    위반 문서만 싣는다. --extra 문서의 숫자는 개발 출처가 아니므로 요약에 싣지 않는다(작업 폴더 docs.jsonl에만)."""
    flagged = []
    for r in dev:
        why = []
        if r["status"] != "created":
            why.append("failed")
        if r["coverage_mismatch"]:
            why.append("coverage_mismatch")
        if r["unassigned_text"] and r["format"] in NATIVE:
            why.append("unassigned_text")
        if why:
            flagged.append({"doc": r["doc"], "format": r["format"], "status": r["status"], "why": why})
    by_format: dict[str, Any] = {}
    for fmt in ("hwp", "hwpx"):
        measured = [p for p in pairs if p["format"] == fmt and p["status"] == "measured"]
        if measured:
            by_format[fmt] = {
                "n": len(measured), "recall_min": min(p["recall"] for p in measured),
                "recall_median": _median([p["recall"] for p in measured]),
                "precision_min": min(p["precision"] for p in measured),
                "precision_median": _median([p["precision"] for p in measured])}
    lo, hi = PAIR_RULE["ratio"]
    by_multiset = sum(1 for p in pairs if p["status"] in ("measured", "excluded_not_same_text")
                      and (p["jaccard"] or 0) >= PAIR_RULE["jaccard"] and lo <= (p["ratio"] or 0) <= hi)
    native_clean = not any(r["coverage_mismatch"] or r["unassigned_text"] for r in dev if r["format"] in NATIVE)
    pdf_clean = not any(r["coverage_mismatch"] for r in dev if r["format"] == "pdf")
    ids = [r["doc"] for r in dev]
    complete = (len(set(ids)) == len(ids) and set(ids) == expected
                and counts["manifest"] == len(dev) + counts["sealed"])
    summary = {
        "schema": "hanji-eval-bigrun/1", "run": run, "env": env, "sources": sources, "counts": counts,
        "dev": stats(dev, twice),
        "pairs": {"links": len(pairs), "status": dict(sorted(Counter(p["status"] for p in pairs).items())),
                  "measured_by_multiset_rule": by_multiset, "by_format": by_format},
        "gate": {"status_complete": complete, "native_ledger_clean": native_clean, "pdf_coverage_clean": pdf_clean,
                 "passed": complete and native_clean and pdf_clean},
        "flagged": sorted(flagged, key=lambda f: f["doc"]),
    }
    guard.check(summary, SUMMARY)
    return summary


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def render_md(summary: dict[str, Any]) -> str:
    """summary.json에서 고정 서식으로만 만든다."""
    c, g, d = summary["counts"], summary["gate"], summary["dev"]
    lines = [f"# 대규모 실행 {summary['run']['name']}", "",
             f"- manifest {c['manifest']}, 실행 {c['run']}, 봉인(sealed) {c['sealed']}, 추가 {c['extra']}"
             f"(manifest와 같아 건너뜀 {c['extra_skipped']})",
             f"- 게이트: 상태 완비 {g['status_complete']}, 네이티브 장부 {g['native_ledger_clean']}, "
             f"PDF 장부 {g['pdf_coverage_clean']}, 통과 {g['passed']}", "",
             "| 형식 | 상태 | coverage_mismatch 문서 | unassigned_text 문서 | 초 중앙값/p95/최대 | 최대 RSS MiB 중앙값/p95/최대 "
             "| 배수 최대 | 실패율·coverage_mismatch 문서율 95% 상한 |", "|---|---|---|---|---|---|---|---|"]
    for fmt in sorted(d["status"]):
        s, r, led, b = d["seconds"][fmt], d["rss_peak_mib"][fmt], d["ledger"][fmt], d["bounds"][fmt]
        lines.append(f"| {fmt} | {d['status'][fmt]} | {led['coverage_mismatch_docs']} | "
                     f"{led['unassigned_text_docs']} | {_fmt(s['median'])}/{_fmt(s['p95'])}/{_fmt(s['max'])} | "
                     f"{_fmt(r['median'])}/{_fmt(r['p95'])}/{_fmt(r['max'])} | {_fmt(d['factor_max'][fmt])} | "
                     f"{_fmt(b['failure_upper'])}·{_fmt(b['coverage_mismatch_upper'])} |")
    b = d["bounds"]["all"]
    lines.append(f"- 전체 {b['docs']}건: 실패율 상한 {_fmt(b['failure_upper'])}, coverage_mismatch 문서율 상한 "
                 f"{_fmt(b['coverage_mismatch_upper'])}, unassigned_text 문서율 상한 {_fmt(b['unassigned_text_upper'])}")
    lines.append(CP_NOTE)
    p = summary["pairs"]
    lines += ["", f"- 짝 연결 {p['links']}: {p['status']}, 다중집합 Jaccard 규칙이면 셈 {p['measured_by_multiset_rule']}"]
    for fmt, v in sorted(p["by_format"].items()):
        lines.append(f"- {fmt}: n {v['n']}, 재현율 최소 {_fmt(v['recall_min'])}·중앙값 {_fmt(v['recall_median'])}, "
                     f"정밀도 최소 {_fmt(v['precision_min'])}·중앙값 {_fmt(v['precision_median'])}")
    lines += ["", "## 실패·게이트 위반 문서", "", "| 문서 | 형식 | 상태 | 까닭 |", "|---|---|---|---|"]
    lines += [f"| {f['doc']} | {f['format']} | {f['status']} | {', '.join(f['why'])} |" for f in summary["flagged"]]
    return "\n".join(lines) + "\n"


def _held(split: dict[str, Any], sealed: list[dict[str, Any]]) -> set[str]:
    """봉인 문서(보류 발행처의 문서와 분할에 적힌 보류 묶음 문서)의 sha256."""
    return {d["sha256"] for d in sealed} | {d["sha256"] for r in split["bundles"] if r["split"] == "holdout"
                                            for d in [r["pdf"], *r["originals"]]}


def check_files(docs: list[dict[str, Any]], manifest: list[dict[str, Any]], manifest_path: Path,
                held: set[str]) -> None:
    """돌릴 문서와 짝으로 열 문서의 파일이 manifest의 sha256과 같은지 작업 프로세스를 띄우기 전에 본다(문서 번호만
    알린다). 봉인 문서의 바이트면 SealError, 없거나 바뀌었거나 링크 등으로 manifest 폴더 밖으로 풀리면 RegistryError."""
    in_run = {d["id"] for d in docs}
    partners = [d for b in corpus.bundles(manifest) if b.pdf["id"] in in_run for d in [b.pdf, *b.originals]]
    base = manifest_path.parent.resolve()
    sealed, bad = set(), set()
    for d in [*docs, *partners]:
        path = (manifest_path.parent / d["file"]).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            bad.add(d["id"])
            continue
        sha = file_sha256(path)
        if sha in held and sha != d["sha256"]:
            sealed.add(d["id"])
        elif sha != d["sha256"]:
            bad.add(d["id"])
    if sealed:
        raise corpus.SealError(f"run set files hold sealed documents: {', '.join(sorted(sealed)[:10])}")
    if bad:
        raise RegistryError(f"run set files differ from the manifest: {', '.join(sorted(bad)[:10])}")


def extras(dirs: Sequence[Path], manifest: list[dict[str, Any]], split: dict[str, Any],
           sealed: list[dict[str, Any]]) -> tuple[list[tuple[str, str, Path]], int]:
    """(돌릴 추가 문서, 건너뛴 수). 봉인 문서(보류 발행처·보류 묶음)와 sha256이 같은 파일이 있으면 SealError, 개발
    실행 세트 문서와 같은 파일은 이미 돌므로 건너뛴다."""
    held = _held(split, sealed)
    known = {d["sha256"] for d in manifest}
    out, skipped, bad = [], 0, 0
    for folder in dirs:
        if not folder.is_dir():
            raise BigrunError("an --extra folder does not exist")
        for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in EXTS):
            sha = file_sha256(path)
            if sha in held:
                bad += 1
            elif sha in known:
                skipped += 1
            elif any(doc == f"extra-{sha[:12]}" for doc, _, _ in out):  # 같은 파일은 한 번만 돈다
                skipped += 1
            else:
                out.append((f"extra-{sha[:12]}", EXTS[path.suffix.lower()], path))
    if bad:
        raise corpus.SealError(f"--extra holds {bad} copies of sealed holdout documents")
    return out, skipped


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout.strip()


def model_shas(allow_missing: bool) -> list[str]:
    """기본 설정의 OCR·레이아웃 모델 파일(크기·sha256 확인). 하나라도 없거나 다르면 ModelError(allow_missing이면 빈
    목록: 그 실행은 모델 없이 돈 것이고 요약의 env.tools[0].models가 빈다)."""
    try:
        return sorted(models.MANIFEST[name].sha256 for name in models.names() if models.resolve(name))
    except ModelError:
        if allow_missing:
            return []
        raise


def run(manifest_path: Path, split_path: Path, out: Path, registry: Registry, name: str, date: str, jobs: int = 4,
        timeout: float = 600.0, twice: bool = False, extra_dirs: Sequence[Path] = (),
        worker: Sequence[str] = WORKER, allow_no_models: bool = False) -> dict[str, Any]:
    """manifest·분할 파일은 datasets.toml의 corpus-big·hanji-ko-v1 파일과 sha256이 같아야 한다(어느 체크아웃의 lab에서
    읽든). out은 새 폴더(또는 빈 폴더)여야 한다. 커밋은 이 하네스가 든 저장소의 HEAD(추적 파일이 바뀌었으면 dirty)."""
    out = workdir.refuse_lab(out, REPO)  # CLI를 거치지 않아도 lab/ 아래에는 쓰지 않는다
    guard.pattern(guard.SLUG)(name, "$.run.name")  # 이름·날짜·git은 문서를 돌리기 전에 본다(끝에서 실패하지 않게)
    guard.pattern(guard.DATE)(date, "$.run.date")
    try:
        commit = _git(REPO, "rev-parse", "HEAD")
        dirty = bool(_git(REPO, "status", "--porcelain", "--untracked-files=no")
                     or _git(REPO, "status", "--porcelain", "--untracked-files=all", "--", "packages"))
    except (OSError, subprocess.CalledProcessError):
        raise BigrunError("the harness checkout is not a git repository with a commit") from None
    dataset = registry.get("corpus-big")
    sha = file_sha256(manifest_path)
    if sha not in {f.sha256 for f in dataset.files}:
        raise RegistryError("corpus-big: manifest sha256 differs from datasets.toml")
    split_sha = file_sha256(split_path)
    if split_sha not in {f.sha256 for f in registry.get("hanji-ko-v1").files}:
        raise RegistryError("hanji-ko-v1: split sha256 differs from datasets.toml")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BigrunError("the work folder must be new or empty")
    manifest = corpus.load_manifest(manifest_path)
    split = corpus.load_split(split_path)
    todo, sealed = corpus.dev_run_set(manifest, sha, split)
    corpus.assert_unsealed(todo, manifest, split)
    check_files(todo, manifest, manifest_path, _held(split, sealed))
    extra_jobs, skipped = extras(extra_dirs, manifest, split, sealed)
    base = manifest_path.parent
    sources = [registry.source("corpus-big", "dev")]
    provenance.check_selection(sources, registry)
    shas = model_shas(allow_no_models)
    # 작업 프로세스마다 ORT 스레드를 나눈다(ingest_many와 같은 규칙, 사용자가 정한 HANJI_ORT_THREADS가 이긴다)
    threads = os.environ.get("HANJI_ORT_THREADS") or str(pool_threads(jobs, cpu_count(), os.cpu_count() or 1) or "")
    env = dict(os.environ) | ({"HANJI_ORT_THREADS": threads} if threads else {})
    jobs_list = [(d["id"], d["format"], base / d["file"], "dev") for d in todo]
    jobs_list += [(doc, fmt, path, "extra") for doc, fmt, path in extra_jobs]
    out.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def append(name: str, record: dict[str, Any]) -> dict[str, Any]:
        with lock, (out / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
        return record

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        records = list(pool.map(lambda j: append("docs.jsonl", run_doc(*j, timeout=timeout, twice=twice,
                                                                       worker=worker, env=env)), jobs_list))
    by_doc = {r["doc"]: r for r in records}
    links = []
    for b in corpus.bundles(manifest):
        if b.pdf["id"] in by_doc:
            links += [(b.bundle, by_doc[o["id"]], by_doc[b.pdf["id"]], base / o["file"], base / b.pdf["file"])
                      for o in b.originals]

    def one(link: tuple) -> dict[str, Any]:
        bundle, original, pdf, opath, ppath = link
        numbers = None
        if original["status"] == "created" and pdf["status"] == "created" and not original["distribution"]:
            state, numbers, _ = _run_worker(["pair", str(opath), str(ppath)], timeout, worker, env)
            numbers = numbers if state == "ok" else None
        return append("pairs.jsonl", pair_record(bundle, original, pdf, numbers))

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        pairs = list(pool.map(one, links))
    dev = sorted((r for r in records if r["set"] == "dev"), key=lambda r: r["doc"])
    machine = guard.machine_env({k: env.get(k, "") for k in ("HANJI_ORT_THREADS", "OMP_NUM_THREADS")})
    tools = [{"tool": "hanji", "version": importlib.metadata.version("hanji"), "models": shas}]
    env = {"machine": machine | {"tools": tools, "datasets": [{"dataset": "corpus-big", "version": dataset.version,
                                                               "identity": dataset.identity}], "split": "hanji-ko-v1"},
           "jobs": jobs, "timeout_s": timeout, "twice": twice}
    run_info = {"name": name, "date": date, "harness_commit": commit, "engine_commit": commit, "dirty": dirty,
                "manifest_sha256": sha, "split_sha256": split_sha}
    counts = {"manifest": len(manifest), "sealed": len(sealed), "run": len(dev), "extra": len(extra_jobs),
              "extra_skipped": skipped}
    summary = summarize(run_info, env, [{"dataset": s.dataset, "split": s.split} for s in sources], counts, dev,
                        {d["id"] for d in todo}, sorted(pairs, key=lambda p: (p["bundle"], p["original"])), twice)
    provenance.check_report(provenance.sources_of([summary]), registry, holdout_open=False)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=True, indent=1) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(render_md(summary), encoding="utf-8")
    return summary
