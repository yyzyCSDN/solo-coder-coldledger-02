from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .sample_pool import SamplePool
from .validation_snapshot import (
    ClimateEnvelope,
    PinnedQualification,
    PlanningFacts,
    SnapshotRegistry,
    seal,
)


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return v.astimezone(timezone.utc)


@dataclass(frozen=True)
class Protocol:
    protocol_id: str
    version: str
    cargo_class: str
    effective_from: datetime
    expires_at: datetime | None = None

    def active(self, at: datetime) -> bool:
        now = _utc(at)
        if now < _utc(self.effective_from):
            return False
        return self.expires_at is None or now < _utc(self.expires_at)


@dataclass(frozen=True)
class Qualification:
    qualification_id: str
    package: str
    protocol_id: str
    protocol_version: str
    cargo_class: str
    lane_class: str
    min_ambient: float
    max_ambient: float
    hours: float
    payload_min: float
    payload_max: float
    effective_from: datetime
    expires_at: datetime
    report_ref: str

    def pinned(self) -> PinnedQualification:
        return PinnedQualification(
            self.qualification_id, self.package, self.protocol_id, self.protocol_version,
            self.cargo_class, self.lane_class,
            ClimateEnvelope(self.min_ambient, self.max_ambient, self.hours),
            self.payload_min, self.payload_max, self.effective_from, self.expires_at,
            self.report_ref,
        )


@dataclass(frozen=True)
class LaneClimate:
    """Seasonal ambient envelope observed/forecast on a lane."""
    lane: str
    season: str
    lane_class: str
    forecast_low: float
    forecast_high: float


@dataclass(frozen=True)
class Chamber:
    chamber_id: str
    capacity_kg: float
    cargo_classes: frozenset[str]
    setup: timedelta = timedelta()


@dataclass(frozen=True)
class _Reservation:
    start: datetime
    end: datetime
    load_kg: float
    batch_id: str
    prev_batch: str | None


@dataclass(frozen=True)
class ValidationDemand:
    batch_id: str
    cargo_class: str
    lane: str
    season: str
    transit_hours: float
    payload_kg: float
    duration: timedelta
    earliest: datetime
    latest: datetime
    priority: int = 0
    sample_count: int = 1

    def validate(self) -> None:
        if not self.batch_id or not self.cargo_class or not self.lane or not self.season:
            raise ValueError("batch_id, cargo_class, lane and season required")
        if self.transit_hours <= 0 or self.payload_kg <= 0:
            raise ValueError("transit_hours and payload_kg must be positive")
        if self.duration <= timedelta():
            raise ValueError("duration must be positive")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if _utc(self.latest) <= _utc(self.earliest):
            raise ValueError("invalid scheduling window")


@dataclass(frozen=True)
class PlannedBatch:
    batch_id: str
    qualification_id: str
    protocol_id: str
    protocol_version: str
    chamber_id: str
    sample_ids: tuple[str, ...]
    start: datetime
    end: datetime
    snapshot_id: str
    thermal_margin: float


@dataclass(frozen=True)
class RejectedDemand:
    batch_id: str
    causes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ValidationPlan:
    run_id: str
    planned_at: datetime
    batches: tuple[PlannedBatch, ...]
    rejected: tuple[RejectedDemand, ...]

    @property
    def complete(self) -> bool:
        return not self.rejected

    def explain(self) -> list[str]:
        lines = [f"run {self.run_id}: {len(self.batches)} scheduled, {len(self.rejected)} rejected"]
        for r in self.rejected:
            lines.append(f"{r.batch_id} unscheduled:")
            for code, detail in r.causes:
                lines.append(f"  - {code}: {detail}")
        return lines


def _version_key(version: str):
    parts = []
    for chunk in version.split("."):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


def active_protocol(cargo_class: str, at: datetime, protocols: Iterable[Protocol]) -> tuple[Protocol | None, tuple[str, str] | None]:
    rows = sorted((p for p in protocols if p.cargo_class == cargo_class and p.active(at)),
                  key=lambda p: (_version_key(p.version), p.protocol_id))
    if not rows:
        return None, ("no_effective_protocol", f"no active protocol for cargo class {cargo_class}")
    newest = rows[-1]
    return newest, None


def _qualification_blocker(q: Qualification, protocol: Protocol, climate: LaneClimate,
                           transit_hours: float, payload_kg: float, at: datetime) -> tuple[str, str] | None:
    now = _utc(at)
    if q.protocol_id != protocol.protocol_id:
        return ("protocol_mismatch", f"{q.qualification_id} qualified under {q.protocol_id}, effective is {protocol.protocol_id}")
    if q.protocol_version != protocol.version:
        return ("protocol_version_stale", f"{q.qualification_id} pinned to {q.protocol_id} {q.protocol_version}, effective is {protocol.version}")
    if q.cargo_class != protocol.cargo_class:
        return ("cargo_class_mismatch", f"{q.qualification_id} for {q.cargo_class}, demand is {protocol.cargo_class}")
    if q.lane_class not in {"*", climate.lane_class}:
        return ("lane_class_mismatch", f"{q.qualification_id} covers {q.lane_class}, lane is {climate.lane_class}")
    if now < _utc(q.effective_from) or now >= _utc(q.expires_at):
        return ("qualification_expired", f"{q.qualification_id} valid {q.effective_from.date()}..{q.expires_at.date()}")
    if climate.forecast_low < q.min_ambient or climate.forecast_high > q.max_ambient:
        return ("climate_envelope_insufficient",
                f"{q.qualification_id} covers [{q.min_ambient},{q.max_ambient}]C, "
                f"{climate.season} forecast [{climate.forecast_low},{climate.forecast_high}]C")
    if transit_hours > q.hours:
        return ("transit_hours_exceed", f"{q.qualification_id} covers {q.hours}h, transit is {transit_hours}h")
    if not q.payload_min <= payload_kg <= q.payload_max:
        return ("payload_out_of_range", f"{q.qualification_id} payload [{q.payload_min},{q.payload_max}]kg, demand is {payload_kg}kg")
    return None


def _chamber_load_at(reservations: list[_Reservation], at: datetime) -> float:
    return sum(r.load_kg for r in reservations if r.start <= at < r.end)


def _placement(chamber: Chamber, reservations: list[_Reservation], demand: ValidationDemand) -> tuple[datetime, datetime] | None:
    """Greedy earliest feasible slot honoring payload capacity and chamber class."""
    if demand.cargo_class not in chamber.cargo_classes:
        return None
    earliest, latest, duration = _utc(demand.earliest), _utc(demand.latest), demand.duration
    candidates = [earliest]
    candidates += [r.end for r in reservations if earliest < r.end < latest]
    for start in sorted(set(candidates)):
        prior_end = max((r.end for r in reservations if r.end <= start), default=None)
        setup = chamber.setup if prior_end == start else timedelta()
        s, e = start + setup, start + setup + duration
        if e > latest:
            continue
        overlaps = [r for r in reservations if r.start < e and r.end > s]
        checkpoints = [s] + [r.start for r in overlaps if s <= r.start < e]
        if all(_chamber_load_at(reservations, t) + demand.payload_kg <= chamber.capacity_kg
               for t in checkpoints):
            return s, e
    return None


def plan_batches(*, run_id: str, planned_at: datetime, demands: Iterable[ValidationDemand],
                 protocols: Iterable[Protocol], qualifications: Iterable[Qualification],
                 climates: Iterable[LaneClimate], chambers: Iterable[Chamber],
                 registry: SnapshotRegistry, samples: SamplePool,
                 snapshot_id_prefix: str = "SNAP") -> ValidationPlan:
    """Schedule validation batches and seal a qualification snapshot per batch.

    Nothing is reused implicitly: an expired/stale qualification never becomes a
    plan, and every rejection records why each option was ruled out.
    """
    at = _utc(planned_at)
    protocol_rows = list(protocols)
    quals = sorted(qualifications, key=lambda q: q.qualification_id)
    climate_map = {(c.lane, c.season): c for c in climates}
    chamber_rows = list(chambers)
    reservations: dict[str, list[_Reservation]] = {c.chamber_id: [] for c in chamber_rows}

    planned: list[PlannedBatch] = []
    rejected: list[RejectedDemand] = []
    seq = 0

    for demand in sorted(demands, key=lambda d: (-d.priority, _utc(d.latest), d.batch_id)):
        demand.validate()
        causes: list[tuple[str, str]] = []

        protocol, protocol_fail = active_protocol(demand.cargo_class, at, protocol_rows)
        if protocol_fail is not None:
            rejected.append(RejectedDemand(demand.batch_id, (protocol_fail,)))
            continue

        climate = climate_map.get((demand.lane, demand.season))
        if climate is None:
            rejected.append(RejectedDemand(demand.batch_id, (
                ("no_climate_data", f"no {demand.season} climate profile for lane {demand.lane}"),)))
            continue

        options: list[tuple[float, datetime, str, Qualification, tuple[str, ...]]] = []
        for q in quals:
            blocker = _qualification_blocker(q, protocol, climate,
                                             demand.transit_hours, demand.payload_kg, at)
            if blocker is not None:
                causes.append(blocker)
                continue
            free, blocked = samples.pool_status(demand.cargo_class, q.package, at)
            if len(free) < demand.sample_count:
                if free or blocked:
                    why = "; ".join(blocked)
                    detail = (f"{q.qualification_id}: need {demand.sample_count} free "
                              f"{demand.cargo_class}/{q.package} sample(s), {len(free)} available"
                              + (f" ({why})" if why else ""))
                else:
                    detail = (f"{q.qualification_id}: need {demand.sample_count} "
                              f"{demand.cargo_class}/{q.package} sample(s), none exist")
                causes.append(("no_reusable_sample", detail))
                continue
            sample_ids = tuple(free[:demand.sample_count])
            placed = None
            for chamber in sorted(chamber_rows, key=lambda c: c.chamber_id):
                slot = _placement(chamber, reservations[chamber.chamber_id], demand)
                if slot is None:
                    if demand.cargo_class not in chamber.cargo_classes:
                        causes.append(("chamber_class_unsupported",
                                       f"{q.qualification_id}: chamber {chamber.chamber_id} does not run {demand.cargo_class}"))
                    else:
                        causes.append(("chamber_capacity_full",
                                       f"{q.qualification_id}: chamber {chamber.chamber_id} cannot fit {demand.payload_kg}kg "
                                       f"for {demand.duration} inside the demand window"))
                    continue
                if placed is None or slot < (placed[1], placed[2]):
                    placed = (chamber.chamber_id, slot[0], slot[1])
            if placed is None:
                continue
            margin = min(climate.forecast_low - q.min_ambient,
                         q.max_ambient - climate.forecast_high,
                         q.hours - demand.transit_hours)
            options.append((margin, placed[1], placed[0], q, sample_ids))

        if not options:
            deduped = tuple(dict.fromkeys(causes))
            rejected.append(RejectedDemand(demand.batch_id, deduped or (
                ("no_candidate", "no qualification could satisfy the demand"),)))
            continue

        # Best thermal margin first; earliest start, then deterministic ids.
        margin, start, chamber_id, q, sample_ids = min(
            options, key=lambda o: (-o[0], o[1], o[2], o[3].qualification_id))
        end = start + demand.duration
        prev_batch = reservations[chamber_id][-1].batch_id if reservations[chamber_id] else None
        reservations[chamber_id].append(_Reservation(start, end, demand.payload_kg, demand.batch_id, prev_batch))

        seq += 1
        snapshot_id = f"{snapshot_id_prefix}-{seq:04d}"
        facts = PlanningFacts(demand.lane, climate.lane_class, demand.season,
                              climate.forecast_low, climate.forecast_high,
                              demand.transit_hours, demand.payload_kg)
        snapshot = seal(snapshot_id, demand.batch_id, run_id, at, q.pinned(), facts)
        registry.register(snapshot)
        for sid in sample_ids:
            samples.checkout(sid, demand.batch_id, start)

        planned.append(PlannedBatch(demand.batch_id, q.qualification_id, protocol.protocol_id,
                                    protocol.version, chamber_id, sample_ids, start, end,
                                    snapshot_id, margin))

    return ValidationPlan(run_id, at, tuple(planned), tuple(rejected))
