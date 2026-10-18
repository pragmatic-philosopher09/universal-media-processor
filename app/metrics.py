"""Privacy-preserving product metrics for the MediaFlow workspace.

In-memory aggregate counters only: no URLs, filenames, IPs or user identifiers are recorded.
The north-star metric is a *successful session*: a finished job whose output was actually
retrieved (downloaded, zipped) by the user.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

WEEK_SECONDS = 7 * 24 * 3600


@dataclass
class Metrics:
    enabled: bool = True
    started_at: float = field(default_factory=time.time)
    jobs_created: Counter = field(default_factory=Counter)  # by mode
    inputs_created: Counter = field(default_factory=Counter)  # by input kind
    jobs_finished: Counter = field(default_factory=Counter)  # by status
    platform_results: dict[str, Counter] = field(default_factory=dict)
    outcome_results: dict[str, Counter] = field(default_factory=dict)
    reviews: Counter = field(default_factory=Counter)  # started / approved
    successful_sessions: int = 0
    _session_times: deque = field(default_factory=lambda: deque(maxlen=10_000))
    _ttfo: deque = field(default_factory=lambda: deque(maxlen=1_000))
    _counted_jobs: set = field(default_factory=set)

    def job_created(self, mode: str, input_kinds: list[str], review: bool) -> None:
        if not self.enabled:
            return
        self.jobs_created[mode] += 1
        for kind in input_kinds:
            self.inputs_created[kind] += 1
        if review:
            self.reviews["started"] += 1

    def review_approved(self) -> None:
        if self.enabled:
            self.reviews["approved"] += 1

    def job_finished(self, mode: str, status: str, platforms: list[tuple[str, bool]]) -> None:
        if not self.enabled:
            return
        self.jobs_finished[status] += 1
        self.outcome_results.setdefault(mode, Counter())[status] += 1
        for platform, ok in platforms:
            self.platform_results.setdefault(platform, Counter())["ok" if ok else "failed"] += 1

    def output_retrieved(self, job_id: str, created_at: float) -> None:
        """Count the first retrieval of a finished job's output as one successful session."""
        if not self.enabled or job_id in self._counted_jobs:
            return
        self._counted_jobs.add(job_id)
        now = time.time()
        self.successful_sessions += 1
        self._session_times.append(now)
        self._ttfo.append(max(0.0, now - created_at))

    def forget(self, job_id: str) -> None:
        self._counted_jobs.discard(job_id)

    def snapshot(self) -> dict:
        now = time.time()
        recent = sum(1 for t in self._session_times if now - t <= WEEK_SECONDS)
        ttfo = sorted(self._ttfo)
        median = ttfo[len(ttfo) // 2] if ttfo else None
        created = sum(self.jobs_created.values())
        done = self.jobs_finished.get("done", 0)
        return {
            "since": self.started_at,
            "north_star": {
                "name": "successful_sessions",
                "description": "Finished jobs whose output was downloaded by the user",
                "total": self.successful_sessions,
                "last_7_days": recent,
            },
            "time_to_first_output_seconds": {
                "median": round(median, 1) if median is not None else None,
                "samples": len(ttfo),
            },
            "jobs": {
                "created": created,
                "by_mode": dict(self.jobs_created),
                "inputs_by_kind": dict(self.inputs_created),
                "finished_by_status": dict(self.jobs_finished),
                "success_rate": round(done / created, 3) if created else None,
            },
            "review": dict(self.reviews),
            "platforms": {k: dict(v) for k, v in sorted(self.platform_results.items())},
            "outcomes": {k: dict(v) for k, v in sorted(self.outcome_results.items())},
        }
