"""Initiative catalog — the set of real initiatives derived from Huly.

The catalog is the fixed menu the LLM mapper assigns PRs onto. Building it from
Huly's parent tree (via the deterministic resolver) is what keeps initiative
identity stable and anchored to Huly: the LLM never invents an initiative, it
only picks from this catalog (or "untracked").
"""

from __future__ import annotations

from .initiative import Initiative, resolve_initiative


def build_catalog(issues: list[dict]) -> list[Initiative]:
    """Distinct ``initiative``-lane subjects across the given Huly issues.

    Each active issue resolves (through its parent chain) to its top initiative;
    the distinct set of those is the catalog. Ops/untracked are fixed lanes, not
    catalog entries, so they're excluded.
    """
    catalog: dict[str, Initiative] = {}
    for issue in issues:
        init = resolve_initiative(issue)
        if init.lane == "initiative" and init.key not in catalog:
            catalog[init.key] = init
    return list(catalog.values())
