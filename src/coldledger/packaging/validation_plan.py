from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return v.astimezone(timezone.utc)


def _fmt(v: datetime) -> str:
    return _utc(v).strftime("%Y-%m-%dT%H:%MZ")


@dataclass(frozen=True)
class Protocol:
    key: str
    version: str
    effective_from: datetime
    expires_at: datetime | None
    cargo_categories: frozenset[str]
    lane_climates: frozenset[str]
    duration: timedelta
    samples_required: int = 1

    def validate(self) -> None:
        if not self.key or not self.version:
            raise ValueError("protocol key and version required")
        if self.duration <= timedelta():
            raise ValueError("duration must be positive")
        if self.samples_required < 1:
            raise ValueError("samples_required must be positive")
        if self.expires_at is not None and _utc(self.expires_at) <= _utc(self.effective_from):
            raise ValueError("protocol expires before it becomes effective")

    def covers(self, cargo_category: str, lane_climate: str) -> bool:
        category_ok = "*" in self.cargo_categories or cargo_category in self.cargo_categories
        climate_ok = "*" in self.lane_climates or lane_climate in self.lane_climates
        return category_ok and climate_ok

    def valid_over(self, start: datetime, end: datetime) -> bool:
        if _utc(start) < _utc(self.effective_from):
            return False
        return self.expires_at is None or _utc(end) <= _utc(self.expires_at)


@dataclass(frozen=True)
class ValidationRequest:
    key: str
    protocol_key: str
    cargo_category: str
    lane_climate: str
    volume: float
    earliest: datetime
    latest: datetime
    priority: int = 0

    def validate(self) -> None:
        if not self.key or not self.protocol_key:
            raise ValueError("request and protocol key required")
        if self.volume <= 0:
            raise ValueError("volume must be positive")
        if _utc(self.latest) <= _utc(self.earliest):
            raise ValueError("invalid scheduling window")


@dataclass(frozen=True)
class Chamber:
    key: str
    capacity: float
    available_from: datetime
    available_until: datetime

    def validate(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if _utc(self.available_until) <= _utc(self.available_from):
            raise ValueError("invalid chamber window")


@dataclass(frozen=True)
class Sample:
    key: str
    cargo_category: str
    expires_at: datetime
    max_uses: int = 1

    def validate(self) -> None:
        if not self.key or not self.cargo_category:
            raise ValueError("sample key and cargo category required")
        if self.max_uses < 1:
            raise ValueError("max_uses must be positive")


@dataclass(frozen=True)
class QualificationSnapshot:
    request_key: str
    protocol_key: str
    protocol_version: str
    protocol_effective_from: datetime
    protocol_expires_at: datetime | None
    planned_at: datetime
    facts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BatchAssignment:
    request_key: str
    chamber_key: str
    start: datetime
    end: datetime
    sample_keys: tuple[str, ...]
    snapshot: QualificationSnapshot


@dataclass(frozen=True)
class Deferral:
    request_key: str
    reason: str
    explanation: str


@dataclass(frozen=True)
class Plan:
    planned_at: datetime
    assignments: tuple[BatchAssignment, ...]
    deferrals: tuple[Deferral, ...]

    @property
    def scheduled(self) -> tuple[str, ...]:
        return tuple(a.request_key for a in self.assignments)

    @property
    def deferred(self) -> tuple[str, ...]:
        return tuple(d.request_key for d in self.deferrals)

    def deferral_reasons(self) -> dict[str, str]:
        return {d.request_key: d.reason for d in self.deferrals}


def _find_slot(busy: list[tuple[datetime, datetime, str]], lower: datetime, duration: timedelta, upper: datetime) -> datetime | None:
    start = lower
    for busy_start, busy_end, _ in busy:
        if busy_end <= start:
            continue
        if start + duration <= busy_start:
            break
        start = busy_end
    return start if start + duration <= upper else None


def _allocate_samples(req: ValidationRequest, protocol: Protocol, samples: list[Sample], uses: dict[str, list], start: datetime, end: datetime) -> list[Sample] | None:
    pool = []
    for s in samples:
        if s.cargo_category != req.cargo_category:
            continue
        if _utc(s.expires_at) < end:
            continue
        history = uses.get(s.key, [])
        if len(history) >= s.max_uses:
            continue
        if any(bs < end and start < be for bs, be, _ in history):
            continue
        pool.append(s)
    pool.sort(key=lambda s: (_utc(s.expires_at), s.key))
    if len(pool) < protocol.samples_required:
        return None
    return pool[:protocol.samples_required]


def _protocol_failure(p: Protocol, req: ValidationRequest) -> str:
    lower = max(_utc(req.earliest), _utc(p.effective_from))
    if p.expires_at is not None and _utc(p.expires_at) < _utc(req.latest) and lower + p.duration > _utc(p.expires_at):
        return "expired"
    if _utc(p.effective_from) > _utc(req.earliest):
        return "not-yet-effective"
    return "window-too-short"


def _protocol_deferral(req: ValidationRequest, eligible: list[Protocol]) -> Deferral:
    ordered = sorted(eligible, key=lambda p: (_utc(p.effective_from), p.version), reverse=True)
    details = []
    for p in ordered:
        failure = _protocol_failure(p, req)
        if failure == "expired":
            details.append(f"{p.version} expired {_fmt(p.expires_at)}")
        elif failure == "not-yet-effective":
            details.append(f"{p.version} effective {_fmt(p.effective_from)} leaves no room in window")
        else:
            details.append(f"{p.version} needs {p.duration} longer than the window")
    reason = {"expired": "protocol-expired", "not-yet-effective": "protocol-not-yet-effective", "window-too-short": "window-too-short"}[_protocol_failure(ordered[0], req)]
    return Deferral(req.key, reason, f"no valid version of {req.protocol_key} for the window ({'; '.join(details)})")


def _chamber_deferral(req: ValidationRequest, fitting: list[Chamber], busy: dict[str, list]) -> Deferral:
    holders = []
    for c in sorted(fitting, key=lambda c: c.key):
        keys = sorted({key for bs, be, key in busy[c.key] if bs < _utc(req.latest) and be > _utc(req.earliest)})
        if keys:
            holders.append(f"{c.key} held by {', '.join(keys)}")
    detail = "; ".join(holders) if holders else "chambers unavailable inside window"
    return Deferral(req.key, "chamber-unavailable", f"no chamber slot inside window; {detail}")


def _sample_deferral(req: ValidationRequest, protocol: Protocol, samples: list[Sample], uses: dict[str, list], start: datetime, end: datetime) -> Deferral:
    free = 0
    expired = 0
    consumers: set[str] = set()
    for s in samples:
        if s.cargo_category != req.cargo_category:
            continue
        history = uses.get(s.key, [])
        if _utc(s.expires_at) < end:
            expired += 1
            continue
        if len(history) >= s.max_uses:
            consumers.update(key for _, _, key in history)
            continue
        overlapping = [key for bs, be, key in history if bs < end and start < be]
        if overlapping:
            consumers.update(overlapping)
        else:
            free += 1
    detail = f"need {protocol.samples_required} {req.cargo_category} samples valid until {_fmt(end)}; {free} uncommitted"
    if consumers:
        detail += f"; committed to {', '.join(sorted(consumers))}"
    if expired:
        detail += f"; {expired} expired"
    return Deferral(req.key, "insufficient-samples", detail)


def _place(req: ValidationRequest, protocols: list[Protocol], chambers: list[Chamber], samples: list[Sample], busy: dict[str, list], uses: dict[str, list], planned_at: datetime) -> tuple[BatchAssignment | None, Deferral | None]:
    eligible = [p for p in protocols if p.key == req.protocol_key and p.covers(req.cargo_category, req.lane_climate)]
    if not eligible:
        return None, Deferral(req.key, "no-eligible-protocol", f"no version of {req.protocol_key} covers {req.cargo_category} on {req.lane_climate} lanes")
    fitting = [c for c in chambers if c.capacity >= req.volume]
    if not fitting:
        cap = max((c.capacity for c in chambers), default=0.0)
        return None, Deferral(req.key, "chamber-capacity", f"payload {req.volume} exceeds every chamber capacity (max {cap})")
    validity_ok = False
    sample_failure = None
    ordered = sorted(eligible, key=lambda p: (_utc(p.effective_from), p.version), reverse=True)
    for p in ordered:
        lower = max(_utc(req.earliest), _utc(p.effective_from))
        upper = _utc(req.latest) if p.expires_at is None else min(_utc(req.latest), _utc(p.expires_at))
        if lower + p.duration > upper:
            continue
        validity_ok = True
        best = None
        for c in fitting:
            start = _find_slot(busy[c.key], max(lower, _utc(c.available_from)), p.duration, min(upper, _utc(c.available_until)))
            if start is not None and (best is None or (start, c.key) < (best[0], best[1].key)):
                best = (start, c)
        if best is None:
            continue
        start, chamber = best
        end = start + p.duration
        chosen = _allocate_samples(req, p, samples, uses, start, end)
        if chosen is None:
            sample_failure = _sample_deferral(req, p, samples, uses, start, end)
            continue
        snapshot = QualificationSnapshot(req.key, p.key, p.version, _utc(p.effective_from), _utc(p.expires_at) if p.expires_at else None, planned_at, (
            ("cargo_category", req.cargo_category),
            ("lane_climate", req.lane_climate),
            ("volume", str(req.volume)),
            ("chamber_key", chamber.key),
            ("chamber_capacity", str(chamber.capacity)),
        ))
        return BatchAssignment(req.key, chamber.key, start, end, tuple(s.key for s in chosen), snapshot), None
    if sample_failure is not None:
        return None, sample_failure
    if validity_ok:
        return None, _chamber_deferral(req, fitting, busy)
    return None, _protocol_deferral(req, eligible)


def plan(requests: Iterable[ValidationRequest], protocols: Iterable[Protocol], chambers: Iterable[Chamber], samples: Iterable[Sample], planned_at: datetime) -> Plan:
    reqs = list(requests)
    ps = list(protocols)
    cs = list(chambers)
    ss = list(samples)
    for p in ps:
        p.validate()
    for c in cs:
        c.validate()
    for s in ss:
        s.validate()
    at = _utc(planned_at)
    busy: dict[str, list[tuple[datetime, datetime, str]]] = {c.key: [] for c in cs}
    uses: dict[str, list[tuple[datetime, datetime, str]]] = {s.key: [] for s in ss}
    assignments: list[BatchAssignment] = []
    deferrals: list[Deferral] = []
    for req in sorted(reqs, key=lambda r: (-r.priority, _utc(r.latest), _utc(r.earliest), r.key)):
        req.validate()
        assignment, deferral = _place(req, ps, cs, ss, busy, uses, at)
        if assignment is None:
            deferrals.append(deferral)
            continue
        assignments.append(assignment)
        busy[assignment.chamber_key].append((assignment.start, assignment.end, req.key))
        busy[assignment.chamber_key].sort()
        for key in assignment.sample_keys:
            uses[key].append((assignment.start, assignment.end, req.key))
            uses[key].sort()
    return Plan(at, tuple(assignments), tuple(deferrals))


@dataclass(frozen=True)
class CarryoverItem:
    request_key: str
    status: str
    explanation: str


def carryover(previous: Plan, protocols: Iterable[Protocol], samples: Iterable[Sample], at: datetime) -> tuple[tuple[BatchAssignment, ...], tuple[CarryoverItem, ...]]:
    now = _utc(at)
    registry: dict[str, list[Protocol]] = {}
    for p in protocols:
        registry.setdefault(p.key, []).append(p)
    sample_map = {s.key: s for s in samples}
    valid: list[BatchAssignment] = []
    stale: list[CarryoverItem] = []
    for a in previous.assignments:
        snap = a.snapshot
        versions = registry.get(snap.protocol_key, [])
        current = next((p for p in versions if p.version == snap.protocol_version), None)
        if current is None:
            stale.append(CarryoverItem(a.request_key, "protocol-unknown", f"{snap.protocol_key} {snap.protocol_version} no longer in registry; re-qualify before reuse"))
            continue
        if current.expires_at is not None and _utc(current.expires_at) <= now:
            stale.append(CarryoverItem(a.request_key, "protocol-expired", f"{snap.protocol_key} {snap.protocol_version} expired {_fmt(current.expires_at)}; do not reuse"))
            continue
        newer = [p for p in versions if p.version != snap.protocol_version and _utc(p.effective_from) <= now and (_utc(p.effective_from), p.version) > (_utc(current.effective_from), current.version)]
        if newer:
            newest = max(newer, key=lambda p: (_utc(p.effective_from), p.version))
            stale.append(CarryoverItem(a.request_key, "protocol-superseded", f"{snap.protocol_key} {newest.version} effective {_fmt(newest.effective_from)} supersedes {snap.protocol_version}; explicit re-confirmation required"))
            continue
        dead = sorted(k for k in a.sample_keys if k in sample_map and _utc(sample_map[k].expires_at) <= now)
        missing = sorted(k for k in a.sample_keys if k not in sample_map)
        if dead or missing:
            detail = f"samples {dead} expired" if dead else ""
            if missing:
                detail += f"; {missing} unknown" if detail else f"samples {missing} unknown"
            stale.append(CarryoverItem(a.request_key, "sample-expired", detail))
            continue
        valid.append(a)
    return tuple(valid), tuple(stale)


def sample_usage(p: Plan) -> dict[str, tuple[tuple[datetime, datetime, str], ...]]:
    out: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for a in p.assignments:
        for key in a.sample_keys:
            out.setdefault(key, []).append((a.start, a.end, a.request_key))
    return {key: tuple(sorted(rows)) for key, rows in sorted(out.items())}


def sample_conflicts(p: Plan, samples: Iterable[Sample] = ()) -> list[str]:
    limits = {s.key: s.max_uses for s in samples}
    problems = []
    for key, rows in sample_usage(p).items():
        for (a_start, a_end, a_key), (b_start, b_end, b_key) in zip(rows, rows[1:]):
            if b_start < a_end:
                problems.append(f"{key}: {a_key} and {b_key} overlap")
        if key in limits and len(rows) > limits[key]:
            problems.append(f"{key}: used {len(rows)} times above max_uses {limits[key]}")
    return problems


def explain(p: Plan) -> list[str]:
    lines = [f"{a.request_key} -> {a.chamber_key} {_fmt(a.start)}..{_fmt(a.end)} via {a.snapshot.protocol_key} {a.snapshot.protocol_version} samples {','.join(a.sample_keys)}" for a in p.assignments]
    lines.extend(f"{d.request_key} deferred ({d.reason}): {d.explanation}" for d in p.deferrals)
    return lines
