import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import sys

import pytest

from hanji import cli
from hanji.cli import main
from hanji.core import build_tree, diff_trees
from hanji.errors import AssetNotFound
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hanji.formats.base import ParsedSource
from hanji.formats.pdf import ocr
from hanji.formats.pdf import parser as pdf_parser
from hanji.formats.pdf.ocr import OcrLine
from hanji.store import SqliteStore
from hanji.store import sqlite as sqlite_store
from hanji_contracts import ChangeBatch, DocumentTree, PageInfo, ProcessingHistory, SourceInfo


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("HANJI_DB", raising=False)
    return tmp_path / "상태" / "state.db"


def write(path, text: str, encoding: str = "utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_parse_prints_contract_json(capsys, db, tmp_path):
    path = write(tmp_path / "한글 폴더" / "보고서.md", "# 제목\n\n본문 문단\n")
    code, out, _ = run(capsys, "parse", path, "--db", db)
    assert code == 0
    tree = DocumentTree.model_validate_json(out)
    assert [b.text for b in tree.blocks] == ["제목", "본문 문단"] and tree.source.name == "보고서.md"
    assert "본문 문단" in out  # ensure_ascii=False


def test_parse_markdown_to_file(capsys, db, tmp_path):
    path = write(tmp_path / "a.md", "# 제목\n\n- 항목\n", encoding="cp949")
    out_file = tmp_path / "결과" / "a.md"
    code, out, _ = run(capsys, "parse", path, "--db", db, "--format", "md", "--out", out_file)
    assert (code, out) == (0, "")
    assert out_file.read_bytes() == "# 제목\n\n- 항목\n".encode("utf-8")


def test_reparse_with_id_and_force(capsys, db, tmp_path):
    path = write(tmp_path / "a.md", "가\n")
    assert run(capsys, "parse", path, "--db", db, "--id", "문서")[0] == 0
    write(path, "가\n\n나\n")
    code, out, _ = run(capsys, "parse", path, "--db", db, "--id", "문서")
    assert code == 0 and DocumentTree.model_validate_json(out).version == 2
    code, out, _ = run(capsys, "parse", path, "--db", db, "--id", "문서", "--force")
    assert code == 0 and DocumentTree.model_validate_json(out).version == 2


def test_export_documents_changes_history(capsys, db, tmp_path):
    path = write(tmp_path / "a.md", "가\n")
    run(capsys, "parse", path, "--db", db, "--id", "d1")
    write(path, "나\n")
    run(capsys, "parse", path, "--db", db, "--id", "d1")
    code, out, _ = run(capsys, "export", "d1", "--version", "1", "--db", db, "--format", "md")
    assert (code, out) == (0, "가\n")
    code, out, _ = run(capsys, "documents", "--db", db)
    assert code == 0 and json.loads(out) == [{"document_id": "d1", "version": 2, "layer_state": "det"}]
    code, out, _ = run(capsys, "changes", "--db", db, "--cursor", "1", "--limit", "5")
    batch = ChangeBatch.model_validate_json(out)
    assert code == 0 and (batch.cursor_from, batch.next_cursor, len(batch.changes)) == (1, 2, 1)
    code, out, _ = run(capsys, "history", "d1", "--db", db)
    assert code == 0 and ProcessingHistory.model_validate_json(out).version == 2


@pytest.mark.parametrize("argv", [[], ["parse"], ["unknown"], ["changes", "--limit", "0"],
                                  ["export", "d", "--version", "0"], ["changes", "--cursor", "-1"],
                                  ["parse", "a.md", "--id", ""]])
def test_usage_errors_exit_2(capsys, tmp_path, monkeypatch, argv):
    state = tmp_path / "state.db"
    monkeypatch.setenv("HANJI_DB", str(state))
    assert run(capsys, *argv)[0] == 2  # 인자 해석 단계에서 끝나 상태 파일을 열지 않는다
    assert not state.exists()


def test_db_option_before_and_after_subcommand(capsys, tmp_path, monkeypatch):
    env_db = tmp_path / "env.db"
    monkeypatch.setenv("HANJI_DB", str(env_db))
    path = write(tmp_path / "a.md", "가\n")
    before, after = tmp_path / "before.db", tmp_path / "after.db"
    assert run(capsys, "--db", before, "parse", path, "--id", "d1")[0] == 0
    assert run(capsys, "parse", path, "--db", after, "--id", "d2")[0] == 0
    assert before.exists() and after.exists() and not env_db.exists()
    assert [d["document_id"] for d in json.loads(run(capsys, "--db", before, "documents")[1])] == ["d1"]
    assert [d["document_id"] for d in json.loads(run(capsys, "documents", "--db", after)[1])] == ["d2"]


def test_db_option_given_twice_later_wins(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("HANJI_DB", raising=False)
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    assert run(capsys, "--db", first, "documents", "--db", second)[0] == 0
    assert second.exists() and not first.exists()


def test_parse_out_directory_fails_before_store(capsys, db, tmp_path):
    path = write(tmp_path / "a.md", "가\n")
    out_dir = tmp_path / "출력"
    out_dir.mkdir()
    code, out, err = run(capsys, "parse", path, "--db", db, "--out", out_dir)
    assert (code, out) == (1, "") and err.count("\n") == 1 and "Traceback" not in err
    assert not db.exists()
    assert json.loads(run(capsys, "documents", "--db", db)[1]) == []


def test_stderr_backslashreplaces_unencodable(monkeypatch):
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="ascii", newline=""))
    cli._configure_streams()  # main()이 쓰는 것과 같은 재설정
    print("hanji: \udc80 문서", file=sys.stderr)  # 짝 없는 서로게이트가 든 메시지
    sys.stderr.flush()
    assert raw.getvalue() == "hanji: \\udc80 문서\n".encode("utf-8")


def test_help_exits_0(capsys):
    code, out, _ = run(capsys, "--help")
    assert code == 0 and "parse" in out


def test_unsupported_format_exit_3(capsys, db, tmp_path):
    code, _, err = run(capsys, "parse", write(tmp_path / "a.txt", "가"), "--db", db)
    assert code == 3 and "unsupported format" in err


def test_parse_failure_exit_4_and_store_unchanged(capsys, db, tmp_path):
    path = tmp_path / "깨진.md"
    path.write_bytes(b"\xff\xfe\x00")
    code, _, err = run(capsys, "parse", path, "--db", db)
    assert code == 4 and "깨진.md" in err
    assert json.loads(run(capsys, "documents", "--db", db)[1]) == []


def test_missing_document_or_version_exit_5(capsys, db, tmp_path):
    assert run(capsys, "export", "없음", "--db", db)[0] == 5
    run(capsys, "parse", write(tmp_path / "a.md", "가\n"), "--db", db, "--id", "d1")
    assert run(capsys, "export", "d1", "--version", "9", "--db", db)[0] == 5
    assert run(capsys, "history", "d1", "--version", "9", "--db", db)[0] == 5


def test_missing_file_exit_1(capsys, db, tmp_path):
    code, _, err = run(capsys, "parse", tmp_path / "없는 파일.md", "--db", db)
    assert code == 1 and "FileNotFoundError" in err
    assert err.count("\n") == 1 and "Traceback" not in err


def test_db_not_sqlite_exit_1(capsys, tmp_path):
    bad = write(tmp_path / "state.db", "이것은 SQLite 파일이 아니다. " * 20)
    code, out, err = run(capsys, "documents", "--db", bad)
    assert (code, out) == (1, "") and err.startswith("hanji: ")
    assert err.count("\n") == 1 and "Traceback" not in err


@pytest.mark.parametrize("old", ["1", "2", "3"])  # 계약 0.1·0.2·0.3 시절 상태 파일
def test_db_from_older_contracts_exit_1(capsys, db, old):
    assert run(capsys, "documents", "--db", db)[0] == 0
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (old,))
    conn.close()
    code, out, err = run(capsys, "documents", "--db", db)
    assert (code, out) == (1, "") and err.startswith("hanji: ") and "ingest again" in err
    assert err.count("\n") == 1 and "Traceback" not in err


def test_deeply_nested_markdown_exit_4_and_store_unchanged(capsys, db, tmp_path):
    nested = "".join("  " * i + "- 항목\n" for i in range(12))
    code, _, err = run(capsys, "parse", write(tmp_path / "깊은.md", nested), "--db", db)
    assert code == 4 and "block nesting too deep" in err and "깊은.md" in err
    assert err.count("\n") == 1 and "Traceback" not in err
    assert json.loads(run(capsys, "documents", "--db", db)[1]) == []
    assert json.loads(run(capsys, "changes", "--db", db)[1])["changes"] == []


def test_db_priority(capsys, tmp_path, monkeypatch):
    option, env, default_dir = tmp_path / "opt.db", tmp_path / "env.db", tmp_path / "기본"
    monkeypatch.setattr(sqlite_store, "user_data_dir", lambda appname, appauthor: str(default_dir))
    monkeypatch.delenv("HANJI_DB", raising=False)
    assert cli.resolve_db(None) == default_dir / "state.db"
    monkeypatch.setenv("HANJI_DB", str(env))
    assert cli.resolve_db(None) == env
    assert cli.resolve_db(str(option)) == option
    run(capsys, "documents", "--db", option)
    run(capsys, "documents")
    assert option.exists() and env.exists() and not (default_dir / "state.db").exists()


def test_stdout_forced_to_utf8(monkeypatch, db, tmp_path):
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", newline=""))
    path = write(tmp_path / "a.md", "# 한글 제목\n")
    assert main(["parse", str(path), "--db", str(db), "--format", "md"]) == 0
    assert raw.getvalue().decode("utf-8") == "# 한글 제목\n"


def seed_figure(db, figures: int = 1) -> tuple[str, bytes]:
    """그림 블록·캡션과 그 이미지를 상태 파일에 바로 넣는다(이 PR의 파서는 아직 그림을 내지 않는다).
    figures개의 그림(캡션 i: "그림 i. 현황")이 모두 같은 이미지를 가리킨다."""
    png = b"\x89PNG\r\n\x1a\n-figure-"
    asset = "sha256:" + hashlib.sha256(png).hexdigest()
    loc = {"kind": "page", "page": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.4}}
    specs = []
    for i in range(figures):
        specs += [{"kind": "figure", "text": "", "confidence": 0.7, "state": "det", "text_source": "text_layer",
                   "locator": loc, "figure": {"asset": asset, "mime": "image/png", "width_px": 4, "height_px": 3,
                                              "dpi": 200, "category": "image", "caption_ref": 2 * i + 1}},
                  {"kind": "caption", "text": f"그림 {i + 1}. 현황", "confidence": 0.7, "state": "det",
                   "text_source": "text_layer", "locator": loc}]
    page = PageInfo(page=1, width_pt=595.0, height_pt=842.0, render_dpi=144)
    source = SourceInfo(name="f.pdf", mime="application/pdf", content_hash="sha256:" + "0" * 64, page_count=1)
    tree = build_tree(ParsedSource(mime="application/pdf", pages=(page,), blocks=specs), "fig", 1, source)
    with SqliteStore(db) as store:
        store.commit(tree, diff_trees(None, tree), ProcessingHistory(document_id="fig", version=1), {asset: png})
    return asset, png


def test_export_assets_writes_pngs_and_markdown_links(capsys, db, tmp_path, monkeypatch):
    asset, png = seed_figure(db)
    monkeypatch.chdir(tmp_path)  # 표준 출력이면 링크는 현재 폴더 기준, 입력 그대로(퍼센트 인코딩)
    code, out, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--assets", "그림")
    name = asset.removeprefix("sha256:")[:16] + ".png"
    assert code == 0 and (tmp_path / "그림" / name).read_bytes() == png
    assert out == f"![그림 1. 현황](%EA%B7%B8%EB%A6%BC/{name})\n\n그림 1. 현황\n"


def test_export_json_with_assets_writes_files_and_the_same_json(capsys, db, tmp_path):
    asset, png = seed_figure(db)
    plain = run(capsys, "export", "fig", "--db", db)[1]
    code, out, _ = run(capsys, "export", "fig", "--db", db, "--assets", tmp_path / "a")
    assert code == 0 and out == plain
    assert (tmp_path / "a" / (asset.removeprefix("sha256:")[:16] + ".png")).read_bytes() == png


def test_export_missing_asset_exit_5(capsys, db, tmp_path, monkeypatch):
    seed_figure(db)

    def missing(self, asset):
        raise AssetNotFound(asset)

    monkeypatch.setattr(SqliteStore, "get_asset", missing)
    code, out, err = run(capsys, "export", "fig", "--db", db, "--assets", tmp_path / "a")
    assert (code, out) == (5, "") and "asset not found: sha256:" in err


def test_export_assets_path_that_is_a_file_exit_1(capsys, db, tmp_path):
    seed_figure(db)
    blocker = write(tmp_path / "file", "x")
    code, _, err = run(capsys, "export", "fig", "--db", db, "--assets", blocker)
    assert code == 1 and "FileExistsError" in err


def test_export_out_links_assets_relative_to_the_out_file(capsys, db, tmp_path):
    asset, png = seed_figure(db)
    docs = tmp_path / "docs"
    code, out, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", docs / "x.md",
                       "--assets", docs / "img")
    name = asset.removeprefix("sha256:")[:16] + ".png"
    assert (code, out) == (0, "") and (docs / "img" / name).read_bytes() == png
    assert (docs / "x.md").read_text(encoding="utf-8") == f"![그림 1. 현황](img/{name})\n\n그림 1. 현황\n"
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", docs / "sub" / "y.md",
                     "--assets", tmp_path / "그림 #1")
    assert code == 0
    assert (docs / "sub" / "y.md").read_text(encoding="utf-8").startswith(
        f"![그림 1. 현황](../../%EA%B7%B8%EB%A6%BC%20%231/{name})")


def test_export_out_on_another_drive_links_a_file_uri(capsys, db, tmp_path, monkeypatch):
    asset, _ = seed_figure(db)

    def other_drive(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", other_drive)
    folder = tmp_path / "그림"
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", tmp_path / "x.md",
                     "--assets", folder)
    name = asset.removeprefix("sha256:")[:16] + ".png"
    assert code == 0 and (tmp_path / "x.md").read_text(encoding="utf-8").startswith(
        f"![그림 1. 현황]({folder.resolve().as_uri()}/{name})")


def test_export_figures_sharing_one_asset_write_it_once(capsys, db, tmp_path, monkeypatch):
    asset, png = seed_figure(db, figures=2)
    calls = []
    original = SqliteStore.get_asset

    def counted(self, key):
        calls.append(key)
        return original(self, key)

    monkeypatch.setattr(SqliteStore, "get_asset", counted)
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--assets", "a")
    name = asset.removeprefix("sha256:")[:16] + ".png"
    assert code == 0 and calls == [asset] and [p.name for p in (tmp_path / "a").iterdir()] == [name]
    assert out == f"![그림 1. 현황](a/{name})\n\n그림 1. 현황\n\n![그림 2. 현황](a/{name})\n\n그림 2. 현황\n"


def test_export_out_directory_fails_before_writing_assets(capsys, db, tmp_path):
    seed_figure(db)
    out_dir = tmp_path / "출력"
    out_dir.mkdir()
    code, out, err = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", out_dir,
                         "--assets", tmp_path / "a")
    assert (code, out) == (1, "") and "IsADirectoryError: output path is a directory" in err
    assert not (tmp_path / "a").exists() and list(out_dir.iterdir()) == []


def test_export_assets_replace_a_symlink_instead_of_writing_through_it(capsys, db, tmp_path):
    asset, png = seed_figure(db)
    outside = write(tmp_path / "outside.txt", "keep me")
    folder = tmp_path / "a"
    folder.mkdir()
    target = folder / (asset.removeprefix("sha256:")[:16] + ".png")
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError) as exc:  # Windows 권한 없음 등
        pytest.skip(f"cannot create symlink: {exc}")
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--assets", folder)
    assert code == 0 and outside.read_text(encoding="utf-8") == "keep me"
    assert not target.is_symlink() and target.read_bytes() == png
    assert sorted(p.name for p in folder.iterdir()) == [target.name]  # 임시 파일이 남지 않는다


@pytest.mark.parametrize("upper", [False, True])
def test_export_out_colliding_with_an_asset_file_exit_1_and_writes_nothing(capsys, db, tmp_path, upper):
    asset, _ = seed_figure(db)
    name = asset.removeprefix("sha256:")[:16] + ".png"
    out = tmp_path / "a" / (name.upper() if upper else name)  # 대소문자를 가리지 않는 파일 시스템도 같은 파일
    code, out_text, err = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", out,
                              "--assets", tmp_path / "a")
    assert (code, out_text) == (1, "") and "output path is also an asset file" in err
    assert not (tmp_path / "a").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_export_assets_keep_the_mode_of_an_existing_png(capsys, db, tmp_path):
    asset, png = seed_figure(db)
    target = write(tmp_path / "a" / (asset.removeprefix("sha256:")[:16] + ".png"), "old")
    target.chmod(0o600)
    assert run(capsys, "export", "fig", "--db", db, "--assets", tmp_path / "a")[0] == 0
    assert target.read_bytes() == png and stat.S_IMODE(target.stat().st_mode) == 0o600


def test_export_out_symlink_is_replaced_not_followed(capsys, db, tmp_path):
    seed_figure(db)
    outside = write(tmp_path / "outside.txt", "keep me")
    out = tmp_path / "docs" / "x.md"
    out.parent.mkdir()
    try:
        os.symlink(outside, out)
    except (OSError, NotImplementedError) as exc:  # Windows 권한 없음 등
        pytest.skip(f"cannot create symlink: {exc}")
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", out)
    assert code == 0 and outside.read_text(encoding="utf-8") == "keep me"
    assert not out.is_symlink() and out.read_text(encoding="utf-8") == "그림 1. 현황\n"
    assert sorted(p.name for p in out.parent.iterdir()) == ["x.md"]


def test_export_links_are_relative_to_the_out_symlink_folder_not_its_target(capsys, db, tmp_path):
    asset, _ = seed_figure(db)
    real = write(tmp_path / "other" / "real.md", "keep me")
    out = tmp_path / "docs" / "x.md"
    out.parent.mkdir()
    try:
        os.symlink(real, out)
    except (OSError, NotImplementedError) as exc:  # Windows 권한 없음 등
        pytest.skip(f"cannot create symlink: {exc}")
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", out,
                     "--assets", tmp_path / "docs" / "img")
    name = asset.removeprefix("sha256:")[:16] + ".png"
    assert code == 0 and real.read_text(encoding="utf-8") == "keep me"
    assert out.read_text(encoding="utf-8") == f"![그림 1. 현황](img/{name})\n\n그림 1. 현황\n"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows resolves '..' lexically before following symlinks")
def test_export_links_follow_a_symlinked_parent_before_dotdot(capsys, db, tmp_path):
    asset, _ = seed_figure(db)
    (tmp_path / "other" / "deep").mkdir(parents=True)
    try:
        os.symlink(tmp_path / "other" / "deep", tmp_path / "link")
    except (OSError, NotImplementedError) as exc:  # Windows 권한 없음 등
        pytest.skip(f"cannot create symlink: {exc}")
    out = tmp_path / "link" / ".." / "x.md"  # 파일 시스템은 other/x.md에 쓴다
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", out,
                     "--assets", tmp_path / "img")
    name = asset.removeprefix("sha256:")[:16] + ".png"
    written = tmp_path / "other" / "x.md"
    assert code == 0 and written.read_text(encoding="utf-8").startswith(f"![그림 1. 현황](../img/{name})")
    assert (written.parent / "../img" / name).resolve().is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows resolves '..' lexically before following symlinks")
def test_export_out_under_a_symlinked_parent_and_dotdot_is_written_where_the_fs_puts_it(capsys, db, tmp_path):
    seed_figure(db)
    (tmp_path / "actual" / "deep").mkdir(parents=True)  # actual/nested는 없다: 내보내기가 만든다
    try:
        os.symlink(tmp_path / "actual" / "deep", tmp_path / "link")
    except (OSError, NotImplementedError) as exc:  # Windows 권한 없음 등
        pytest.skip(f"cannot create symlink: {exc}")
    code, _, _ = run(capsys, "export", "fig", "--db", db, "--format", "md",
                     "--out", tmp_path / "link" / ".." / "nested" / "x.md")
    assert code == 0 and (tmp_path / "actual" / "nested" / "x.md").is_file()
    assert not (tmp_path / "nested").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_export_fchmod_failure_keeps_the_old_file_and_leaves_no_temp(capsys, db, tmp_path, monkeypatch):
    seed_figure(db)
    out = write(tmp_path / "docs" / "x.md", "old")

    def failing(fd, mode):
        raise OSError("fchmod failed")

    monkeypatch.setattr(os, "fchmod", failing)
    code, _, err = run(capsys, "export", "fig", "--db", db, "--format", "md", "--out", out)
    assert code == 1 and "fchmod failed" in err
    assert out.read_text(encoding="utf-8") == "old" and [p.name for p in out.parent.iterdir()] == ["x.md"]


def test_parse_no_ocr_keeps_an_unreliable_page_with_its_ledger_and_history(capsys, db, tmp_path, monkeypatch):
    """--no-ocr로 파싱한 unreliable 쪽: 깨진 글자층 블록(신뢰도 0.2)·쪽 글자 장부·처리 이력이 상태 파일까지 간다."""
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(595.0, 842.0), invariant=1, pageCompression=0)
    c.setFont("HYGothic-Medium", 11)
    c.drawString(72, 770, "깨진 쪽 글자다.")
    c.showPage()
    c.save()
    path = tmp_path / "깨짐.pdf"
    path.write_bytes(buf.getvalue())
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    code, out, _ = run(capsys, "parse", path, "--db", db, "--id", "u1", "--no-ocr", "--no-layout")
    tree = DocumentTree.model_validate_json(out)
    assert code == 0 and [(b.text, b.confidence) for b in tree.blocks] == [("깨진 쪽 글자다.", 0.2)]
    (page,) = tree.pages
    assert (page.text_layer, page.coverage.layer_chars, page.coverage.in_blocks) == ("unreliable", 7, 7)
    code, out, _ = run(capsys, "history", "u1", "--db", db)
    regions = ProcessingHistory.model_validate_json(out).regions
    assert code == 0 and [(r.region_id, r.fallback_reason) for r in regions] == [
        ("p1-unreliable-text-layer", "unreliable_text_layer_kept")]


def test_view_notice_follows_the_unreliable_page_history(capsys, db, tmp_path, monkeypatch):
    """view는 상태 파일의 처리 이력을 뷰어에 넘긴다: OCR로 다시 읽은 unreliable 쪽은 OCR 안내, --no-ocr로 글자층을 남긴
    쪽은 믿기 어렵다는 안내(원본이 같으면 저장된 버전을 쓰므로 상태 파일을 따로 둔다). OCR은 모델 없이 바꿔 끼운다."""
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(595.0, 842.0), invariant=1, pageCompression=0)
    c.setFont("HYGothic-Medium", 11)
    c.drawString(72, 770, "깨진 쪽 글자다.")
    c.showPage()
    c.save()
    path = tmp_path / "깨짐.pdf"
    path.write_bytes(buf.getvalue())
    monkeypatch.setattr(pdf_parser, "classify", lambda stats: "unreliable")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "get_reader", lambda: None)
    monkeypatch.setattr(ocr, "read_lines", lambda image: [
        OcrLine(box=((200, 170), (500, 170), (500, 215), (200, 215)), text="다시 읽은 쪽 글자다.", score=0.9)])

    def notice(state, *argv):
        html = tmp_path / f"{len(argv)}.view.html"
        assert run(capsys, "view", path, "--db", state, "--no-layout", "--out", html, *argv)[0] == 0
        data = re.search(r'id="ko-data">(.*?)</script>', html.read_text(encoding="utf-8"), re.DOTALL).group(1)
        return json.loads(data)["pages"][0]["notice"]

    assert notice(db) == "글자층이 깨져 OCR로 다시 읽음(검증 전)"
    assert notice(tmp_path / "off.db", "--no-ocr") == "글자층이 깨져 블록 글자를 믿기 어렵다(신뢰도 0.2 이하)"
