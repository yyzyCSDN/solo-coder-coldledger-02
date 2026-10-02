import unittest
from datetime import datetime, timedelta, timezone

from coldledger.api import ColdLedgerService
from coldledger.packaging.sample_pool import Sample, SamplePool
from coldledger.packaging.validation_planner import (
    Chamber,
    LaneClimate,
    Protocol,
    Qualification,
    ValidationDemand,
    plan_batches,
)
from coldledger.packaging.validation_snapshot import SnapshotRegistry, verify

UTC = timezone.utc
AT = datetime(2026, 10, 2, tzinfo=UTC)
DAY = timedelta(days=1)


def protocol(v="2.1", cargo="BIO", expired=False):
    return Protocol("TEMP-MAPPING", v, cargo,
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 6, 1, tzinfo=UTC) if expired else None)


def qualification(qid="Q1", version="2.1", cargo="BIO", lane_class="AIR",
                  low=-30.0, high=25.0, hours=96.0,
                  pmin=0.0, pmax=50.0, expires=datetime(2027, 1, 1, tzinfo=UTC),
                  package="BOX-A"):
    return Qualification(qid, package, "TEMP-MAPPING", version, cargo, lane_class,
                         low, high, hours, pmin, pmax,
                         datetime(2026, 1, 1, tzinfo=UTC), expires, f"RPT-{qid}")


def climates():
    return [
        LaneClimate("DE-CN", "winter", "AIR", -25.0, 10.0),
        LaneClimate("DE-CN", "summer", "AIR", 5.0, 38.0),
        LaneClimate("DE-CN", "autumn", "AIR", -10.0, 22.0),
    ]


def chambers(capacity=100.0, classes=frozenset({"BIO"})):
    return [Chamber("CH-1", capacity, classes, timedelta(minutes=30))]


def demand(batch="B-1", season="winter", payload=10.0, hours=72.0, **kw):
    kw.setdefault("earliest", AT)
    kw.setdefault("latest", AT + 7 * DAY)
    return ValidationDemand(batch, "BIO", "DE-CN", season, hours, payload,
                            timedelta(hours=8), **kw)


def pool(*samples):
    return SamplePool(list(samples))


def sample(sid="S1", cargo="BIO", package="BOX-A", max_uses=3):
    return Sample(sid, cargo, package, max_uses, timedelta(days=2))


def run(demands=None, **kw):
    registry = SnapshotRegistry()
    samples = kw.pop("samples", pool(sample()))
    plan = plan_batches(run_id=kw.pop("run_id", "RUN-1"),
                        planned_at=kw.pop("planned_at", AT),
                        demands=demands if demands is not None else [demand()],
                        protocols=kw.pop("protocols", [protocol()]),
                        qualifications=kw.pop("qualifications", [qualification()]),
                        climates=kw.pop("climates", climates()),
                        chambers=kw.pop("chambers", chambers()),
                        registry=registry, samples=samples)
    return plan, registry, samples


class ValidationPlannerTests(unittest.TestCase):
    def test_happy_path_seals_snapshot_and_checks_out_sample(self):
        plan, registry, samples = run()
        self.assertTrue(plan.complete, plan.explain())
        self.assertEqual(len(plan.batches), 1)
        b = plan.batches[0]
        self.assertEqual(b.protocol_version, "2.1")
        self.assertEqual(b.sample_ids, ("S1",))
        snap = registry.get(b.snapshot_id)
        self.assertTrue(verify(snap))
        self.assertEqual(snap.batch_id, "B-1")
        self.assertEqual(snap.facts.season, "winter")
        # the snapshot genuinely pins the qualification as it was at planning time
        self.assertEqual(snap.pinned.protocol_version, "2.1")
        use = samples.uses("S1")[0]
        self.assertEqual(use.batch_id, "B-1")
        self.assertIsNone(use.released_at)

    def test_winter_cold_snap_rejects_summer_envelope_with_explained_tradeoff(self):
        # qualification only covers down to -20C; winter forecast hits -25C
        q = qualification(low=-20.0)
        plan, _, _ = run(qualifications=[q])
        self.assertFalse(plan.complete)
        self.assertEqual(len(plan.rejected), 1)
        codes = {c for c, _ in plan.rejected[0].causes}
        self.assertIn("climate_envelope_insufficient", codes)
        text = "\n".join(plan.explain())
        self.assertIn("B-1 unscheduled", text)
        self.assertIn("-25.0", text)

    def test_expired_qualification_is_never_silently_reused(self):
        q = qualification(expires=datetime(2026, 9, 1, tzinfo=UTC))
        plan, registry, _ = run(qualifications=[q])
        self.assertEqual(plan.batches, ())
        self.assertEqual(plan.rejected[0].causes[0][0], "qualification_expired")
        # nothing sealed, so nothing can be mistaken for a valid plan
        self.assertEqual(registry.entries(), ())

    def test_stale_protocol_version_blocks_and_mentions_effective_version(self):
        # protocol 2.1 is effective; the only qualification was done under 1.9
        plan, _, _ = run(qualifications=[qualification(version="1.9")])
        causes = dict(plan.rejected[0].causes)
        self.assertIn("protocol_version_stale", causes)
        self.assertIn("2.1", causes["protocol_version_stale"])

    def test_no_active_protocol_for_cargo_class(self):
        plan, _, _ = run(protocols=[protocol(expired=True)])
        self.assertEqual(plan.rejected[0].causes[0][0], "no_effective_protocol")

    def test_sample_reuse_is_counted_and_exhaustion_explained(self):
        small_pool = pool(sample("S1", max_uses=1))
        d1 = demand("B-1")
        d2 = demand("B-2")
        plan, _, samples = run(demands=[d1, d2], samples=small_pool,
                               chambers=chambers(1000.0))
        self.assertEqual([b.batch_id for b in plan.batches], ["B-1"])
        self.assertEqual(plan.rejected[0].batch_id, "B-2")
        self.assertEqual(plan.rejected[0].causes[0][0], "no_reusable_sample")
        samples.audit()
        # released + replanned after limit -> still blocked (max_uses is absolute)
        samples.release("S1", "B-1", AT + DAY)
        plan2, _, _ = run(demands=[d2], samples=small_pool, run_id="RUN-2",
                          chambers=chambers(1000.0))
        self.assertIn("max_uses=1", plan2.rejected[0].causes[0][1])

    def test_cooldown_blocks_immediate_reuse_then_allows(self):
        p = pool(sample("S1", max_uses=5))
        plan1, _, p = run(demands=[demand("B-1")], samples=p)
        self.assertTrue(plan1.complete)
        p.release("S1", "B-1", AT + timedelta(hours=1))
        # next planning run one day later is still inside the 2-day cooldown
        soon = AT + DAY
        plan2, _, _ = run(demands=[demand("B-2")], samples=p, planned_at=soon, run_id="RUN-2")
        self.assertEqual(plan2.rejected[0].causes[0][0], "no_reusable_sample")
        self.assertIn("cooldown", plan2.rejected[0].causes[0][1])
        later = AT + 3 * DAY
        plan3, _, _ = run(demands=[demand("B-3", earliest=later)], samples=p,
                          planned_at=later, run_id="RUN-3")
        self.assertTrue(plan3.complete, plan3.explain())

    def test_chamber_payload_capacity_schedules_parallel_only_within_kg_limit(self):
        big_pool = pool(sample("S1"), sample("S2"))
        ds = [demand("B-1", payload=60.0), demand("B-2", payload=60.0)]
        plan, _, _ = run(demands=ds, samples=big_pool, chambers=chambers(100.0),
                         qualifications=[qualification(pmax=100.0)])
        # second batch cannot overlap (60+60 > 100): planner places it after the first
        self.assertEqual(len(plan.batches), 2)
        a, b = sorted(plan.batches, key=lambda x: x.start)
        self.assertGreaterEqual(b.start, a.end)

    def test_priority_scheduling_honours_latest_window(self):
        big_pool = pool(sample("S1"), sample("S2"), sample("S3"))
        urgent = demand("URGENT", priority=10, latest=AT + 2 * DAY)
        normal = demand("NORMAL", priority=0)
        # tiny chamber forces serialization; the urgent one must land inside its window
        plan, _, _ = run(demands=[normal, urgent], samples=big_pool)
        self.assertEqual([b.batch_id for b in plan.batches], ["URGENT", "NORMAL"])

    def test_snapshot_reuse_gate_rejects_expired_and_stale(self):
        plan, registry, _ = run()
        sid = plan.batches[0].snapshot_id
        later = datetime(2027, 6, 1, tzinfo=UTC)
        decision = registry.evaluate_reuse(
            sid, later, protocol_id="TEMP-MAPPING", protocol_version="2.1",
            lane_class="AIR", forecast_low=-20.0, forecast_high=10.0,
            transit_hours=72.0, payload_kg=10.0)
        self.assertFalse(decision.allowed)
        codes = {c for c, _ in decision.reasons}
        self.assertIn("qualification_expired", codes)

        # same snapshot against a newer protocol version -> stale
        d2 = registry.evaluate_reuse(
            sid, AT, protocol_id="TEMP-MAPPING", protocol_version="3.0",
            lane_class="AIR", forecast_low=-20.0, forecast_high=10.0,
            transit_hours=72.0, payload_kg=10.0)
        self.assertIn("protocol_version_stale", {c for c, _ in d2.reasons})

        # identical conditions, still in validity -> explicit reuse is permitted
        ok = registry.evaluate_reuse(
            sid, AT, protocol_id="TEMP-MAPPING", protocol_version="2.1",
            lane_class="AIR", forecast_low=-20.0, forecast_high=10.0,
            transit_hours=72.0, payload_kg=10.0)
        self.assertTrue(ok.allowed)

    def test_service_facade_plans(self):
        svc = ColdLedgerService()
        registry = SnapshotRegistry()
        out = svc.plan_validation_batches(
            run_id="R", planned_at=AT, demands=[demand()],
            protocols=[protocol()], qualifications=[qualification()],
            climates=climates(), chambers=chambers(),
            registry=registry, samples=pool(sample()))
        self.assertTrue(out.complete)


if __name__ == "__main__":
    unittest.main()
