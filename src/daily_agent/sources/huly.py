"""Huly (task tracking) source — STUB.

Awaiting access details before wiring up for real. Huly self-hosted/cloud
exposes an API; once you provide a base URL + token I'll implement:
  * issues / tasks by project,
  * recent status changes,
  * linking a GitHub repo to its Huly project.

For now this raises a clear NotConfigured so the rest of the system runs and
the deep-dive agent simply notes Huly is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass


class HulyNotConfigured(RuntimeError):
    pass


@dataclass
class HulyClient:
    base_url: str = ""
    token: str = ""

    def _require(self) -> None:
        if not self.base_url or not self.token:
            raise HulyNotConfigured(
                "Huly is not configured yet. Provide DAILY_AGENT_HULY_URL and "
                "DAILY_AGENT_HULY_TOKEN to enable task-tracker context."
            )

    async def project_tasks(self, project: str) -> list[dict]:
        self._require()
        raise NotImplementedError("Huly integration pending access details.")
