"""실행 모델 자원: ORT 스레드 손잡이, 메모리·CPU 한도 알아내기(가짜 /proc·cgroup 파일을 임시 뿌리에), 크기 문자열,
비용·입장 산수. onnxruntime이 필요한 테스트는 실제 모델 세션을 연다."""

import os
from types import SimpleNamespace

import pytest

from hanji import models, resources
from hanji.errors import HanjiError
from hanji.formats.pdf import layout, ocr


def require_models(*names: str) -> None:
    """실제 모델 파일이 필요한 테스트: 찾을 수 없으면 건너뛰고, HANJI_CI_REQUIRE_MODELS=1이면 실패한다."""
    missing = [name for name in names if models.find(name) is None]
    if missing:
        if os.environ.get("HANJI_CI_REQUIRE_MODELS") == "1":
            pytest.fail(f"model files not found: {missing}; run `hanji models fetch`")
        pytest.skip(f"model files not found: {missing} (hanji models fetch)")


@pytest.mark.parametrize(("raw", "threads"), [(None, None), ("", None), ("2", 2), (" 3 ", 3)])
def test_ort_threads_env_reads_a_positive_integer(monkeypatch, raw, threads):
    monkeypatch.delenv("HANJI_ORT_THREADS", raising=False)
    if raw is not None:
        monkeypatch.setenv("HANJI_ORT_THREADS", raw)
    assert resources.ort_threads_env() == threads


def test_ort_threads_env_accepts_the_largest_int32(monkeypatch):
    monkeypatch.setenv("HANJI_ORT_THREADS", str(2**31 - 1))
    assert resources.ort_threads_env() == 2**31 - 1


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5", "²", str(2**31),
                                 pytest.param("9" * 5000, id="5000-digits")])
def test_ort_threads_env_rejects_other_values_as_a_setting_error(monkeypatch, raw):
    monkeypatch.setenv("HANJI_ORT_THREADS", raw)
    with pytest.raises(HanjiError, match=r"HANJI_ORT_THREADS must be a positive integer"):
        resources.ort_threads_env()


def test_set_ort_threads_changes_session_options_only_when_asked(monkeypatch):
    monkeypatch.delenv("HANJI_ORT_THREADS", raising=False)
    options = SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
    resources.set_ort_threads(options)
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (0, 0)
    monkeypatch.setenv("HANJI_ORT_THREADS", "2")
    resources.set_ort_threads(options)
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (2, 1)


def test_ocr_and_layout_sessions_follow_hanji_ort_threads(monkeypatch):
    """HANJI_ORT_THREADS=2면 OCR·레이아웃 세션이 intra 2·inter 1, 없으면 onnxruntime 기본값(0)."""
    pytest.importorskip("onnxruntime")
    require_models(*ocr.MODEL_NAMES, *layout.MODEL_NAMES)
    from hanji.formats.pdf.layout.detector import LayoutDetector
    from hanji.formats.pdf.ocr import reader

    def threads():
        sessions = (reader._session(models.resolve("ocr-det")),
                    LayoutDetector(models.resolve("layout"), models.resolve("layout-config")).session)
        return [(s.get_session_options().intra_op_num_threads, s.get_session_options().inter_op_num_threads)
                for s in sessions]

    monkeypatch.delenv("HANJI_ORT_THREADS", raising=False)
    assert threads() == [(0, 0), (0, 0)]
    monkeypatch.setenv("HANJI_ORT_THREADS", "2")
    assert threads() == [(2, 1), (2, 1)]


def test_bad_hanji_ort_threads_stops_sessions_as_a_setting_error(monkeypatch):
    """잘못된 값은 설치 문제(OcrUnavailable·LayoutUnavailable)가 아니라 설정 오류(HanjiError, 종료 코드 1)이고 세션을
    만들기 전에 알린다(모델 파일이 없어도 같다)."""
    monkeypatch.setenv("HANJI_ORT_THREADS", "many")
    monkeypatch.setattr(ocr, "_reader", None)
    monkeypatch.setattr(layout, "_detector", None)
    for build in (ocr.get_reader, layout.get_detector):
        with pytest.raises(HanjiError, match=r"^HANJI_ORT_THREADS must be a positive integer, got 'many'$") as info:
            build()
        assert type(info.value) is HanjiError
    assert ocr._reader is None and layout._detector is None


V2_MOUNT = "30 23 0:26 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime shared:4 - cgroup2 cgroup2 rw,nsdelegate\n"
V1_MOUNTS = ("35 25 0:30 / /sys/fs/cgroup/memory rw,nosuid shared:15 - cgroup cgroup rw,memory\n"
             "36 25 0:31 / /sys/fs/cgroup/cpu,cpuacct rw,nosuid shared:16 - cgroup cgroup rw,cpu,cpuacct\n")


def fake_root(tmp_path, files: dict[str, str]):
    """가짜 파일 시스템 뿌리: {뿌리 기준 경로: 내용}."""
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_cgroup_v2_takes_the_smallest_room_from_its_own_group_up_to_the_mount(tmp_path):
    """자기 경로부터 마운트 뿌리까지 min(memory.max, 물리) − memory.current의 최솟값. 'max' 단계는 건너뛴다. 조상이 더
    작으면 조상. 마운트 뿌리(/kubepods)는 경로에서 뺀다."""
    root = fake_root(tmp_path, {
        "proc/self/cgroup": "0::/kubepods/pod1/c1\n",
        "proc/self/mountinfo": V2_MOUNT.replace(" / /sys/fs/cgroup", " /kubepods /sys/fs/cgroup"),
        "sys/fs/cgroup/pod1/c1/memory.max": "max\n",
        "sys/fs/cgroup/pod1/c1/memory.current": "100\n",
        "sys/fs/cgroup/pod1/memory.max": str(8 * resources.GiB),
        "sys/fs/cgroup/pod1/memory.current": str(3 * resources.GiB),
        "sys/fs/cgroup/memory.max": str(6 * resources.GiB),
        "sys/fs/cgroup/memory.current": str(2 * resources.GiB),
    })
    assert resources._available(root, 64 * resources.GiB) == 4 * resources.GiB  # 뿌리 6 − 2 < pod1 8 − 3
    (root / "sys/fs/cgroup/memory.max").write_text("max\n", encoding="utf-8")
    assert resources._available(root, 64 * resources.GiB) == 5 * resources.GiB


def test_cgroup_v1_skips_the_unlimited_marker_and_its_usage(tmp_path):
    """v1 memory 계층: 자기 경로와 조상의 최솟값. '제한 없음' 표시값(2**62 이상)은 한도가 아니라 그 단계를 건너뛰고
    사용량도 빼지 않는다(스펙 §8.5 공통, Task 4 리뷰 판정)."""
    root = fake_root(tmp_path, {
        "proc/self/cgroup": "12:cpu,cpuacct:/docker/abc\n9:memory:/docker/abc\n0::/\n",
        "proc/self/mountinfo": V1_MOUNTS,
        "sys/fs/cgroup/memory/docker/abc/memory.limit_in_bytes": "9223372036854771712\n",
        "sys/fs/cgroup/memory/docker/abc/memory.usage_in_bytes": str(10 * resources.GiB),
        "sys/fs/cgroup/memory/docker/memory.limit_in_bytes": str(20 * resources.GiB),
        "sys/fs/cgroup/memory/docker/memory.usage_in_bytes": str(resources.GiB),
    })
    assert resources._available(root, 16 * resources.GiB) == 15 * resources.GiB  # abc의 10GiB 사용량은 빼지 않는다
    (root / "sys/fs/cgroup/memory/docker/memory.limit_in_bytes").write_text(str(4 * resources.GiB), encoding="utf-8")
    assert resources._available(root, 16 * resources.GiB) == 3 * resources.GiB


@pytest.mark.parametrize("marker", [str(2**63 - 4096), str(0x7FFFFFFFFFFF0000), str(2**62)])
def test_cgroup_v1_with_the_unlimited_marker_everywhere_is_physical_memory(tmp_path, marker):
    """컨테이너 밖 v1 호스트: 모든 단계(마운트 뿌리 포함)가 표시값이면 물리 메모리다. 페이지 캐시가 든 뿌리 사용량을
    빼지 않는다."""
    root = fake_root(tmp_path, {
        "proc/self/cgroup": "9:memory:/user.slice\n",
        "proc/self/mountinfo": V1_MOUNTS,
        "sys/fs/cgroup/memory/user.slice/memory.limit_in_bytes": marker,
        "sys/fs/cgroup/memory/user.slice/memory.usage_in_bytes": str(2 * resources.GiB),
        "sys/fs/cgroup/memory/memory.limit_in_bytes": marker,
        "sys/fs/cgroup/memory/memory.usage_in_bytes": str(14 * resources.GiB),
    })
    assert resources._available(root, 16 * resources.GiB) == 16 * resources.GiB


def test_root_files_are_read_when_proc_is_missing_and_usage_is_still_subtracted(tmp_path):
    """/proc 정보가 없으면 뿌리 파일만 본다. 사용량을 빼고, 물리 이상 유한 한도는 min(한도, 물리) − 사용량."""
    root = fake_root(tmp_path, {"sys/fs/cgroup/memory.max": str(32 * resources.GiB),
                                "sys/fs/cgroup/memory.current": str(resources.GiB)})
    assert resources._available(root, 16 * resources.GiB) == 15 * resources.GiB
    root = fake_root(tmp_path / "v1", {"sys/fs/cgroup/memory/memory.limit_in_bytes": str(4 * resources.GiB)})
    assert resources._available(root, 16 * resources.GiB) == 4 * resources.GiB  # 사용량 파일이 없으면 0


def test_root_files_are_read_when_proc_is_there_but_no_mount_or_limit_is_found(tmp_path):
    """/proc는 읽히지만 cgroup 마운트를 찾지 못하거나(다른 마운트뿐), 찾은 계층 어디에도 한도 파일이 없으면 뿌리
    파일을 본다(스펙 §8.5-3). 예전 코드는 물리 메모리를 돌려 예산을 크게 잡았다."""
    root = fake_root(tmp_path / "nomount", {
        "proc/self/cgroup": "0::/app\n", "proc/self/mountinfo": "22 1 0:21 / /proc rw - proc proc rw\n",
        "sys/fs/cgroup/memory.max": str(4 * resources.GiB), "sys/fs/cgroup/memory.current": str(resources.GiB)})
    assert resources._available(root, 16 * resources.GiB) == 3 * resources.GiB
    root = fake_root(tmp_path / "nolimit", {  # 통합 계층이 /sys/fs/cgroup/unified에 있고 거기엔 한도가 없다
        "proc/self/cgroup": "0::/app\n",
        "proc/self/mountinfo": V2_MOUNT.replace("/sys/fs/cgroup ", "/sys/fs/cgroup/unified "),
        "sys/fs/cgroup/unified/app/memory.current": "100\n",
        "sys/fs/cgroup/memory/memory.limit_in_bytes": str(2 * resources.GiB)})
    assert resources._available(root, 16 * resources.GiB) == 2 * resources.GiB


def test_without_cgroup_limits_memory_is_physical_or_unknown(tmp_path, monkeypatch):
    root = fake_root(tmp_path, {"proc/self/cgroup": "0::/user.slice\n", "proc/self/mountinfo": V2_MOUNT,
                                "sys/fs/cgroup/user.slice/memory.max": "max\n"})
    assert resources._available(root, 16 * resources.GiB) == 16 * resources.GiB
    assert resources._available(tmp_path / "empty", None) is None
    monkeypatch.delattr(os, "sysconf", raising=False)  # Windows
    assert resources.physical_memory() is None


@pytest.mark.parametrize(("files", "cores"), [
    ({"sys/fs/cgroup/app/cpu.max": "200000 100000\n"}, 2),
    ({"sys/fs/cgroup/app/cpu.max": "150000 100000\n"}, 2),
    ({"sys/fs/cgroup/app/cpu.max": "max 100000\n"}, 8),
    ({"sys/fs/cgroup/app/cpu.max": "max 100000\n", "sys/fs/cgroup/cpu.max": "300000 100000\n"}, 3),
])
def test_cgroup_v2_cpu_quota_rounds_up_and_takes_the_smallest(tmp_path, files, cores):
    root = fake_root(tmp_path, {"proc/self/cgroup": "0::/app\n", "proc/self/mountinfo": V2_MOUNT, **files})
    assert resources._cores(root, [8, 12]) == cores


@pytest.mark.parametrize(("quota", "cores"), [("-1", 6), ("250000", 3)])
def test_cgroup_v1_cpu_quota(tmp_path, quota, cores):
    root = fake_root(tmp_path, {"proc/self/cgroup": "12:cpu,cpuacct:/docker/abc\n9:memory:/docker/abc\n",
                                "proc/self/mountinfo": V1_MOUNTS,
                                "sys/fs/cgroup/cpu,cpuacct/docker/abc/cpu.cfs_quota_us": quota,
                                "sys/fs/cgroup/cpu,cpuacct/docker/abc/cpu.cfs_period_us": "100000"})
    assert resources._cores(root, [6]) == cores


def test_cpu_count_takes_a_smaller_affinity_and_a_smaller_quota(tmp_path, monkeypatch):
    """실효 코어 = min(os.cpu_count(), sched_getaffinity(있을 때), cgroup 쿼터). 가짜 os 값과 가짜 뿌리로 본다."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    assert resources._cpu_count(fake_root(tmp_path / "plain", {})) == 3  # affinity 3 < cpu_count 8
    root = fake_root(tmp_path / "quota", {"proc/self/cgroup": "0::/app\n", "proc/self/mountinfo": V2_MOUNT,
                                          "sys/fs/cgroup/app/cpu.max": "200000 100000\n"})
    assert resources._cpu_count(root) == 2  # 쿼터 2 < affinity 3
    monkeypatch.delattr(os, "sched_getaffinity")  # macOS·Windows: affinity 없음
    assert resources._cpu_count(fake_root(tmp_path / "plain", {})) == 8


@pytest.mark.parametrize(("text", "size"), [
    ("8G", 8 * 2**30), ("512M", 512 * 2**20), ("1.5GiB", 3 * 2**29), ("4096", 4096), ("2k", 2048), ("1TB", 2**40),
    (" 8G ", 8 * 2**30),
])
def test_parse_size_reads_1024_multiples(text, size):
    assert resources.parse_size(text) == size


@pytest.mark.parametrize("text", ["", "abc", "1.5", "0", "0.0001K", "-1G", "8X", "G", "3 GiB"])
def test_parse_size_rejects_other_text(text):
    with pytest.raises(ValueError, match="invalid size"):
        resources.parse_size(text)


def test_budget_is_eighty_percent_of_the_limit_and_a_larger_max_memory_is_clipped():
    assert resources.budget(None, 10 * resources.GiB) == (8 * resources.GiB, [])
    assert resources.budget(None, None) == (None, [])
    assert resources.budget(4 * resources.GiB, 10 * resources.GiB) == (4 * resources.GiB, [])
    assert resources.budget(4 * resources.GiB, None) == (4 * resources.GiB, [])
    assert resources.budget(16 * resources.GiB, 10 * resources.GiB) == (10 * resources.GiB, [
        "max memory 16.0GiB is above the available memory 10.0GiB; using 10.0GiB"])


@pytest.mark.parametrize("text", ["--5", "²", "1e9", "+5", " 1 2", "0x10",
                                  pytest.param("9" * 5000, id="5000-digits")])
def test_broken_limit_and_usage_files_count_as_unknown(tmp_path, text):
    """숫자 파일 내용은 ASCII 정수만 읽는다. 깨진 한도는 그 단계가 없는 것, 깨진 사용량은 0이다(예외를 내지 않는다)."""
    root = fake_root(tmp_path, {"proc/self/cgroup": "0::/app\n", "proc/self/mountinfo": V2_MOUNT,
                                "sys/fs/cgroup/app/memory.max": text,
                                "sys/fs/cgroup/memory.max": str(4 * resources.GiB),
                                "sys/fs/cgroup/memory.current": text})
    assert resources._available(root, 16 * resources.GiB) == 4 * resources.GiB


@pytest.mark.parametrize("files", [
    {"sys/fs/cgroup/app/cpu.max": "200000 0\n"},
    {"sys/fs/cgroup/app/cpu.max": "²00000 100000\n"},
    {"sys/fs/cgroup/app/cpu.max": "200000 -100000\n"},
    {"sys/fs/cgroup/app/cpu.max": "200000\n"},
    pytest.param({"sys/fs/cgroup/app/cpu.max": "9" * 5000 + " 100000\n"}, id="5000-digits"),
])
def test_broken_cgroup_v2_cpu_max_counts_as_no_quota(tmp_path, files):
    root = fake_root(tmp_path, {"proc/self/cgroup": "0::/app\n", "proc/self/mountinfo": V2_MOUNT, **files})
    assert resources._cores(root, [8]) == 8


@pytest.mark.parametrize(("quota", "period"), [("250000", "0"), ("250000", "-100000"), ("--1", "100000"),
                                               ("250000", "1.5")])
def test_broken_cgroup_v1_cpu_quota_counts_as_no_quota(tmp_path, quota, period):
    root = fake_root(tmp_path, {"proc/self/cgroup": "12:cpu,cpuacct:/docker/abc\n",
                                "proc/self/mountinfo": V1_MOUNTS,
                                "sys/fs/cgroup/cpu,cpuacct/docker/abc/cpu.cfs_quota_us": quota,
                                "sys/fs/cgroup/cpu,cpuacct/docker/abc/cpu.cfs_period_us": period})
    assert resources._cores(root, [6]) == 6


def test_cpu_count_ignores_an_affinity_that_cannot_be_read(tmp_path, monkeypatch):
    def broken(pid):
        raise OSError("affinity not readable")

    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", broken, raising=False)
    assert resources._cpu_count(fake_root(tmp_path, {})) == 8


@pytest.mark.parametrize(("page", "pages"), [(4096, -1), (-1, 1000), (4096, 0)])
def test_physical_memory_that_is_not_positive_is_unknown(monkeypatch, page, pages):
    values = {"SC_PAGE_SIZE": page, "SC_PHYS_PAGES": pages}
    monkeypatch.setattr(os, "sysconf", values.__getitem__, raising=False)
    assert resources.physical_memory() is None


def test_the_mount_that_holds_the_own_cgroup_is_chosen_before_an_unrelated_one(tmp_path):
    """같은 종류 마운트가 여럿이면 자기 cgroup 경로를 품은 마운트를 먼저 고른다. 앞에 놓인 다른 cgroup2 마운트(뿌리
    /other)를 고르면 엉뚱한 폴더의 한도를 읽는다."""
    other = "40 23 0:50 /other /mnt/other rw,relatime shared:9 - cgroup2 cgroup2 rw\n"
    root = fake_root(tmp_path, {"proc/self/cgroup": "0::/app\n", "proc/self/mountinfo": other + V2_MOUNT,
                                "mnt/other/memory.max": str(8 * resources.GiB),
                                "mnt/other/cpu.max": "400000 100000\n",
                                "sys/fs/cgroup/app/memory.max": str(2 * resources.GiB),
                                "sys/fs/cgroup/app/cpu.max": "100000 100000\n"})
    assert resources._available(root, 16 * resources.GiB) == 2 * resources.GiB
    assert resources._cores(root, [8]) == 1


def test_an_own_path_outside_the_namespace_is_read_as_the_mount_point(tmp_path):
    """다른 cgroup 이름공간에서 본 경로("/../..")는 마운트 지점으로 본다. 마운트 지점 밖 폴더를 읽지 않는다."""
    root = fake_root(tmp_path, {"proc/self/cgroup": "0::/../../outside\n", "proc/self/mountinfo": V2_MOUNT,
                                "sys/outside/memory.max": str(resources.GiB),
                                "sys/memory.max": str(resources.GiB),
                                "sys/fs/cgroup/memory.max": str(3 * resources.GiB)})
    assert resources._available(root, 16 * resources.GiB) == 3 * resources.GiB


@pytest.mark.parametrize(("text", "size"), [
    ("9007199254740993", 2**53 + 1), ("9007199254740993K", (2**53 + 1) * 1024), ("8B", 8), ("8kb", 8192),
    ("1.5KiB", 1536),
])
def test_parse_size_is_exact_for_whole_numbers_and_takes_a_plain_b(text, size):
    assert resources.parse_size(text) == size


@pytest.mark.parametrize("text", ["8iB", "1.5B", pytest.param("9" * 400 + ".5G", id="400-digits-G"), "８G"])
def test_parse_size_rejects_ib_without_a_prefix_overflow_and_other_digits(text):
    with pytest.raises(ValueError, match="invalid size"):
        resources.parse_size(text)
