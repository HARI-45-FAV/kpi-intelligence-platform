"""Diagnose the Comparison Policy pipeline without calling a model.

Reads the real uploaded documents from ``data/documents`` and reports, per file:
document text extraction, lexical retrieval, and what the retrieved passages
would put in front of the model. No LLM call is made, so this costs nothing and
can be run as often as needed.

    python scripts/diag_bucket_extraction.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# A handbook is prose written by a person: it contains arrows, dashes and
# accented names that a Windows console's default cp1252 cannot encode. Reading
# the document must not fail because of how the terminal prints it.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass

from app.copilot.text import chunk_text, extract_text, unreadable_reason  # noqa: E402
from app.services.bucket_extraction import (  # noqa: E402
    RETRIEVAL_CHAR_BUDGET,
    MAX_SCAN_CHARS,
)
from app.services.bucket_retrieval import retrieve_policy_passages  # noqa: E402


def report(path: Path) -> None:
    data = path.read_bytes()
    print("=" * 78)
    print(f"FILE      {path.name}")
    print(f"BYTES     {len(data):,}")

    text = extract_text(data, None, path.name)
    if text is None:
        print("EXTRACT   FAILED ->", unreadable_reason(None, path.name))
        return
    print(f"EXTRACT   OK, {len(text):,} characters")

    chunks = chunk_text(text[:MAX_SCAN_CHARS], target_chars=900)
    print(f"CHUNKS    {len(chunks)} passages")

    retrieval = retrieve_policy_passages(
        text[:MAX_SCAN_CHARS], char_budget=RETRIEVAL_CHAR_BUDGET
    )
    if retrieval.empty:
        print("RETRIEVAL EMPTY -- no passage mentions comparison policy.")
        print("          => extraction returns NEEDS_REVIEW without calling the model.")
        return

    print(
        f"RETRIEVAL {len(retrieval.passages)}/{retrieval.total_passages} passages, "
        f"{retrieval.selected_characters:,}/{retrieval.document_characters:,} chars"
    )
    for passage in retrieval.passages:
        print(f"  [{passage.ordinal:>3}] score={passage.score:6.2f}  {passage.matched[:8]}")
        excerpt = " ".join(passage.text.split())[:200]
        print(f"        {excerpt}")
    print()
    print("--- PROMPT EXCERPT THE MODEL WOULD SEE (first 1500 chars) ---")
    print(retrieval.text[:1500])


def main() -> int:
    store = ROOT / "data" / "documents"
    files = sorted(p for p in store.rglob("*") if p.is_file())
    if not files:
        print(f"No documents under {store}")
        return 1
    for path in files:
        report(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
