"""End-to-end bucket extraction against the real handbook, with a mocked model.

Runs the full ``extract_bucket_config`` pipeline -- retrieval, quarantine, date
grounding, budget forcing, validation -- against the documents actually stored in
``data/documents``, using a stub provider that returns what a competent model
returns when shown those passages. No network call, no API quota.

    python scripts/diag_bucket_llm_mock.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from app.copilot.text import extract_text  # noqa: E402
from app.llm.config import get_llm_config  # noqa: E402
from app.llm.provider import LLMProvider, LLMResponse, LLMUsage  # noqa: E402
from app.services.bucket_extraction import extract_bucket_config  # noqa: E402

#: What a competent model returns having read the retrieved handbook passages.
#: The weekday/week/month/event values are all literally stated in the document
#: ("Friday ~1.20x; Saturday ~1.25x", "Week 3 ~1.15x", "January Orders ~1.10x",
#: "NovaFest (15-21 Oct) ~1.40x", "2026 ~1.12x corresponding 2025 period").
HANDBOOK_ANSWER = {
    "same_day_of_week": {"enabled": True, "days": ["Friday", "Saturday"]},
    "same_week_of_month": {"enabled": True, "weeks": [3]},
    "same_month_or_season": {"enabled": True, "months": ["January"]},
    "business_event": {
        "enabled": True,
        # The document writes the window as "15-21 Oct" with no year, so a model
        # supplying full ISO dates is supplying a year the document never states.
        "events": [
            {
                "name": "NovaFest",
                "dates": [
                    "2025-10-15", "2025-10-16", "2025-10-17", "2025-10-18",
                    "2025-10-19", "2025-10-20", "2025-10-21",
                ],
            }
        ],
    },
    "yoy_period": {"enabled": True},
}


class StubModel(LLMProvider):
    def __init__(self, payload: dict) -> None:
        super().__init__(get_llm_config())
        self._payload = payload

    async def generate(self, messages, tools=None, stream=False):
        return LLMResponse(
            text=json.dumps(self._payload),
            model="mock/handbook-reader",
            usage=LLMUsage(prompt_tokens=1500, completion_tokens=180),
        )

    async def aclose(self) -> None:
        return None


async def run(path: Path, payload: dict) -> None:
    text = extract_text(path.read_bytes(), None, path.name)
    if text is None:
        print(f"{path.name}: not extractable")
        return

    draft = await extract_bucket_config(text, provider=StubModel(payload))

    print("=" * 78)
    print(f"DOCUMENT      {path.name}")
    print(f"MODEL         {draft.model}")
    print(f"NEEDS REVIEW  {draft.needs_review}")
    print(f"ENABLED SLOTS {[str(b) for b in draft.config.enabled_buckets] or 'NONE'}")
    print()
    print("RESULTING CONFIGURATION")
    print(json.dumps(draft.config.as_dict(), indent=2))
    if draft.review_reasons:
        print("\nREVIEW REASONS")
        for reason in draft.review_reasons:
            print(f"  - {reason}")
    if draft.config.warnings:
        print("\nWARNINGS")
        for warning in draft.config.warnings:
            print(f"  - {warning}")
    if draft.notes:
        print("\nNOTES")
        for note in draft.notes:
            print(f"  - {note}")


async def main() -> None:
    store = ROOT / "data" / "documents"
    handbook = next(
        (p for p in sorted(store.rglob("*.docx")) if "Detection_Engine" in p.name), None
    )
    if handbook is None:
        print("No handbook found under data/documents")
        return
    await run(handbook, HANDBOOK_ANSWER)


if __name__ == "__main__":
    asyncio.run(main())
