"""The AI Copilot: an optional retrieval and explanation layer over the platform.

The platform below is deterministic and governed. KPI meaning comes from
approved, immutable versions; numbers come from formula contracts compiled to
SQL and pushed down to the tenant's own database; profiling, join safety and
reconciliation are computed and stored. None of that changes here.

What this package adds is a way to *ask about* that material in English. It has
no privileges of its own: every read goes through the same ``AccessContext``,
the same permission keys and the same company-scoped loaders the REST API uses.
The model never sees a database handle, a connection string, a credential or a
SQL string it authored -- it sees the output of a small set of named, validated
tools, and it answers from that or says it does not know.

Layout:

* ``context``   -- the resolved, company-scoped context one turn runs inside
* ``tools/``    -- the governed tool layer; the model's entire vocabulary
* ``evidence``  -- what an answer is allowed to be built from, and cite
* ``retrieval`` -- company-scoped search over governed knowledge
* ``prompts``   -- the single place the Copilot's rules are written down
* ``orchestrator`` -- assembles context, evidence and tools around the provider
"""

from __future__ import annotations

from app.copilot.context import CopilotContext, build_context
from app.copilot.evidence import EvidenceBundle, EvidenceItem

__all__ = ["CopilotContext", "EvidenceBundle", "EvidenceItem", "build_context"]
