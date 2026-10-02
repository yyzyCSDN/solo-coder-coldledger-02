from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock


@dataclass(frozen=True)
class Sample:
    """Reusable validation specimen. Each use is tied to a batch and tracked."""
    sample_id: str
    cargo_class: str
    package: str
    max_uses: int
    cooldown: timedelta = timedelta()
    quarantined: bool = False

    def validate(self) -> None:
        if not self.sample_id or not self.cargo_class or not self.package:
            raise ValueError("sample_id, cargo_class and package required")
        if self.max_uses <= 0:
            raise ValueError("max_uses must be positive")
        if self.cooldown < timedelta():
            raise ValueError("cooldown must be non-negative")


@dataclass(frozen=True)
class SampleUse:
    sample_id: str
    batch_id: str
    started_at: datetime
    released_at: datetime | None


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return v.astimezone(timezone.utc)


class SamplePool:
    """Ledger of reusable samples with use-count and cooldown enforcement."""

    def __init__(self, samples: list[Sample] | None = None):
        self._samples: dict[str, Sample] = {}
        self._uses: dict[str, list[SampleUse]] = {}
        self._lock = RLock()
        for s in samples or []:
            self.add(s)

    def add(self, sample: Sample) -> None:
        sample.validate()
        with self._lock:
            if sample.sample_id in self._samples:
                raise ValueError(f"duplicate sample {sample.sample_id}")
            self._samples[sample.sample_id] = sample
            self._uses[sample.sample_id] = []

    def quarantine(self, sample_id: str) -> None:
        with self._lock:
            if sample_id not in self._samples:
                raise KeyError(sample_id)
            s = self._samples[sample_id]
            self._samples[sample_id] = Sample(s.sample_id, s.cargo_class, s.package,
                                              s.max_uses, s.cooldown, True)

    def uses(self, sample_id: str) -> tuple[SampleUse, ...]:
        with self._lock:
            return tuple(self._uses[sample_id])

    def availability(self, sample_id: str, at: datetime) -> tuple[bool, str]:
        """Return whether the sample may start another run at ``at`` and why not."""
        now = _utc(at)
        with self._lock:
            s = self._samples[sample_id]
            if s.quarantined:
                return False, "quarantined"
            rows = self._uses[sample_id]
            active = [u for u in rows if u.released_at is None]
            if active:
                return False, f"in use by batch {active[0].batch_id}"
            if len(rows) >= s.max_uses:
                return False, f"reached max_uses={s.max_uses}"
            if rows:
                last_release = max((u.released_at for u in rows if u.released_at is not None),
                                   default=max(u.started_at for u in rows))
                ready_at = _utc(last_release) + s.cooldown
                if now < ready_at:
                    return False, f"cooldown until {ready_at.isoformat()}"
            return True, "available"

    def available_samples(self, cargo_class: str, package: str, at: datetime) -> list[str]:
        with self._lock:
            ids = [sid for sid, s in self._samples.items()
                   if s.cargo_class == cargo_class and s.package == package]
        return [sid for sid in sorted(ids) if self.availability(sid, at)[0]]

    def pool_status(self, cargo_class: str, package: str, at: datetime) -> tuple[list[str], list[str]]:
        """Return (available ids, human reasons for every unavailable id)."""
        with self._lock:
            ids = sorted(sid for sid, s in self._samples.items()
                         if s.cargo_class == cargo_class and s.package == package)
        free, blocked = [], []
        for sid in ids:
            ok, why = self.availability(sid, at)
            if ok:
                free.append(sid)
            else:
                blocked.append(f"{sid}: {why}")
        return free, blocked

    def checkout(self, sample_id: str, batch_id: str, started_at: datetime) -> SampleUse:
        ok, why = self.availability(sample_id, _utc(started_at))
        if not ok:
            raise ValueError(f"sample {sample_id} unavailable: {why}")
        with self._lock:
            if any(u.batch_id == batch_id for u in self._uses[sample_id]):
                raise ValueError(f"sample {sample_id} already booked by batch {batch_id}")
            use = SampleUse(sample_id, batch_id, _utc(started_at), None)
            self._uses[sample_id].append(use)
            return use

    def release(self, sample_id: str, batch_id: str, released_at: datetime) -> SampleUse:
        with self._lock:
            rows = self._uses[sample_id]
            for i, u in enumerate(rows):
                if u.batch_id == batch_id and u.released_at is None:
                    done = SampleUse(u.sample_id, u.batch_id, u.started_at, _utc(released_at))
                    rows[i] = done
                    return done
            raise KeyError(f"no active use of {sample_id} by {batch_id}")

    def audit(self) -> None:
        with self._lock:
            for sid, rows in self._uses.items():
                if len(rows) > self._samples[sid].max_uses:
                    raise ValueError(f"sample {sid} exceeded max_uses")
                active = [u for u in rows if u.released_at is None]
                if len(active) > 1:
                    raise ValueError(f"sample {sid} checked out by multiple batches")
