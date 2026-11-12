"""대규모 실행의 문서 하나(새 하위 프로세스에서 돈다). 표준 출력에 JSON 한 줄(숫자·열거값·해시만)을 쓴다.

  ingest <파일> <문서 번호>  새 임시 SqliteStore + ingest(force=True), 기본 설정
  pair <원본> <PDF>           원본 블록 글자(표는 칸 글자)와 PDF 원시 글자층의 한글 음절 다중집합 크기만
"""

import hashlib
import json
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

DISTRIBUTION = "distribution_preview_only"


def _maxrss() -> int | None:
    """지금까지 이 프로세스의 최대 RSS(바이트). macOS는 바이트, 리눅스는 KiB로 준다. Windows는 None."""
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest(path: Path, doc: str) -> dict:
    from hanji import LocalEngine, SqliteStore

    with tempfile.TemporaryDirectory(prefix="hanji-eval-") as tmp:
        store = SqliteStore(Path(tmp) / "store.sqlite")
        engine = LocalEngine(store)
        before = _maxrss()
        start = time.perf_counter()
        try:
            ref = engine.ingest(str(path), document_id=doc, force=True)
        except Exception as error:  # 오류 종류(클래스 이름)만 남긴다. 메시지는 글자를 담을 수 있어 버린다
            return {"status": "parse_error", "error": type(error).__name__, "seconds": time.perf_counter() - start,
                    "rss_before": before, "rss_peak": _maxrss()}
        seconds = time.perf_counter() - start
        peak = _maxrss()
        tree = engine.get_tree(ref.document_id)
        history = engine.history(ref.document_id)
        reasons = Counter(r.fallback_reason for r in history.regions if r.fallback_reason)
        assets = sorted(b.figure.asset for b in tree.blocks if b.figure is not None)
        store.close()
    return {
        "status": "created", "error": None, "seconds": seconds, "rss_before": before, "rss_peak": peak,
        "pages": tree.source.page_count, "kinds": dict(Counter(b.kind for b in tree.blocks)), "reasons": dict(reasons),
        "tree_sha256": _sha(tree.model_dump_json()), "history_sha256": _sha(history.model_dump_json()),
        "assets_sha256": _sha("\n".join(assets)), "distribution": DISTRIBUTION in reasons,
    }


def _hangul(text: str) -> Counter[str]:
    return Counter(ch for ch in unicodedata.normalize("NFKC", text) if "가" <= ch <= "힣")


def pair(original: Path, pdf: Path) -> dict:
    import pypdfium2
    from hanji import LocalEngine, MemoryStore
    from hanji.formats.detect import default_parsers

    engine = LocalEngine(MemoryStore(), default_parsers(ocr=False, layout=False))
    tree = engine.get_tree(engine.ingest(str(original), force=True).document_id)
    parts = []
    for b in tree.blocks:
        parts += [c.text for c in b.table.cells] if b.table is not None else [b.text]
    h = _hangul("".join(parts))
    document = pypdfium2.PdfDocument(str(pdf))
    try:
        texts = []
        for i in range(len(document)):
            page = document[i]
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range())
            textpage.close()
            page.close()
    finally:
        document.close()
    p = _hangul("".join(texts))
    return {"hangul_original": sum(h.values()), "hangul_pdf": sum(p.values()), "common": sum((h & p).values()),
            "union": sum((h | p).values()), "set_common": len(h.keys() & p.keys()),
            "set_union": len(h.keys() | p.keys())}


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "ingest":
        out = ingest(Path(argv[1]), argv[2])
    elif len(argv) == 3 and argv[0] == "pair":
        out = pair(Path(argv[1]), Path(argv[2]))
    else:
        print("usage: worker ingest <file> <doc> | pair <original> <pdf>", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
