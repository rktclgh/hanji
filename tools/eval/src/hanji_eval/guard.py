"""숫자만 보고서: 필드마다 허용하는 꼴(열거값·해시·숫자·정해진 id 패턴)을 정해 두고 맞지 않으면 쓰지 않는다. 자유 문자열
필드는 없다. 표준 출력·로그도 이 검사를 거친 값만 찍는다(emit)."""

import json
import math
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

Spec = Callable[[Any, str], None]


class GuardError(ValueError):
    """값이 필드 스키마에 맞지 않는다. 메시지에는 필드 경로만 든다(값은 넣지 않는다)."""


def _fail(path: str, what: str) -> None:
    raise GuardError(f"{path}: {what}")


def enum(*values: str) -> Spec:
    allowed = frozenset(values)

    def check(value: Any, path: str) -> None:
        if not isinstance(value, str) or value not in allowed:
            _fail(path, "not an allowed value")
    return check


def pattern(regex: str) -> Spec:
    compiled = re.compile(regex)

    def check(value: Any, path: str) -> None:
        if not isinstance(value, str) or not value.isascii() or compiled.fullmatch(value) is None:
            _fail(path, "does not match the field pattern")
    return check


def number(lo: float | None = None, hi: float | None = None) -> Spec:
    def check(value: Any, path: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            _fail(path, "not a finite number")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            _fail(path, "number out of range")
    return check


def integer(lo: int = 0) -> Spec:
    def check(value: Any, path: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < lo:
            _fail(path, f"not an integer >= {lo}")
    return check


def boolean() -> Spec:
    def check(value: Any, path: str) -> None:
        if not isinstance(value, bool):
            _fail(path, "not a boolean")
    return check


def optional(spec: Spec) -> Spec:
    def check(value: Any, path: str) -> None:
        if value is not None:
            spec(value, path)
    return check


def array(spec: Spec, min_len: int = 0, max_len: int = 1_000_000) -> Spec:
    def check(value: Any, path: str) -> None:
        if not isinstance(value, list) or not min_len <= len(value) <= max_len:
            _fail(path, "not a list of the allowed length")
        for i, item in enumerate(value):
            spec(item, f"{path}[{i}]")
    return check


def obj(fields: Mapping[str, Spec]) -> Spec:
    def check(value: Any, path: str) -> None:
        if not isinstance(value, dict) or set(value) != set(fields):
            _fail(path, "keys differ from the schema")
        for key, spec in fields.items():
            spec(value[key], f"{path}.{key}")
    return check


def mapping(key: Spec, value_spec: Spec) -> Spec:
    def check(value: Any, path: str) -> None:
        if not isinstance(value, dict):
            _fail(path, "not a mapping")
        for k, v in value.items():
            key(k, f"{path}<key>")
            value_spec(v, f"{path}.{k}")
    return check


def check(value: Any, spec: Spec) -> None:
    spec(value, "$")


def emit(record: Any, spec: Spec) -> None:
    """검사를 통과한 값만 한 줄 JSON으로 표준 출력에 찍는다."""
    check(record, spec)
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":")), flush=True)


# 정해진 id·값 패턴(스펙 §7.3)
DOC = r"(pdf|hwp|hwpx|docx|pptx)-\d{4}|[0-9a-f]{16}|extra-[0-9a-f]{12}"
BUNDLE = r"grp-\d{4}"
PUBLISHER = r"pub-\d{3}"
SHA256 = r"[0-9a-f]{64}"
COMMIT = r"[0-9a-f]{40}"
VERSION = r"\d+(\.\d+){0,3}([.+-][0-9a-z]{1,16})?"
DATE = r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?)?"
SLUG = r"(?=[a-z0-9-]*[a-z])[a-z0-9][a-z0-9-]{0,47}"  # 글자가 하나는 있어야 한다(전화번호 꼴 거부)
RULE = r"[A-Z]+_V\d+"
MASK = r"mask\d+"
CODE = r"[a-z][a-z0-9_]{0,63}"
ERROR_CLASS = r"[A-Z][A-Za-z0-9_]{0,63}"
CPU = (r"Apple M[1-9]( Pro| Max| Ultra)?|Intel\(R\) (Core|Xeon)\(TM\) [A-Za-z0-9-]{1,24}( CPU)?( @ \d\.\d{2}GHz)?"
       r"|AMD (Ryzen|EPYC) [A-Za-z0-9 -]{1,40}|other")

DATASETS = ("hanji-ko-v1", "hanji-ko-scan-v1", "dp-bench", "tableeval-v0", "borderless-v0", "layout-v0",
            "corpus-big")
SPLITS = ("dev", "holdout", "all")
SPLIT_FILES = ("hanji-ko-v1",)
TOOLS = ("hanji", "docling", "opendataloader")
CONFIGS = ("default", "core", "ocr-ko", "local")
FORMATS = ("pdf", "hwp", "hwpx", "docx", "pptx")
STATUSES = ("created", "parse_error", "timeout", "crashed")
METRICS = (
    "nid", "nid_ws", "cer", "hangul_precision", "hangul_recall", "hangul_f1",
    "number_exact", "number_precision", "number_recall", "number_unverified_rate",
    "table_precision", "table_recall", "teds", "teds_s", "teds_matched", "teds_s_matched", "grits_con", "grits_top",
    "table_perfect", "heading_precision", "heading_recall", "heading_f1", "heading_level_agreement",
    "kendall_tau", "order_gap", "header_footer_precision", "header_footer_recall",
    "missing_rate", "missing_recorded_rate", "missing_unrecorded_rate",
    "outside_gt_rate", "insertion_rate", "duplicate_rate", "ambiguous_rate",
    "ece", "ece_same_value", "brier", "aurc", "seconds_per_page", "max_rss_mib", "failure_rate",
)
AGGS = ("doc_mean", "char_weighted", "micro", "median", "p95", "max", "rate", "count")
FLAGS = ("few_clusters", "one_sided_upper", "one_sided_lower", "tau_undefined", "no_units")
UNITS = ("doc", "page", "block", "table")
# 1단계(도구와 무관한) 제외: 모든 도구가 같은 수를 가져야 한다(§6.5 제외 장부)
MASK_REASONS = (
    "excluded_no_provenance", "revision_unverified", "excluded_revision_mismatch", "excluded_native_ledger",
    "excluded_distribution", "excluded_field", "excluded_ruby", "excluded_split_table", "excluded_nested_table",
    "excluded_box_table", "excluded_equation", "excluded_page_furniture", "unverified", "layer_outside_gt", "pii",
)
PREDICTION_REASONS = ("outside_gt", "ambiguous")
# 엔진 처리 이력 사유(엔진 상수, test_bigrun이 엔진 소스와 대조한다)와 그 밖 = other
REASONS = (
    "alternate_content_skipped", "borderless_table", "borderless_table_failed", "chart_text", "comments_skipped",
    "coverage_mismatch", "distribution_preview_only", "embedded_object", "equation_script", "equation_text",
    "field_unterminated", "figure_image_budget", "figure_image_linked", "figure_image_missing",
    "figure_image_not_stored", "figure_image_too_large", "figure_image_unreadable", "figure_image_unsupported",
    "hidden_comment", "hidden_slide", "hidden_text", "invalid_utf16", "layout_table_not_attempted",
    "masterpage_text_skipped", "master_text_skipped", "merged_cell_text_kept", "nested_table_flattened",
    "record_inconsistent", "shape_text", "smartart_text", "speaker_notes_skipped", "special_note_skipped",
    "spine_item_skipped", "symbol_char", "table_cells_padded", "table_in_figure_dropped", "table_invalid",
    "table_merge_repaired", "table_too_large", "tracked_deletion_skipped", "unassigned_text", "unknown_control",
    "unknown_control_char", "unknown_inline_element", "unknown_record", "unreferenced_part_skipped",
    "unreliable_text_layer_kept", "unreliable_text_layer_ocr", "other",
)
# 작업자가 남기는 예외 클래스(hanji.errors + 흔한 내장 예외 + 작업자 규약 위반)와 그 밖 = other
ERRORS = (
    "HanjiError", "UnsupportedFormat", "ParseError", "DocumentNotFound", "VersionNotFound", "StoreConflict",
    "VlmUnavailable", "AssetNotFound", "ModelError", "FileNotFoundError", "PermissionError", "IsADirectoryError",
    "OSError", "MemoryError", "RecursionError", "ValueError", "KeyError", "IndexError", "TypeError",
    "UnicodeDecodeError", "UnicodeEncodeError", "OverflowError", "ZeroDivisionError", "AssertionError",
    "RuntimeError", "NotImplementedError", "WorkerProtocolError", "other",
)
EXCLUSIONS = MASK_REASONS + PREDICTION_REASONS

ENV = obj({
    "system": enum("Darwin", "Linux", "Windows"),
    "machine": enum("arm64", "x86_64", "AMD64", "aarch64"),
    "cpu_model": pattern(CPU),
    "cores": integer(1),
    "memory_bytes": optional(integer(1)),
    "python": pattern(VERSION),
    "threads": mapping(enum("HANJI_ORT_THREADS", "OMP_NUM_THREADS"), integer(1)),
    "tools": array(obj({"tool": enum(*TOOLS), "version": pattern(VERSION), "models": array(pattern(SHA256))})),
    "datasets": array(obj({"dataset": enum(*DATASETS), "version": pattern(VERSION), "identity": pattern(SHA256)})),
    "split": optional(enum(*SPLIT_FILES)),
})
SOURCE = obj({"dataset": pattern(SHA256), "split": enum(*SPLITS)})
REPORT = obj({
    "schema": enum("hanji-eval-report/1"),
    "run": obj({
        "name": pattern(SLUG), "date": pattern(DATE), "harness_commit": pattern(COMMIT),
        "engine_commit": pattern(COMMIT),
        "rules": obj({"norm": optional(pattern(RULE)), "num": optional(pattern(RULE)),
                      "adapter": optional(pattern(RULE)), "mask": optional(pattern(MASK)),
                      "truth_version": optional(integer(1))}),
    }),
    "env": ENV,
    "ledger": array(obj({
        "dataset": enum(*DATASETS), "split": enum(*SPLITS), "tool": enum(*TOOLS), "unit": enum(*UNITS),
        "total": integer(), "scored": integer(), "excluded": mapping(enum(*EXCLUSIONS), integer()),
    })),
    "results": array(obj({
        "dataset": enum(*DATASETS), "split": enum(*SPLITS), "tool": enum(*TOOLS), "tool_version": pattern(VERSION),
        "config": enum(*CONFIGS), "metric": enum(*METRICS), "agg": enum(*AGGS), "value": optional(number()),
        "ci95": optional(array(number(), 2, 2)), "n": integer(), "n_failed": integer(),
        "flags": array(enum(*FLAGS)), "sources": array(SOURCE, 1),
    })),
    "failures": array(obj({"dataset": enum(*DATASETS), "split": enum(*SPLITS), "tool": enum(*TOOLS),
                           "doc": pattern(DOC), "status": enum(*STATUSES[1:])})),
    "holdout_opened": obj({"opened": boolean(), "entry": optional(integer(1))}),
})


def machine_env(threads: Mapping[str, str]) -> dict[str, Any]:
    """이 컴퓨터의 장비 정보(ENV의 앞 필드). CPU 이름은 정해진 꼴에 맞을 때만, 아니면 other."""
    brand = ""
    if sys.platform == "darwin":
        brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                               encoding="utf-8").stdout.strip()
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as f:
                brand = next((ln.split(":", 1)[1].strip() for ln in f if ln.startswith("model name")), "")
        except OSError:
            brand = ""
    try:
        pattern(CPU)(brand, "$")
    except GuardError:
        brand = "other"
    memory = None
    if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return {
        "system": platform.system(), "machine": platform.machine(), "cpu_model": brand,
        "cores": os.cpu_count() or 1, "memory_bytes": memory, "python": platform.python_version(),
        "threads": {k: int(v) for k, v in threads.items() if v.isdigit() and int(v) > 0},
    }
