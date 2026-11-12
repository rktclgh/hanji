"""의존성 라이선스 검사: 잠금(uv.lock)의 모든 배포판이 licenses.toml에 같은 판으로 있고, 배포판이 밝힌 식(declared)과
묶인 구성 요소(bundled)가 정한 규칙을 넘지 않는지 본다. OR은 하나라도 허용이면, AND·WITH는 모두 허용이어야 통과.
모르는 값·읽히지 않는 식은 실패다. 묶인 구성 요소의 status는 정해진 여섯 가지뿐이다(컨트롤러 판정 R4a)."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# 스펙 §3.2 허용 목록 + R2(numpy·Pillow) + R3(글꼴 OFL) + R4a(묶인 구성 요소) + 실측으로 찾은 허용형
ALLOWED = frozenset({
    "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0",
    "0BSD", "Zlib", "CC0-1.0", "MIT-CMU",
    "OFL-1.1", "BSL-1.0", "Unicode-3.0", "Unicode-DFS-2016", "FTL", "IJG", "libpng-2.0", "libtiff",
    "LicenseRef-AGG-2.3",
    "BSD-3-Clause-Open-MPI", "Unlicense", "LicenseRef-Public-Domain", "NAIST-2003", "CC-BY-4.0", "bzip2-1.0.6", "TCL",
    "MIT-open-group", "X11",
})
ALLOWED_EXCEPTIONS = frozenset({"LLVM-exception"})
RUNTIME_EXCEPTIONS = frozenset({"GCC-exception-2.0", "GCC-exception-3.1", "Classpath-exception-2.0"})
STATUSES = ("allowed", "or-alternative", "runtime-exception", "weak-copyleft-library", "optional-not-shipped",
            "build-only")
COPYLEFT = ("AGPL-", "GPL-", "LGPL-", "MPL-", "EPL-", "CDDL-", "EUPL-", "OSL-", "CPL-", "SSPL-")
WEAK = ("LGPL-", "MPL-")
_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+-]+)")


class SpdxError(ValueError):
    """SPDX 식을 읽을 수 없다."""


@dataclass(frozen=True, slots=True)
class Bundled:
    component: str
    spdx: str
    status: str
    evidence: str
    branch: str | None = None
    absent: tuple[str, ...] = ()
    sbom_name: str | None = None
    sbom_group: int | None = None


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    version: str
    declared: str
    basis: str
    bundled: tuple[Bundled, ...] = ()


def normalize_name(name: str) -> str:
    """PEP 503 이름 정규화."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _tokens(expr: str) -> list[str]:
    out, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if m is None:
            if expr[pos:].strip():
                raise SpdxError(f"unreadable SPDX expression: {expr!r}")
            break
        out.append(m.group(1))
        pos = m.end()
    if not out:
        raise SpdxError("empty SPDX expression")
    return out


def _evaluate(expr: str, ok) -> bool:
    """WITH > AND > OR, 괄호. ok(license_id, exception | None) -> bool."""
    tokens = _tokens(expr)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise SpdxError(f"unexpected end of SPDX expression: {expr!r}")
        pos += 1
        return tokens[pos - 1]

    def atom() -> bool:
        tok = take()
        if tok == "(":
            value = either()
            if take() != ")":
                raise SpdxError(f"unbalanced parentheses: {expr!r}")
            return value
        if tok in ("AND", "OR", "WITH", ")"):
            raise SpdxError(f"unexpected {tok!r} in SPDX expression: {expr!r}")
        exception = None
        if peek() == "WITH":
            take()
            exception = take()
        return ok(tok, exception)

    def both() -> bool:
        value = atom()
        while peek() == "AND":
            take()
            value = atom() and value
        return value

    def either() -> bool:
        value = both()
        while peek() == "OR":
            take()
            value = both() or value
        return value

    result = either()
    if pos != len(tokens):
        raise SpdxError(f"trailing tokens in SPDX expression: {expr!r}")
    return result


def allowed(expr: str) -> bool:
    """SPDX 식이 허용 목록 안이면 참."""
    return _evaluate(expr, lambda lic, exc: lic in ALLOWED and (exc is None or exc in ALLOWED_EXCEPTIONS))


def license_ids(expr: str) -> list[str]:
    """식에 든 라이선스 id(예외 빼고), 나온 차례."""
    out: list[str] = []
    _evaluate(expr, lambda lic, exc: out.append(lic) or True)
    return out


def _copyleft(lic: str) -> bool:
    return lic.startswith(COPYLEFT)


def bundled_problem(b: Bundled) -> str | None:
    """묶인 구성 요소 한 줄의 문제(없으면 None)."""
    if b.status not in STATUSES:
        return f"status {b.status} is not one of the fixed statuses"
    if not b.component.strip() or not b.evidence.strip():
        return "component and evidence are required"
    if b.status == "allowed":
        return None if allowed(b.spdx) else f"license {b.spdx} not allowed"
    if b.status == "or-alternative":
        if " OR " not in f" {b.spdx} " or b.branch is None:
            return "or-alternative needs an OR expression and the chosen branch"
        if b.branch not in license_ids(b.spdx) or not allowed(b.branch):
            return f"branch {b.branch} is not an allowed alternative of {b.spdx}"
        return None
    if b.status == "runtime-exception":
        m = re.fullmatch(r"(\S+) WITH (\S+)", b.spdx.strip())
        if m is None or not _copyleft(m.group(1)) or m.group(2) not in RUNTIME_EXCEPTIONS:
            return f"{b.spdx} is not a copyleft license with a runtime library exception"
        return None
    if b.status == "weak-copyleft-library":
        ids = license_ids(b.spdx)
        if not ids or not all(i.startswith(WEAK) for i in ids):
            return f"{b.spdx} is not a weak copyleft (LGPL·MPL) license"
        return None
    if b.status == "optional-not-shipped":
        return None if b.absent else "optional-not-shipped needs absent patterns that match no installed file"
    return None  # build-only: 근거(evidence)만 있으면 된다


def lock_packages(lock: Path) -> dict[str, str]:
    """uv.lock의 배포판 → 판(정규화한 이름)."""
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    return {normalize_name(p["name"]): p.get("version", "") for p in data.get("package", [])}


def _bundled(raw: dict) -> Bundled:
    return Bundled(raw.get("component", ""), raw.get("spdx", ""), raw.get("status", ""), raw.get("evidence", ""),
                   raw.get("branch"), tuple(raw.get("absent", ())), raw.get("sbom_name"), raw.get("sbom_group"))


def load_table(path: Path) -> list[Entry]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [Entry(normalize_name(p["name"]), p["version"], p["declared"], p["basis"],
                  tuple(_bundled(b) for b in p.get("bundled", ())))
            for p in data.get("package", [])]


def check(lock: Path, table: Path) -> list[str]:
    """문제 목록(빈 목록이면 통과). 문제 문장에는 배포판·구성 요소 이름·판·식만 든다."""
    locked = lock_packages(lock)
    entries = load_table(table)
    problems: list[str] = []
    names = [e.name for e in entries]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{name}: listed more than once")
    listed = {e.name: e for e in entries}
    for name in sorted(locked.keys() - listed.keys()):
        problems.append(f"{name}: in lock but not in licenses table")
    for name in sorted(listed.keys() - locked.keys()):
        problems.append(f"{name}: in licenses table but not in lock")
    for name in sorted(locked.keys() & listed.keys()):
        entry = listed[name]
        if entry.version != locked[name]:
            problems.append(f"{name}: table version {entry.version} != lock version {locked[name]}")
        if not entry.basis.strip():
            problems.append(f"{name}: no license basis")
        try:
            if not allowed(entry.declared):
                problems.append(f"{name}: license {entry.declared} not allowed")
            for b in entry.bundled:
                problem = bundled_problem(b)
                if problem:
                    problems.append(f"{name}: bundled {b.component}: {problem}")
        except SpdxError as error:
            problems.append(f"{name}: {error}")
    return problems
