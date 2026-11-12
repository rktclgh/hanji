"""커밋용 보고서: report.json(스키마 hanji-eval-report/1)과 그것에서만 만드는 report.md. 쓰기 전에 필드 스키마, 제외
장부 등식, 출처(공개 가능·봉인)를 검사하고 하나라도 어긋나면 쓰지 않는다."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import guard, holdout, provenance
from .datasets import Registry, RegistryError, Source


class ReportError(ValueError):
    """보고서가 장부 등식이나 병합 규칙에 맞지 않는다."""


# 병합·중복 검사의 줄 열쇠(값이 아닌 자리)
KEYS = {"ledger": ("dataset", "split", "unit", "tool"),
        "results": ("dataset", "split", "tool", "tool_version", "config", "metric", "agg"),
        "failures": ("dataset", "split", "tool", "doc")}


def check_ledger(ledger: list[dict[str, Any]]) -> None:
    """전체 = 채점 + Σ 사유별 제외(줄마다), 1단계 제외 수는 같은 데이터셋·분할·단위에서 도구마다 같다.
    같은 (데이터셋·분할·단위·도구) 줄이 둘 이상이면 실패한다(앞 줄을 덮어써 같음 검사를 건너뛰지 않게)."""
    masks: dict[tuple[str, str, str], dict[str, tuple[int, ...]]] = defaultdict(dict)
    for row in ledger:
        key = (row["dataset"], row["split"], row["unit"])
        if row["tool"] in masks[key]:
            raise ReportError(f"duplicate ledger row: {'/'.join(key)}/{row['tool']}")
        if row["total"] != row["scored"] + sum(row["excluded"].values()):
            raise ReportError(f"ledger does not add up: {'/'.join(key)}/{row['tool']}")
        masks[key][row["tool"]] = tuple(row["excluded"].get(r, 0) for r in guard.MASK_REASONS)
    for key, by_tool in masks.items():
        if len(set(by_tool.values())) > 1:
            raise ReportError(f"mask exclusions differ between tools: {'/'.join(key)}")


def check_unique(part: str, rows: list[dict[str, Any]]) -> None:
    """같은 열쇠(KEYS)의 줄이 둘 이상이면 실패한다(꼭 같은 줄도: 하나로 두는 것은 merge만 한다)."""
    seen: set[tuple] = set()
    for row in rows:
        key = tuple(row[k] for k in KEYS[part])
        if key in seen:
            raise ReportError(f"duplicate {part} row: {'/'.join(map(str, key))}")
        seen.add(key)


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """두 보고서를 합친다(스키마가 같고 둘 다 그 스키마에 맞아야 하며 run·env가 같아야 한다). 결과의 출처는 그대로
    따라간다."""
    if a.get("schema") != b.get("schema"):
        raise ReportError("reports with different schemas cannot be merged")
    guard.check(a, guard.REPORT)  # 줄을 합치기 전에 둘 다 본다(합친 뒤의 검사만으로는 b의 틀린 꼴이 가려진다)
    guard.check(b, guard.REPORT)
    if a["run"] != b["run"] or a["env"] != b["env"]:
        raise ReportError("reports from different runs cannot be merged")
    if a["holdout_opened"]["opened"] and b["holdout_opened"]["opened"] and a["holdout_opened"] != b["holdout_opened"]:
        raise ReportError("reports that cite different holdout log entries cannot be merged")
    opened = a["holdout_opened"] if a["holdout_opened"]["opened"] else b["holdout_opened"]
    return {"schema": a["schema"], "run": a["run"], "env": a["env"], "holdout_opened": opened,
            **{part: _union(part, a[part], b[part]) for part in KEYS}}


def _union(part: str, a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """꼭 같은 줄은 하나로 두고, 열쇠가 같은데 값이 다른 줄은 거부한다(조용히 겹치지 않게)."""
    out: dict[tuple, dict[str, Any]] = {}
    for row in a + b:
        key = tuple(row[k] for k in KEYS[part])
        if key in out and out[key] != row:
            raise ReportError(f"conflicting {part} rows: {'/'.join(map(str, key))}")
        out.setdefault(key, row)
    return list(out.values())


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, list):
        return "[" + ", ".join(_cell(v) for v in value) + "]"
    return str(value)


def render_md(report: dict[str, Any]) -> str:
    """report.json에서 고정 서식(한국어 표 머리)으로만 만든다."""
    run = report["run"]
    lines = [f"# 평가 보고서 {run['name']}", "", f"- 날짜: {run['date']}", f"- 하네스 커밋: {run['harness_commit']}",
             f"- 엔진 커밋: {run['engine_commit']}",
             f"- 보류 세트를 열었나: {'예' if report['holdout_opened']['opened'] else '아니오'}", "",
             "## 결과", "", "| 데이터셋 | 분할 | 도구 | 판 | 설정 | 지표 | 집계 | 값 | 95% 구간 | n | 실패 | 표시 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["results"]:
        lines.append("| " + " | ".join(_cell(r[k]) for k in (
            "dataset", "split", "tool", "tool_version", "config", "metric", "agg", "value", "ci95", "n", "n_failed",
        )) + f" | {', '.join(r['flags']) or '-'} |")
    lines += ["", "## 제외 장부", "", "| 데이터셋 | 분할 | 도구 | 단위 | 전체 | 채점 | 제외 |", "|---|---|---|---|---|---|---|"]
    for row in report["ledger"]:
        excluded = ", ".join(f"{k} {v}" for k, v in sorted(row["excluded"].items())) or "-"
        lines.append(f"| {row['dataset']} | {row['split']} | {row['tool']} | {row['unit']} | {row['total']} | "
                     f"{row['scored']} | {excluded} |")
    lines += ["", "## 실패", "", "| 데이터셋 | 분할 | 도구 | 문서 | 상태 |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(f[k] for k in ("dataset", "split", "tool", "doc", "status")) + " |"
              for f in report["failures"]]
    return "\n".join(lines) + "\n"


def _named(registry: Registry, row: dict[str, Any]) -> Source:
    """줄에 적힌 데이터셋 이름·분할의 출처(표에 없는 이름이면 ProvenanceError)."""
    try:
        return registry.source(row["dataset"], row["split"])
    except RegistryError:
        raise provenance.ProvenanceError(f"report: {row['dataset']}/{row['split']} is not a registered split") from None


def _sealed(registry: Registry, source: Source) -> bool:
    try:
        return source.split in registry.by_identity(source.dataset).sealed_splits
    except RegistryError:
        return False  # 모르는 identity는 provenance.check_report가 ProvenanceError로 거부한다


def _check_opened(report: dict[str, Any], registry: Registry, holdout_log: Path, sources: set[Source]) -> None:
    """보류 세트를 열었다면 인용한 기록이 이 보고서의 봉인 출처(데이터셋·분할·identity)와 엔진 커밋에 맞아야 한다."""
    opened = report["holdout_opened"]
    if opened["opened"] != (opened["entry"] is not None):
        raise ReportError("holdout_opened.entry must be set exactly when the holdout was opened")
    if not opened["opened"]:
        return
    if not any(_sealed(registry, s) for s in sources):
        raise ReportError("holdout_opened is set but the report has no sealed sources")
    entry = next((e for e in holdout.read_log(holdout_log) if e["entry"] == opened["entry"]), None)
    if entry is None:
        raise ReportError("holdout_opened.entry is not in the holdout log")
    for source in sources:
        dataset = registry.by_identity(source.dataset)
        if source.split not in dataset.sealed_splits:
            continue
        if (entry["dataset"], entry["split"]) != (dataset.name, source.split):
            raise ReportError("the holdout log entry was opened for another dataset or split")
        if entry["identity"] != source.dataset:
            raise ReportError("the holdout log entry was opened for other dataset files (identity differs)")
    if entry["engine_commit"] != report["run"]["engine_commit"]:
        raise ReportError("the holdout log entry was opened for another engine commit")


def write(report: dict[str, Any], out_dir: Path, registry: Registry, holdout_log: Path) -> None:
    """검사를 모두 통과하면 out_dir/report.json·report.md를 쓴다. 결과 줄은 적힌 데이터셋·분할이 제 출처에 들어야
    하고, 장부·실패 줄은 적힌 데이터셋·분할을 출처로 같은 검사를 받는다."""
    guard.check(report, guard.REPORT)
    check_ledger(report["ledger"])
    check_unique("results", report["results"])
    check_unique("failures", report["failures"])
    sources = set(provenance.sources_of(report["results"]))
    for row in report["results"]:
        named, carried = _named(registry, row), provenance.sources_of([row])
        if named not in carried:
            raise provenance.ProvenanceError(f"report: a {row['dataset']}/{row['split']} row has other sources")
        # 봉인 출처를 단 값은 그 봉인 분할로 적혀야 한다(보류 값이 report.md에 "dev"로 찍히지 않게)
        if any(s != named and _sealed(registry, s) for s in carried):
            raise provenance.ProvenanceError(
                f"report: a {row['dataset']}/{row['split']} row carries sealed sources under another label")
    sources |= {_named(registry, row) for row in report["ledger"] + report["failures"]}
    provenance.check_report(sources, registry, report["holdout_opened"]["opened"])  # 모르는 identity는 여기서
    _check_opened(report, registry, holdout_log, sources)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=1) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_md(report), encoding="utf-8")
