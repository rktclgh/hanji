"""의존성 라이선스 검사: 잠금(uv.lock)의 모든 배포판이 licenses.toml에 같은 판으로 있고, 배포판이 밝힌 식(declared)과
묶인 구성 요소(bundled)가 정한 규칙을 넘지 않는지 본다. OR은 하나라도 허용이면, AND·WITH는 모두 허용이어야 통과.
모르는 값·읽히지 않는 식은 실패다. 묶인 구성 요소의 status는 정해진 여섯 가지뿐이다(컨트롤러 판정 R4a).
wheel_tags는 휠 WHEEL 파일 Tag: 값에 맞추는 fnmatch 패턴이다: 배포판 줄은 묶인 줄을 적은 휠, 묶인 줄은 그 줄이 맞는 휠.
check()는 플랫폼을 보지 않고 모든 줄(플랫폼의 합집합)을 검사한다. 플랫폼별로 거르는 것은 설치 환경 시험뿐이다.
설치된 플랫폼 휠은 모두 적은 휠(wheel_tags)이 맞아야 하고, 묶인 줄이 없으면 no_bundled에 그 근거를 적는다."""

import collections
import fnmatch
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
UNKNOWN = frozenset({"UNKNOWN", "NOASSERTION", "NONE"})
STATUSES = ("allowed", "or-alternative", "runtime-exception", "weak-copyleft-library", "optional-not-shipped",
            "build-only")
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
    wheel_tags: tuple[str, ...] = ()  # 비면 모든 플랫폼


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    version: str
    declared: str
    basis: str
    bundled: tuple[Bundled, ...] = ()
    wheel_tags: tuple[str, ...] = ()  # 묶인 줄을 적은 휠(플랫폼 휠이면 설치된 휠이 여기 맞아야 한다)
    no_bundled: str | None = None  # 플랫폼 휠에 묶인 구성 요소가 없다는 근거(bundled와 함께 쓰지 않는다)


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


def _closing(tokens: list[str], start: int) -> int:
    depth = 0
    for i in range(start, len(tokens)):
        depth += {"(": 1, ")": -1}.get(tokens[i], 0)
        if depth == 0:
            return i
    return -1


def _unwrap(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] == "(" and _closing(tokens, 0) == len(tokens) - 1:
        tokens = tokens[1:-1]
    return tokens


def alternatives(expr: str) -> list[str]:
    """맨 위 OR 갈래(AND가 먼저 묶이고 괄호 안 OR은 나누지 않는다). 바깥 괄호를 벗겨 토큰을 빈칸 하나로 잇는다."""
    license_ids(expr)  # 읽히지 않는 식은 SpdxError
    parts: list[list[str]] = [[]]
    depth = 0
    for tok in _unwrap(_tokens(expr)):
        depth += {"(": 1, ")": -1}.get(tok, 0)
        if tok == "OR" and depth == 0:
            parts.append([])
        else:
            parts[-1].append(tok)
    return [" ".join(_unwrap(part)) for part in parts]


def _runtime_exception(spdx: str) -> bool:
    m = re.fullmatch(r"(\S+) WITH (\S+)", spdx.strip())
    if m is None or m.group(2) not in RUNTIME_EXCEPTIONS or not m.group(1).startswith("GPL-"):
        return False  # GCC·Classpath 예외는 GPL에만 붙는다(AGPL·SSPL 등은 받지 않는다)
    return m.group(2) != "Classpath-exception-2.0" or m.group(1).startswith("GPL-2.0")


def wheel_tags(wheel_text: str) -> list[str]:
    """WHEEL 파일의 Tag: 값."""
    return [line.split(":", 1)[1].strip() for line in wheel_text.splitlines() if line.startswith("Tag:")]


def platform_specific(tags: list[str]) -> bool:
    return any(not t.endswith("-none-any") for t in tags)


def _matches(patterns: tuple[str, ...], tags: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(t, p) for p in patterns for t in tags)


def applies(b: Bundled, tags: list[str]) -> bool:
    """묶인 줄이 이 휠에 맞는가(wheel_tags가 없으면 모든 휠)."""
    return not b.wheel_tags or _matches(b.wheel_tags, tags)


def recorded_for(entry: Entry, tags: list[str]) -> bool:
    """배포판의 묶인 줄을 이 휠에서 적었는가(순수 파이썬 휠은 늘 참)."""
    return not platform_specific(tags) or _matches(entry.wheel_tags, tags)


def sbom_license(component: dict) -> str:
    """SBOM 구성 요소의 라이선스 항목들을 AND로 이은 식(항목이 없으면 빈 문자열)."""
    return " AND ".join(item.get("expression") or item.get("license", {}).get("id", "")
                        for item in component.get("licenses", ()))


def sbom_problems(entry: Entry, tags: list[str], components: list[dict]) -> list[str]:
    """설치된 휠의 SBOM 구성 요소가 이 휠에 맞는 묶인 줄과 꼭 같은지(이름 줄은 이름·식, 나머지는 식별 수)."""
    if not recorded_for(entry, tags):
        return [f"{entry.name}: no bundled rows recorded for wheel tags {', '.join(tags)}"]
    rows = [b for b in entry.bundled if applies(b, tags)]
    named = {b.sbom_name: b for b in rows if b.sbom_name}
    problems = [f"{entry.name}: sbom component {n} not found"
                for n in sorted(named.keys() - {c["name"] for c in components})]
    for c in components:
        b, lic = named.get(c["name"]), sbom_license(c)
        if b is not None and lic and lic != b.spdx:
            problems.append(f"{entry.name}: sbom component {c['name']} is {lic}, table says {b.spdx}")
    rest = collections.Counter(sbom_license(c) for c in components if c["name"] not in named)
    groups: collections.Counter[str] = collections.Counter()
    for b in rows:
        groups[b.spdx] += b.sbom_group or 0
    groups = +groups
    if rest != groups:
        problems.append(f"{entry.name}: sbom licence groups {dict(sorted(rest.items()))} != table "
                        f"{dict(sorted(groups.items()))}")
    return problems


def bundled_problem(b: Bundled) -> str | None:
    """묶인 구성 요소 한 줄의 문제(없으면 None)."""
    if b.status not in STATUSES:
        return f"status {b.status} is not one of the fixed statuses"
    if not b.component.strip() or not b.evidence.strip():
        return "component and evidence are required"
    if b.status == "allowed":
        return None if allowed(b.spdx) else f"license {b.spdx} not allowed"
    if b.status == "or-alternative":
        options = alternatives(b.spdx)
        if len(options) < 2 or b.branch is None:
            return "or-alternative needs an OR expression and the chosen branch"
        # 고른 쪽은 맨 위 OR 갈래 하나 전체여야 한다: "MIT AND AGPL OR GPL"의 MIT는 갈래가 아니다
        if " ".join(_unwrap(_tokens(b.branch))) not in options or not allowed(b.branch):
            return f"branch {b.branch} is not an allowed alternative of {b.spdx}"
        return None
    if b.status == "runtime-exception":
        if not _runtime_exception(b.spdx):
            return f"{b.spdx} is not a copyleft license with a runtime library exception"
        return None
    if b.status == "weak-copyleft-library":
        ids = license_ids(b.spdx)
        if not ids or not all(i.startswith(WEAK) for i in ids):
            return f"{b.spdx} is not a weak copyleft (LGPL·MPL) license"
        return None
    unknown = sorted(set(license_ids(b.spdx)) & UNKNOWN)  # 읽히지 않는 식은 SpdxError, 모르는 값은 실패
    if unknown:
        return f"license {unknown[0]} is not a known license"
    if b.status == "optional-not-shipped":
        return None if b.absent else "optional-not-shipped needs absent patterns that match no installed file"
    return None  # build-only: 근거(evidence)만 있으면 된다


def _lock_versions(lock: Path) -> dict[str, list[str]]:
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for p in data.get("package", []):
        out.setdefault(normalize_name(p["name"]), []).append(p.get("version", ""))
    return out


def lock_packages(lock: Path) -> dict[str, str]:
    """uv.lock의 배포판 → 판(정규화한 이름). 한 이름이 여러 판이면 check()가 실패로 알린다."""
    return {name: versions[-1] for name, versions in _lock_versions(lock).items()}


def _bundled(raw: dict) -> Bundled:
    return Bundled(raw.get("component", ""), raw.get("spdx", ""), raw.get("status", ""), raw.get("evidence", ""),
                   raw.get("branch"), tuple(raw.get("absent", ())), raw.get("sbom_name"), raw.get("sbom_group"),
                   tuple(raw.get("wheel_tags", ())))


def load_table(path: Path) -> list[Entry]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [Entry(normalize_name(p["name"]), p["version"], p["declared"], p["basis"],
                  tuple(_bundled(b) for b in p.get("bundled", ())), tuple(p.get("wheel_tags", ())),
                  p.get("no_bundled"))
            for p in data.get("package", [])]


def _tags_ok(tags: tuple) -> bool:
    return all(isinstance(t, str) and t.strip() for t in tags)


def check(lock: Path, table: Path) -> list[str]:
    """문제 목록(빈 목록이면 통과). 문제 문장에는 배포판·구성 요소 이름·판·식만 든다."""
    versions = _lock_versions(lock)
    locked = lock_packages(lock)
    entries = load_table(table)
    problems: list[str] = [f"{n}: locked more than once ({', '.join(v)})" for n, v in sorted(versions.items())
                           if len(v) > 1]
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
        if not _tags_ok(entry.wheel_tags):
            problems.append(f"{name}: wheel_tags must be non-empty strings")
        if entry.no_bundled is not None:
            if not isinstance(entry.no_bundled, str) or not entry.no_bundled.strip():
                problems.append(f"{name}: no_bundled must state its evidence")
            elif entry.bundled:
                problems.append(f"{name}: no_bundled together with bundled rows")
            elif not entry.wheel_tags:
                problems.append(f"{name}: no_bundled needs the wheel_tags it was recorded from")
        try:
            if not allowed(entry.declared):
                problems.append(f"{name}: license {entry.declared} not allowed")
        except SpdxError as error:
            problems.append(f"{name}: {error}")
        for b in entry.bundled:  # 한 줄이 읽히지 않아도 나머지 줄은 계속 본다
            try:
                problem = bundled_problem(b)
            except SpdxError as error:
                problem = str(error)
            if problem is None and not _tags_ok(b.wheel_tags):
                problem = "wheel_tags must be non-empty strings"
            if problem is None and not set(b.wheel_tags) <= set(entry.wheel_tags):
                extra = ", ".join(sorted(set(b.wheel_tags) - set(entry.wheel_tags)))
                problem = f"wheel_tags {extra} not among the wheel_tags the package was recorded from"
            if problem:
                problems.append(f"{name}: bundled {b.component}: {problem}")
    return problems
