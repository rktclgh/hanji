"""hanji 명령줄. main(argv)는 종료 코드를 돌려준다."""

import argparse
import json
import os
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hanji_contracts import DocumentTree

from . import models as model_files
from .engine import LocalEngine
from .errors import HanjiError, exit_code
from .export import asset_name, to_markdown
from .formats.detect import default_parsers
from .formats.pdf import layout as layout_runtime
from .formats.pdf.parser import MIME as PDF_MIME
from .store.sqlite import DB_ENV, SqliteStore, resolve_db
from .viewer import DEFAULT_DPI, render_html, render_page_images

APP_NAME = "hanji"
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2  # 문서 오류 코드(3·4·5)는 errors.exit_code
MAX_DPI = 600
OCR_HELP = ("스캔 쪽과 글자층이 깨진(unreliable) 쪽의 OCR을 끈다. 끄거나 OCR 추가 설치가 없으면 unreliable 쪽은 깨진 "
            "글자층으로 블록을 만든다(신뢰도 0.2 이하) (기본: OCR 추가 설치가 있으면 켠다. 원본이 같으면 저장된 버전을 "
            "쓰니 바꾸려면 parse --force)")
LAYOUT_HELP = ("레이아웃 모델(선·도형 그림·캡션)을 끈다. 사진(이미지 객체)은 그대로 그림 (기본: 레이아웃 추가 설치가 있으면 "
               "켠다. 원본이 같으면 저장된 버전을 쓰니 바꾸려면 parse --force)")
FETCH_TO_HELP = "받을 폴더 (기본: 사용자 캐시). 폐쇄망은 이 폴더를 옮겨 HANJI_MODEL_DIR로 가리킨다"
ASSETS_HELP = ("그림 이미지를 이 폴더에 <sha256 앞 16자>.png로 쓴다(--format md면 마크다운이 그 파일을 가리킨다. 링크는 "
               "--out 파일 폴더 기준 상대 경로, 표준 출력이면 현재 폴더 기준)")


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _dpi(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_DPI:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_DPI}")
    return value


def _non_empty(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("must not be empty")
    return text


def _configure_streams() -> None:
    """stdout은 UTF-8 고정, stderr는 인코딩 못 하는 글자(짝 없는 서로게이트 등)를 이스케이프해 예외를 막는다."""
    for stream, errors in ((sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # 콘솔 기본 인코딩(Windows cp949 등)에 기대지 않는다
            reconfigure(encoding="utf-8", errors=errors)


def _build_parser() -> argparse.ArgumentParser:
    db_help = f"상태 파일 경로 (기본: ${DB_ENV} 또는 사용자 데이터 폴더/state.db)"
    common = argparse.ArgumentParser(add_help=False)
    # 하위 명령 기본값이 최상위 값을 덮어쓰지 않도록 SUPPRESS. 둘 다 주면 뒤(하위 명령 쪽)가 이긴다.
    common.add_argument("--db", default=argparse.SUPPRESS, help=db_help)
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--format", choices=("json", "md"), default="json")
    output.add_argument("--out", help="출력 파일 경로 (기본: 표준 출력)")

    parser = argparse.ArgumentParser(prog=APP_NAME, description="한국어도 잘하는 범용 문서 파서")
    parser.add_argument("--db", help=db_help)
    sub = parser.add_subparsers(dest="command", required=True)
    parse = sub.add_parser("parse", parents=[common, output], help="파일을 파싱·저장하고 문서 트리를 출력")
    parse.add_argument("file")
    parse.add_argument("--id", dest="document_id", type=_non_empty, help="문서 ID (기본: doc_ + 원본 sha256 앞 24자리)")
    parse.add_argument("--force", action="store_true", help="원본이 같아도 다시 파싱")
    parse.add_argument("--no-ocr", action="store_true", help=OCR_HELP)
    parse.add_argument("--no-layout", action="store_true", help=LAYOUT_HELP)
    export = sub.add_parser("export", parents=[common, output], help="저장된 문서 트리를 출력")
    export.add_argument("document_id")
    export.add_argument("--version", type=_positive)
    export.add_argument("--assets", type=_non_empty, help=ASSETS_HELP)
    sub.add_parser("documents", parents=[common], help="문서마다 최신 버전")
    changes = sub.add_parser("changes", parents=[common], help="커서 뒤의 변경 내역")
    changes.add_argument("--cursor", type=_non_negative)
    changes.add_argument("--limit", type=_positive, default=100)
    history = sub.add_parser("history", parents=[common], help="처리 이력")
    history.add_argument("document_id")
    history.add_argument("--version", type=_positive)
    view = sub.add_parser("view", parents=[common], help="파싱 결과를 HTML 한 장으로 만든다(원본이 같으면 저장된 버전)")
    view.add_argument("file")
    view.add_argument("--id", dest="document_id", type=_non_empty, help="문서 ID (기본: doc_ + 원본 sha256 앞 24자리)")
    view.add_argument("--out", type=_non_empty, help="HTML 경로 (기본: 현재 폴더/<파일 이름(확장자 제외)>.view.html)")
    view.add_argument("--dpi", type=_dpi, default=DEFAULT_DPI, help=f"쪽 이미지 해상도 (기본 {DEFAULT_DPI})")
    view.add_argument("--no-ocr", action="store_true", help=OCR_HELP)
    view.add_argument("--no-layout", action="store_true", help=LAYOUT_HELP)
    models = sub.add_parser("models", help="모델 파일(OCR·레이아웃)")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    fetch = models_sub.add_parser("fetch", help="모델 파일을 업스트림 고정 주소에서 받아 크기·SHA-256을 확인한다")
    fetch.add_argument("variant", nargs="?", choices=(*model_files.variants(), "all"), default="all",
                       help="받을 모델 (기본: all)")
    fetch.add_argument("--to", metavar="DIR", type=_non_empty, help=FETCH_TO_HELP)
    return parser


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write(text: str, out: str | None) -> None:
    if out is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(path, text.encode("utf-8"))


def _emit_tree(tree: DocumentTree, args: argparse.Namespace, assets_dir: str | None = None) -> None:
    _write(to_markdown(tree, assets_dir) if args.format == "md" else _json(tree.model_dump(mode="json")), args.out)


def _assets_link(assets: str, out: str | None) -> str:
    """마크다운 링크의 폴더 부분('/' 구분). --out이 있으면 그 파일 폴더 기준 상대 경로, 표준 출력이면 현재 폴더 기준
    입력 그대로. 상대 경로가 없거나(Windows 다른 드라이브) 드라이브가 붙은 경로를 표준 출력에 쓰면 file:// URI.
    두 폴더는 실제 위치로(부모의 심볼릭 링크·'..'를 파일 시스템처럼 풀어) 비교한다. --out 파일 자체는 따라가지
    않는다(심볼릭 링크여도 그 자리를 바꿔 쓴다)."""
    folder = Path(assets)
    try:
        if out is not None:
            return Path(os.path.relpath(folder.resolve(), Path(out).parent.resolve())).as_posix()
        if not folder.drive:  # C:·UNC 경로가 C:/… 같은 스킴 모양 링크가 되지 않게
            return folder.as_posix()
    except ValueError:  # Windows: 드라이브가 다르면 상대 경로가 없다
        pass
    return folder.resolve().as_uri()


_POSIX_MODES = sys.platform != "win32" and hasattr(os, "fchmod")


def _new_file_mode() -> int:
    """새 파일의 umask 기본 권한(mkstemp는 0600으로 만든다)."""
    umask = os.umask(0)  # umask는 읽으려면 바꿔야 한다(CLI는 한 스레드라 바로 되돌린다)
    os.umask(umask)
    return 0o666 & ~umask


def _path_key(path: Path) -> str:
    """같은 파일인지 비교할 열쇠: 절대 경로(심볼릭 링크 풀기)를 NFC·대소문자 무시로. 대소문자·정규화를 가리는
    파일 시스템에서는 다른 파일도 같다고 볼 수 있지만, 쓰기 전에 거절하는 쪽이 안전하다."""
    return unicodedata.normalize("NFC", os.path.normcase(str(path.resolve()))).casefold()


def _asset_paths(tree: DocumentTree, folder: Path, out: str | None) -> dict[str, Path]:
    """그림 이미지마다 쓸 경로(같은 이미지는 한 번). --out이 폴더나 이미지 파일과 겹치면 아무것도 쓰기 전에 ValueError."""
    paths = {asset: folder / asset_name(asset)
             for asset in dict.fromkeys(b.figure.asset for b in tree.blocks if b.figure is not None)}
    if out is not None:
        key = _path_key(Path(out))
        if key == _path_key(folder) or any(key == _path_key(path) for path in paths.values()):
            raise ValueError(f"output path is also an asset file: {out}")
    return paths


def _write_assets(engine: LocalEngine, paths: dict[str, Path], folder: Path) -> None:
    """그림 이미지를 쓴다. 없는 이미지는 AssetNotFound. 같은 폴더의 임시 파일에 쓴 뒤 바꿔 끼운다: 그 이름이
    심볼릭 링크여도 링크가 가리키는 파일을 덮지 않고 링크 자리를 PNG로 바꾼다."""
    folder.mkdir(parents=True, exist_ok=True)
    for asset, path in paths.items():
        _replace_file(path, engine.get_asset(asset))


def _replace_file(path: Path, data: bytes) -> None:
    """같은 폴더의 새 임시 파일(배타적으로 만든 고유 이름)에 다 쓴 뒤 바꿔 끼운다: 실패해도 이전 파일이 반쯤 덮이지
    않고, 그 이름이 심볼릭 링크면 가리키는 파일을 덮지 않고 링크 자리를 바꾼다. 권한은 경로가 아니라 fd로 정한다:
    이미 있던 보통 파일의 권한, 새 파일·심볼릭 링크 자리는 umask 기본 권한(mkstemp는 0600으로 만든다). Windows는
    권한을 건드리지 않는다(읽기 전용 임시 파일이 남지 않게). 어떤 오류든 임시 파일을 지우고 다시 던진다."""
    path = path.parent.resolve() / path.name  # 부모의 심볼릭 링크·'..'는 파일 시스템처럼 푼다(마지막 이름은 그대로)
    mode = None
    if _POSIX_MODES:
        try:
            st = os.lstat(path)
            mode = stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else _new_file_mode()
        except FileNotFoundError:
            mode = _new_file_mode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")  # 짧은 이름: 긴 출력 이름도 이름 길이 한도를 넘지 않게
    try:
        with os.fdopen(fd, "wb") as f:  # 나갈 때(오류여도) fd를 닫는다
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:  # 지우지 못해도 원래 오류를 가리지 않는다
            pass
        raise


def view_path(file: str, out: str | None) -> Path:
    """--out이 없으면 현재 폴더의 <파일 이름(확장자 제외, NFC)>.view.html."""
    if out:
        return Path(out)
    return Path.cwd() / (unicodedata.normalize("NFC", Path(file).stem) + ".view.html")


def _view(args: argparse.Namespace, engine: LocalEngine) -> None:
    data = Path(args.file).read_bytes()  # 한 번만 읽어 수집과 쪽 그림에 같은 바이트를 쓴다
    ref = engine.ingest_bytes(data, Path(args.file).name, document_id=args.document_id)
    tree = engine.get_tree(ref.document_id, ref.version)
    previous = engine.get_tree(ref.document_id, ref.version - 1) if ref.version > 1 else None
    images = None
    if tree.source.mime == PDF_MIME:
        images = render_page_images(data, tree.source.name, args.dpi)
    out = view_path(args.file, args.out)
    notice = tree.source.mime == PDF_MIME and not args.no_layout and not layout_runtime.available()
    html = render_html(tree, images, previous, layout_notice=notice,
                       history=engine.history(ref.document_id, ref.version))
    out.parent.mkdir(parents=True, exist_ok=True)
    # 문서 글자에 짝 없는 서로게이트가 있어도 쓴다(인코딩 못 하는 글자는 "?")
    _replace_file(out, html.encode("utf-8", errors="replace"))
    _write(f"{out}\n", None)


def _fetch_models(args: argparse.Namespace) -> None:
    """받은(또는 이미 있던) 파일 경로를 한 줄씩. 받기 시작할 때마다 표준 오류에 한 줄."""
    def report(entry: model_files.ModelFile, url: str) -> None:
        print(f"{APP_NAME}: downloading {entry.path} ({entry.size:,} bytes) from {url}", file=sys.stderr, flush=True)

    for path in model_files.fetch(None if args.variant == "all" else [args.variant], args.to, report):
        _write(f"{path}\n", None)


def _run(args: argparse.Namespace, engine: LocalEngine) -> None:
    match args.command:
        case "parse":
            ref = engine.ingest(args.file, document_id=args.document_id, force=args.force)
            _emit_tree(engine.get_tree(ref.document_id, ref.version), args)
        case "export":
            tree = engine.get_tree(args.document_id, args.version)
            if args.assets:
                _write_assets(engine, _asset_paths(tree, Path(args.assets), args.out), Path(args.assets))
            _emit_tree(tree, args, _assets_link(args.assets, args.out) if args.assets else None)
        case "documents":
            _write(_json([ref.model_dump(mode="json") for ref in engine.documents()]), None)
        case "changes":
            _write(_json(engine.changes(args.cursor, args.limit).model_dump(mode="json")), None)
        case "history":
            _write(_json(engine.history(args.document_id, args.version).model_dump(mode="json")), None)
        case "view":
            _view(args, engine)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_streams()
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse: 사용법 오류 2, --help 0
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        if args.command == "models":  # 모델 받기는 상태 파일을 열지 않는다
            _fetch_models(args)
            return EXIT_OK
        out = (view_path(args.file, args.out) if args.command == "view"
               else args.out if args.command in ("parse", "export") else None)
        if out and Path(out).is_dir():  # 저장소·그림 파일을 건드리기 전에 막는다(view는 기본 출력 경로도)
            raise IsADirectoryError(f"output path is a directory: {out}")
        with SqliteStore(resolve_db(args.db)) as store:
            no_ocr, no_layout = getattr(args, "no_ocr", False), getattr(args, "no_layout", False)
            parsers = (default_parsers(ocr=False if no_ocr else None, layout=False if no_layout else None)
                       if no_ocr or no_layout else None)
            _run(args, LocalEngine(store, parsers))
    except HanjiError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return exit_code(exc)
    except Exception as exc:  # 그 밖(파일 없음·권한 등)
        print(f"{APP_NAME}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK
