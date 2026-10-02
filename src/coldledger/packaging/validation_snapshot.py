from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock


@dataclass(frozen=True)
class ClimateEnvelope:
    min_ambient: float
    max_ambient: float
    hours: float


@dataclass(frozen=True)
class PinnedQualification:
    """The exact package qualification that justified a validation batch."""
    qualification_id: str
    package: str
    protocol_id: str
    protocol_version: str
    cargo_class: str
    lane_class: str
    envelope: ClimateEnvelope
    payload_min: float
    payload_max: float
    effective_from: datetime
    expires_at: datetime
    report_ref: str


@dataclass(frozen=True)
class PlanningFacts:
    """Demand facts observed at planning time; frozen into the snapshot."""
    lane: str
    lane_class: str
    season: str
    forecast_low: float
    forecast_high: float
    transit_hours: float
    payload_kg: float


@dataclass(frozen=True)
class QualificationSnapshot:
    snapshot_id: str
    batch_id: str
    run_id: str
    created_at: datetime
    pinned: PinnedQualification
    facts: PlanningFacts
    checksum: str


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return v.astimezone(timezone.utc)


def checksum_of(pinned: PinnedQualification, facts: PlanningFacts,
                snapshot_id: str, batch_id: str, run_id: str, created_at: datetime) -> str:
    payload = {
        "snapshot_id": snapshot_id,
        "batch_id": batch_id,
        "run_id": run_id,
        "created_at": _utc(created_at).isoformat(),
        "pinned": {
            "qualification_id": pinned.qualification_id,
            "package": pinned.package,
            "protocol_id": pinned.protocol_id,
            "protocol_version": pinned.protocol_version,
            "cargo_class": pinned.cargo_class,
            "lane_class": pinned.lane_class,
            "min_ambient": pinned.envelope.min_ambient,
            "max_ambient": pinned.envelope.max_ambient,
            "hours": pinned.envelope.hours,
            "payload_min": pinned.payload_min,
            "payload_max": pinned.payload_max,
            "effective_from": _utc(pinned.effective_from).isoformat(),
            "expires_at": _utc(pinned.expires_at).isoformat(),
            "report_ref": pinned.report_ref,
        },
        "facts": {
            "lane": facts.lane,
            "lane_class": facts.lane_class,
            "season": facts.season,
            "forecast_low": facts.forecast_low,
            "forecast_high": facts.forecast_high,
            "transit_hours": facts.transit_hours,
            "payload_kg": facts.payload_kg,
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def seal(snapshot_id: str, batch_id: str, run_id: str, created_at: datetime,
         pinned: PinnedQualification, facts: PlanningFacts) -> QualificationSnapshot:
    digest = checksum_of(pinned, facts, snapshot_id, batch_id, run_id, created_at)
    return QualificationSnapshot(snapshot_id, batch_id, run_id, _utc(created_at), pinned, facts, digest)


def verify(snapshot: QualificationSnapshot) -> bool:
    expected = checksum_of(snapshot.pinned, snapshot.facts, snapshot.snapshot_id,
                           snapshot.batch_id, snapshot.run_id, snapshot.created_at)
    return expected == snapshot.checksum


@dataclass(frozen=True)
class ReuseDecision:
    snapshot_id: str
    allowed: bool
    reasons: tuple[tuple[str, str], ...]


class SnapshotRegistry:
    """Append-only store of qualification snapshots.

    Snapshots are evidence of *what was known* when a batch was planned.
    Reusing one for a later batch always goes through ``evaluate_reuse`` and
    must survive expiry/protocol/envelope gates — nothing is reused silently.
    """

    def __init__(self):
        self._rows: dict[str, QualificationSnapshot] = {}
        self._order: list[str] = []
        self._lock = RLock()

    def register(self, snapshot: QualificationSnapshot) -> QualificationSnapshot:
        if not verify(snapshot):
            raise ValueError("snapshot checksum mismatch")
        with self._lock:
            if snapshot.snapshot_id in self._rows:
                raise ValueError(f"duplicate snapshot {snapshot.snapshot_id}")
            self._rows[snapshot.snapshot_id] = snapshot
            self._order.append(snapshot.snapshot_id)
            return snapshot

    def get(self, snapshot_id: str) -> QualificationSnapshot:
        with self._lock:
            try:
                return self._rows[snapshot_id]
            except KeyError:
                raise KeyError(f"unknown snapshot {snapshot_id}")

    def entries(self) -> tuple[QualificationSnapshot, ...]:
        with self._lock:
            return tuple(self._rows[k] for k in self._order)

    def for_batch(self, batch_id: str) -> tuple[QualificationSnapshot, ...]:
        with self._lock:
            return tuple(s for s in (self._rows[k] for k in self._order) if s.batch_id == batch_id)

    def evaluate_reuse(self, snapshot_id: str, at: datetime, *, protocol_id: str,
                       protocol_version: str, lane_class: str, forecast_low: float,
                       forecast_high: float, transit_hours: float,
                       payload_kg: float) -> ReuseDecision:
        snapshot = self.get(snapshot_id)
        p, f = snapshot.pinned, snapshot.facts
        now = _utc(at)
        reasons: list[tuple[str, str]] = []
        if now < _utc(p.effective_from):
            reasons.append(("qualification_not_yet_effective",
                            f"{p.qualification_id} effective from {_utc(p.effective_from).date()}"))
        if now >= _utc(p.expires_at):
            reasons.append(("qualification_expired",
                            f"{p.qualification_id} expired at {_utc(p.expires_at).isoformat()}"))
        if p.protocol_id != protocol_id:
            reasons.append(("protocol_mismatch",
                            f"snapshot under {p.protocol_id}, effective protocol is {protocol_id}"))
        elif p.protocol_version != protocol_version:
            reasons.append(("protocol_version_stale",
                            f"snapshot under {protocol_id} {p.protocol_version}, effective version is {protocol_version}"))
        if p.lane_class not in {"*", lane_class}:
            reasons.append(("lane_class_mismatch",
                            f"snapshot qualified for {p.lane_class}, demand is {lane_class}"))
        if forecast_low < p.envelope.min_ambient or forecast_high > p.envelope.max_ambient:
            reasons.append(("climate_envelope_insufficient",
                            f"forecast [{forecast_low},{forecast_high}] outside qualified "
                            f"[{p.envelope.min_ambient},{p.envelope.max_ambient}]"))
        if transit_hours > p.envelope.hours:
            reasons.append(("transit_hours_exceed",
                            f"transit {transit_hours}h exceeds qualified {p.envelope.hours}h"))
        if not p.payload_min <= payload_kg <= p.payload_max:
            reasons.append(("payload_out_of_range",
                            f"payload {payload_kg}kg outside [{p.payload_min},{p.payload_max}]"))
        return ReuseDecision(snapshot_id, not reasons, tuple(reasons))
