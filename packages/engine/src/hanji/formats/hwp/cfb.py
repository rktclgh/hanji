"""OLE 복합 파일(MS-CFB)을 믿을 수 없는 입력으로 읽는다(HWP 스펙 §4). 형식 무관이라 다른 OLE 형식도 쓸 수 있다.

생성자가 머리·DIFAT·FAT·디렉터리·mini FAT를 읽고, 이름 트리를 명시적 스택으로 돌아 경로 → 스트림을 만들며, 닿는 모든
스트림의 사슬을 미리 따라가 섹터 소유권(큰 섹터 표·mini 섹터 표)과 선언 크기를 확인한다(정수만 다루고 바이트는 읽지
않는다). 그래서 사슬 간 공유·순환·범위 밖 섹터·선언보다 짧은 사슬은 열 때 ParseError다. read는 정확한 경로의 스트림을
요청한 상한 + 1바이트까지만 모은다. 실패는 ParseError(원인, "{파일 이름}" 또는 "{파일 이름}:{스트림 경로}")."""

import struct
import sys
from array import array
from dataclasses import dataclass

from ...errors import ParseError

SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MAXREGSECT, DIFSECT, FATSECT, ENDOFCHAIN, FREESECT = 0xFFFFFFFA, 0xFFFFFFFC, 0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF
HEADER_DIFAT = 109  # 머리에 든 DIFAT 항목 수
ENTRY = 128  # 디렉터리 항목 크기
MINI_SHIFT = 6  # mini 섹터 64B
MINI_CUTOFF = 4096  # 이보다 작은 스트림은 mini 스트림에 있다
MAX_DIR_ENTRIES = 4096  # 디렉터리 항목 수(실측 최대 168)
EMPTY, STORAGE, STREAM, ROOT = 0, 1, 2, 5
FORBIDDEN = frozenset("/\\:!")  # 이름에 있으면 경로처럼 보인다


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    kind: int
    left: int
    right: int
    child: int
    start: int
    size: int


def _words(raw: bytes) -> array:
    """리틀 엔디언 UINT32 배열(정수 하나에 4바이트: 파이썬 정수 튜플보다 작다)."""
    out = array("I", raw[:len(raw) - len(raw) % 4])
    if sys.byteorder == "big":
        out.byteswap()
    return out


def _fold(name: str) -> tuple[int, str]:
    """CFB 이름 비교 키: 길이 먼저, 그다음 글자마다 단순 대문자 접기(한 글자가 여러 글자로 늘어나면 그대로)."""
    return len(name), "".join(c.upper() if len(c.upper()) == 1 else c for c in name)


class Compound:
    """열린 복합 파일. streams는 경로("BodyText/Section0" 꼴, 저장소 이름을 /로 이음) → 선언 크기."""

    def __init__(self, data: bytes, name: str) -> None:
        self.name = name
        self._data = data
        if len(data) < 512 or data[:8] != SIGNATURE:
            raise ParseError("not an OLE compound file (CFB signature)", name)
        major, order, shift, mini_shift = struct.unpack_from("<HHHH", data, 0x1A)
        if order != 0xFFFE:
            raise ParseError("CFB byte order mark is not FFFE", name)
        if (major, shift) not in ((3, 9), (4, 12)):
            raise ParseError(f"unsupported CFB version {major} with sector shift {shift}", name)
        if mini_shift != MINI_SHIFT:
            raise ParseError(f"unsupported CFB mini sector shift {mini_shift}", name)
        nfat, first_dir = struct.unpack_from("<II", data, 0x2C)
        cutoff, first_minifat, nminifat, first_difat, ndifat = struct.unpack_from("<IIIII", data, 0x38)
        if cutoff != MINI_CUTOFF:
            raise ParseError(f"unsupported CFB mini stream cutoff {cutoff}", name)
        self.major = major
        self.sector = 1 << shift
        self.count = (len(data) - self.sector + self.sector - 1) // self.sector  # 파일에 있는 섹터 수(끝 조각 포함)
        self._owner = bytearray(self.count)  # 큰 섹터 소유 표시(0 = 아직 없음)
        if nfat > self.count or ndifat > self.count:
            raise ParseError("CFB header declares more FAT or DIFAT sectors than the file has", name)
        fat_sectors = self._difat(first_difat, ndifat, nfat)
        self._fat = _words(b"".join(self._sector(s) for s in fat_sectors))
        for sectors, mark, what in ((fat_sectors, FATSECT, "FAT"), (self._difat_sectors, DIFSECT, "DIFAT")):
            if any(s >= len(self._fat) or self._fat[s] != mark for s in sectors):
                raise ParseError(f"CFB {what} sector is not marked as such in the FAT", name)
        directory = self._chain(first_dir, MAX_DIR_ENTRIES * ENTRY // self.sector + 1, "directory")
        if len(directory) * self.sector // ENTRY > MAX_DIR_ENTRIES:
            raise ParseError(f"more than {MAX_DIR_ENTRIES} CFB directory entries", name)
        raw = b"".join(self._sector(s) for s in directory)
        self._entries = [self._entry(raw, k) for k in range(len(raw) // ENTRY)]
        root = self._entries[0] if self._entries else None
        if root is None or root.kind != ROOT or any(e.kind == ROOT for e in self._entries[1:]):
            raise ParseError("CFB directory must have exactly one root entry, first", name)
        minifat = self._chain(first_minifat, nminifat, "mini FAT") if nminifat else []
        if len(minifat) != nminifat:
            raise ParseError("CFB mini FAT sector count does not match the header", name)
        self._minifat = _words(b"".join(self._sector(s) for s in minifat))
        self._mini = self._chain(root.start, None, "mini stream", root.size) if root.size else []
        self._mini_count = (root.size + (1 << MINI_SHIFT) - 1) >> MINI_SHIFT
        self._mini_owner = bytearray(self._mini_count)
        self._chains: dict[str, list[int]] = {}
        self.streams: dict[str, int] = {}
        self._tree(root)

    def _sector(self, s: int) -> bytes:
        start = (s + 1) * self.sector
        return self._data[start:start + self.sector]

    def _difat(self, first: int, ndifat: int, nfat: int) -> list[int]:
        """머리 109개 + DIFAT 섹터 사슬(선언 개수만큼)의 항목. 유효 항목은 앞에 모여 있고 수가 머리의 FAT 섹터 수와
        같으며, 모두 파일 안의 서로 다른 섹터다(FAT 배열 크기를 파일 크기 이하로 묶는다)."""
        entries = list(_words(self._data[0x4C:0x4C + HEADER_DIFAT * 4]))
        self._difat_sectors: list[int] = []
        s = first
        for _ in range(ndifat):
            self._claim(s, "DIFAT")
            self._difat_sectors.append(s)
            words = _words(self._sector(s))
            if len(words) < self.sector // 4:
                raise ParseError("CFB DIFAT sector is truncated", self.name)
            entries += words[:-1]
            s = words[-1]
        if s not in (ENDOFCHAIN, FREESECT):
            raise ParseError("CFB DIFAT chain is longer than the header declares", self.name)
        valid = entries[:nfat]
        if len(valid) < nfat or any(e > MAXREGSECT for e in valid) or any(e != FREESECT for e in entries[nfat:]):
            raise ParseError("CFB DIFAT does not list exactly the declared FAT sectors", self.name)
        for e in valid:
            self._claim(e, "FAT")
        return valid

    def _claim(self, s: int, what: str) -> None:
        """큰 섹터 s를 사슬 하나의 것으로 표시한다. 범위 밖이거나 이미 다른 사슬(또는 같은 사슬 앞)이 가졌으면
        ParseError(공유·순환)."""
        if s > MAXREGSECT or s >= self.count:
            raise ParseError(f"CFB {what} sector out of range", self.name)
        if self._owner[s]:
            raise ParseError(f"CFB {what} sector is used twice (shared or cyclic chain)", self.name)
        self._owner[s] = 1

    def _chain(self, start: int, most: int | None, what: str, size: int | None = None) -> list[int]:
        """FAT 사슬의 섹터들. most개를 넘으면 그만 따른다(None은 끝까지). size가 있으면 그 크기를 덮어야 한다(선언보다
        긴 사슬은 그대로 두고 읽을 때 자른다)."""
        out: list[int] = []
        s = start
        while s != ENDOFCHAIN and (most is None or len(out) < most):
            self._claim(s, what)
            out.append(s)
            if s >= len(self._fat):
                raise ParseError(f"CFB {what} sector has no FAT entry", self.name)
            s = self._fat[s]
        if size is not None and len(out) * self.sector < size:
            raise ParseError(f"CFB {what} chain ends before its declared size", self.name)
        return out

    def _entry(self, raw: bytes, k: int) -> _Entry:
        at = k * ENTRY
        (length,) = struct.unpack_from("<H", raw, at + 64)
        kind = raw[at + 66]
        left, right, child = struct.unpack_from("<III", raw, at + 68)
        start, size = struct.unpack_from("<IQ", raw, at + 116)
        if self.major == 3:
            size &= 0xFFFFFFFF  # major 3은 크기 상위 32비트를 무시한다(MS-CFB)
        if kind not in (EMPTY, STORAGE, STREAM, ROOT):
            raise ParseError(f"unknown CFB directory entry type {kind}", self.name)
        if kind == EMPTY:
            return _Entry("", kind, left, right, child, start, size)
        if length > 64 or length < 2 or length % 2 or raw[at + length - 2:at + length] != b"\0\0":
            raise ParseError("invalid CFB entry name length", self.name)
        try:
            entry_name = raw[at:at + length - 2].decode("utf-16-le")
        except UnicodeDecodeError:
            raise ParseError("invalid CFB entry name", self.name) from None
        if FORBIDDEN & set(entry_name) or "\0" in entry_name:
            raise ParseError("CFB entry name has a forbidden character", self.name)
        return _Entry(entry_name, kind, left, right, child, start, size)

    def _tree(self, root: _Entry) -> None:
        """뿌리 child부터 left·right·child를 명시적 스택으로 돈다. 다시 만난 항목(순환·공유), 빈 항목, 자식 있는 스트림,
        같은 저장소 안 CFB 비교 규칙으로 같은 이름은 ParseError. 스트림마다 사슬을 미리 따라 소유를 센다."""
        seen = {0}
        names: dict[int, set[tuple[int, str]]] = {}  # 부모 항목 → 자식 이름 키
        stack = [(root.child, 0, "")] if root.child != NOSTREAM else []
        while stack:
            k, parent, prefix = stack.pop()
            if k >= len(self._entries) or k in seen:
                raise ParseError("CFB directory tree has a cycle, a shared or a missing entry", self.name)
            seen.add(k)
            entry = self._entries[k]
            if entry.kind == EMPTY:
                raise ParseError("CFB directory tree reaches an unused entry", self.name)
            key = _fold(entry.name)
            if key in names.setdefault(parent, set()):
                raise ParseError("duplicate CFB entry name in a storage", f"{self.name}:{prefix}{entry.name}")
            names[parent].add(key)
            path = prefix + entry.name
            for side in (entry.right, entry.left):
                if side != NOSTREAM:
                    stack.append((side, parent, prefix))
            if entry.kind == STORAGE:
                if entry.child != NOSTREAM:
                    stack.append((entry.child, k, path + "/"))
            elif entry.child != NOSTREAM:
                raise ParseError("CFB stream entry has children", f"{self.name}:{path}")
            else:
                if path in self.streams:
                    raise ParseError("duplicate CFB stream path", f"{self.name}:{path}")
                self.streams[path] = entry.size
                self._chains[path] = self._stream_chain(entry, path)

    def _stream_chain(self, entry: _Entry, path: str) -> list[int]:
        if entry.size == 0:
            return []
        if entry.size >= MINI_CUTOFF:
            try:
                return self._chain(entry.start, None, "stream", entry.size)
            except ParseError as exc:
                raise ParseError(exc.reason, f"{self.name}:{path}") from None
        out: list[int] = []
        m = entry.start
        while m != ENDOFCHAIN and len(out) * (1 << MINI_SHIFT) < entry.size:
            if m >= self._mini_count or m >= len(self._minifat):
                raise ParseError("CFB mini sector out of range", f"{self.name}:{path}")
            if self._mini_owner[m]:
                raise ParseError("CFB mini sector is used twice (shared or cyclic chain)", f"{self.name}:{path}")
            self._mini_owner[m] = 1
            out.append(m)
            m = self._minifat[m]
        if len(out) * (1 << MINI_SHIFT) < entry.size:
            raise ParseError("CFB mini chain ends before its declared size", f"{self.name}:{path}")
        return out

    def has(self, path: str) -> bool:
        return path in self.streams

    def read(self, path: str, limit: int) -> bytes:
        """정확한 경로의 스트림을 min(크기, limit + 1)바이트까지만 모은다(부르는 쪽이 길이로 상한 초과를 안다)."""
        if path not in self.streams:
            raise ParseError("missing CFB stream", f"{self.name}:{path}")
        take = min(self.streams[path], limit + 1)
        chain = self._chains[path]
        if self.streams[path] >= MINI_CUTOFF:
            need = (take + self.sector - 1) // self.sector
            return b"".join(self._sector(s) for s in chain[:need])[:take]
        unit = 1 << MINI_SHIFT
        need = (take + unit - 1) // unit
        out = []
        for m in chain[:need]:
            big, at = divmod(m * unit, self.sector)
            out.append(self._sector(self._mini[big])[at:at + unit])
        return b"".join(out)[:take]
