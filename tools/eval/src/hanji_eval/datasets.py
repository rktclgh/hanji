"""데이터셋 표(datasets.toml): 고정 판·파일 sha256·라이선스·공개 가능 여부·쓰임새. 데이터셋은 이름이 아니라 파일 sha256
묶음(identity)으로 식별한다: 같은 파일을 다른 이름으로 다시 적는 별칭은 읽을 때 거부한다."""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROLES = frozenset({"selection", "calibration", "diagnostic", "bigrun"})
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SPLIT = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(ValueError):
    """datasets.toml이 규칙에 맞지 않는다."""


@dataclass(frozen=True, slots=True)
class Source:
    """값의 출처 하나: 데이터셋 identity(sha256 묶음)와 분할."""

    dataset: str
    split: str


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    sha256: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    version: str
    license: str
    license_basis: str
    publishable: bool
    roles: frozenset[str]
    splits: tuple[str, ...]
    sealed_splits: frozenset[str]
    use: str
    revision: str | None
    files: tuple[FileEntry, ...]

    @property
    def identity(self) -> str:
        """파일 sha256을 정렬해 줄로 이은 것의 sha256(경로·이름과 무관)."""
        return hashlib.sha256("\n".join(sorted(f.sha256 for f in self.files)).encode("ascii")).hexdigest()


def _relative(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or not p.parts or ".." in p.parts or "\\" in path:
        raise RegistryError(f"file path must be relative without '..': {path!r}")
    return str(p)


def _dataset(raw: dict) -> Dataset:
    name = raw.get("name", "")
    if not _NAME.match(name):
        raise RegistryError(f"bad dataset name: {name!r}")
    roles = frozenset(raw.get("roles", ()))
    if not roles <= ROLES:
        raise RegistryError(f"{name}: unknown roles {sorted(roles - ROLES)}")
    splits = tuple(raw.get("splits", ()))
    sealed = frozenset(raw.get("sealed_splits", ()))
    if not splits or not all(_SPLIT.match(s) for s in splits) or len(set(splits)) != len(splits):
        raise RegistryError(f"{name}: bad splits {list(splits)}")
    if not sealed <= set(splits):
        raise RegistryError(f"{name}: sealed splits {sorted(sealed - set(splits))} are not splits")
    revision = raw.get("revision")
    files = []
    for f in raw.get("files", ()):
        sha, url = f.get("sha256", ""), f.get("url")
        if not _SHA.match(sha):
            raise RegistryError(f"{name}: bad sha256 for {f.get('path')!r}")
        if url is not None and (not revision or revision not in url):
            raise RegistryError(f"{name}: url is not pinned to the dataset revision: {url!r}")
        files.append(FileEntry(_relative(f.get("path", "")), sha, url))
    if not files:
        raise RegistryError(f"{name}: no files")
    if len({f.path for f in files}) != len(files):
        raise RegistryError(f"{name}: duplicate file paths")
    if not isinstance(raw.get("publishable"), bool):
        raise RegistryError(f"{name}: publishable must be true or false")
    for field in ("version", "license", "license_basis", "use"):  # 스펙 §3.2: 판·라이선스와 그 근거·쓰임새
        if not str(raw.get(field, "")).strip():
            raise RegistryError(f"{name}: {field} is required")
    return Dataset(name, str(raw.get("version", "")), raw.get("license", ""), raw.get("license_basis", ""),
                   raw["publishable"], roles, splits, sealed, raw.get("use", ""), revision, tuple(files))


class Registry:
    def __init__(self, datasets: list[Dataset]) -> None:
        self._by_name: dict[str, Dataset] = {}
        self._by_identity: dict[str, Dataset] = {}
        for d in datasets:
            if d.name in self._by_name:
                raise RegistryError(f"dataset listed twice: {d.name}")
            other = self._by_identity.get(d.identity)
            if other is not None:
                raise RegistryError(f"{d.name}: same files as {other.name} (alias)")
            self._by_name[d.name] = d
            self._by_identity[d.identity] = d

    @classmethod
    def from_text(cls, text: str) -> "Registry":
        return cls([_dataset(raw) for raw in tomllib.loads(text).get("dataset", [])])

    @classmethod
    def load(cls, path: Path) -> "Registry":
        return cls.from_text(path.read_text(encoding="utf-8"))

    def get(self, name: str) -> Dataset:
        try:
            return self._by_name[name]
        except KeyError:
            raise RegistryError(f"unknown dataset: {name}") from None

    def by_identity(self, identity: str) -> Dataset:
        try:
            return self._by_identity[identity]
        except KeyError:
            raise RegistryError(f"unknown dataset identity: {identity}") from None

    def source(self, name: str, split: str) -> Source:
        dataset = self.get(name)
        if split not in dataset.splits:
            raise RegistryError(f"{name}: unknown split {split!r}")
        return Source(dataset.identity, split)

    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(dataset: Dataset, root: Path) -> None:
    """로컬 파일(저장소 뿌리 기준 경로)의 sha256이 표와 같은지 본다. 다르면 RegistryError."""
    for f in dataset.files:
        path = root / f.path
        if not path.is_file():
            raise RegistryError(f"{dataset.name}: missing file {f.path}")
        if file_sha256(path) != f.sha256:
            raise RegistryError(f"{dataset.name}: sha256 mismatch for {f.path}")
