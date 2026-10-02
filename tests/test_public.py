import unittest
from datetime import datetime,timezone,timedelta,date,time
from decimal import Decimal
from coldledger.api import ColdLedgerService
from coldledger.inventory.fefo import Stock
from coldledger.quality.release import Policy,Case
from coldledger.transport.connections import Leg
from coldledger.audit.trail import AuditTrail
from coldledger.storage.event_store import EventStore
from coldledger.transport.route_selection import Route,select
from coldledger.compliance.license_registry import License,valid_license
from coldledger.packaging.validation_plan import Protocol,ValidationRequest,Chamber,Sample,plan,carryover,sample_usage,sample_conflicts,explain
UTC=timezone.utc
class PublicTests(unittest.TestCase):
    def test_health(self): self.assertEqual(ColdLedgerService().health()['runtime_dependencies'],0)
    def test_import_every_module(self):
        import coldledger, pkgutil, importlib
        names=[m.name for m in pkgutil.walk_packages(coldledger.__path__, coldledger.__name__+'.')]
        for name in names: importlib.import_module(name)
        self.assertGreater(len(names),100)
    def test_fefo(self):
        rows=[Stock('A',date(2027,1,1),Decimal('5')),Stock('B',date(2027,2,1),Decimal('10'))]
        picks=ColdLedgerService().reserve_inventory(rows,8,date(2026,10,1),30)
        self.assertEqual([p.lot for p in picks],['A','B'])
    def test_release(self):
        p=[Policy(1,2.0,frozenset({'pod'}),datetime(2026,1,1,tzinfo=UTC))]
        self.assertTrue(ColdLedgerService().release_decision(Case(datetime(2026,2,1,tzinfo=UTC),1,frozenset({'pod'})),p))
    def test_connection(self):
        i=Leg('i',datetime(2026,1,1,8,tzinfo=UTC),datetime(2026,1,1,7,tzinfo=UTC),'FRA')
        o=Leg('o',datetime(2026,1,1,12,tzinfo=UTC),datetime(2026,1,1,9,tzinfo=UTC),'FRA')
        self.assertEqual(ColdLedgerService().next_connection(i,[o],30).leg_id,'o')
    def test_audit(self):
        a=AuditTrail();a.append('u','create','S');a.append('q','release','S');self.assertTrue(a.verify())
    def test_store_conflict(self):
        s=EventStore();s.append('x',0,'created',{})
        with self.assertRaises(RuntimeError):s.append('x',0,'again',{})
    def test_route(self):
        r=select([Route('r',8,100,10,.1,1,50)],20,.2);self.assertEqual(r.route_id,'r')
    def test_license(self):
        l=License('H','DE','BIO',date(2026,1,1),date(2026,12,31));self.assertIsNotNone(valid_license('H','DE','BIO',date(2026,5,1),[l]))
    def test_validation_plan(self):
        proto=Protocol('ISTA-7D','2025A',datetime(2025,1,1,tzinfo=UTC),datetime(2027,1,1,tzinfo=UTC),frozenset({'bio'}),frozenset({'hot'}),timedelta(hours=48),2)
        ch=Chamber('C1',100,datetime(2026,10,1,tzinfo=UTC),datetime(2026,11,1,tzinfo=UTC))
        samples=[Sample('S1','bio',datetime(2027,1,1,tzinfo=UTC),2),Sample('S2','bio',datetime(2027,1,1,tzinfo=UTC),2)]
        reqs=[ValidationRequest('R1','ISTA-7D','bio','hot',50,datetime(2026,10,2,tzinfo=UTC),datetime(2026,10,20,tzinfo=UTC),1),
              ValidationRequest('R2','ISTA-7D','bio','hot',50,datetime(2026,10,2,tzinfo=UTC),datetime(2026,10,20,tzinfo=UTC),0)]
        result=ColdLedgerService().plan_validation(reqs,[proto],[ch],samples,datetime(2026,10,1,tzinfo=UTC))
        self.assertEqual(result.scheduled,('R1','R2'))
        self.assertEqual(result.deferrals,())
        first=result.assignments[0]
        self.assertEqual((first.start,first.end),(datetime(2026,10,2,tzinfo=UTC),datetime(2026,10,4,tzinfo=UTC)))
        self.assertEqual(first.sample_keys,('S1','S2'))
        snap=first.snapshot
        self.assertEqual((snap.protocol_key,snap.protocol_version),('ISTA-7D','2025A'))
        self.assertEqual(snap.planned_at,datetime(2026,10,1,tzinfo=UTC))
        self.assertEqual(dict(snap.facts)['cargo_category'],'bio')
        usage=sample_usage(result)
        self.assertEqual(sorted(usage),['S1','S2'])
        self.assertEqual(len(usage['S1']),2)
        self.assertEqual(sample_conflicts(result,samples),[])
    def test_validation_deferrals(self):
        proto=Protocol('P','v2',datetime(2026,1,1,tzinfo=UTC),None,frozenset({'bio','vac'}),frozenset({'hot'}),timedelta(hours=24),1)
        old=Protocol('Q','q1',datetime(2024,1,1,tzinfo=UTC),datetime(2025,1,1,tzinfo=UTC),frozenset({'bio'}),frozenset({'hot'}),timedelta(hours=24),1)
        ch=Chamber('C1',10,datetime(2026,10,1,tzinfo=UTC),datetime(2026,10,10,tzinfo=UTC))
        s=Sample('S1','bio',datetime(2027,1,1,tzinfo=UTC),1)
        t0=datetime(2026,10,1,tzinfo=UTC)
        reqs=[ValidationRequest('OK','P','bio','hot',5,t0,datetime(2026,10,10,tzinfo=UTC),1),
              ValidationRequest('FULL','P','bio','hot',5,t0,datetime(2026,10,2,tzinfo=UTC)),
              ValidationRequest('BIG','P','bio','hot',50,t0,datetime(2026,10,10,tzinfo=UTC)),
              ValidationRequest('EXP','Q','bio','hot',5,t0,datetime(2026,10,10,tzinfo=UTC)),
              ValidationRequest('DRY','P','vac','hot',5,t0,datetime(2026,10,10,tzinfo=UTC))]
        result=plan(reqs,[proto,old],[ch],[s],t0)
        self.assertEqual(result.scheduled,('OK',))
        reasons=result.deferral_reasons()
        self.assertEqual(reasons['FULL'],'chamber-unavailable')
        self.assertEqual(reasons['BIG'],'chamber-capacity')
        self.assertEqual(reasons['EXP'],'protocol-expired')
        self.assertEqual(reasons['DRY'],'insufficient-samples')
        full=next(d for d in result.deferrals if d.request_key=='FULL')
        self.assertIn('OK',full.explanation)
        exp=next(d for d in result.deferrals if d.request_key=='EXP')
        self.assertIn('q1',exp.explanation)
        self.assertEqual(len(explain(result)),5)
    def test_validation_carryover(self):
        v1=Protocol('P','v1',datetime(2025,1,1,tzinfo=UTC),datetime(2027,1,1,tzinfo=UTC),frozenset({'bio'}),frozenset({'hot'}),timedelta(hours=24),1)
        ch=Chamber('C1',10,datetime(2026,1,1,tzinfo=UTC),datetime(2026,2,1,tzinfo=UTC))
        s=Sample('S1','bio',datetime(2027,6,1,tzinfo=UTC),1)
        req=ValidationRequest('R1','P','bio','hot',5,datetime(2026,1,2,tzinfo=UTC),datetime(2026,1,10,tzinfo=UTC))
        p=plan([req],[v1],[ch],[s],datetime(2026,1,1,tzinfo=UTC))
        self.assertEqual(p.scheduled,('R1',))
        v2=Protocol('P','v2',datetime(2026,7,1,tzinfo=UTC),None,frozenset({'bio'}),frozenset({'hot'}),timedelta(hours=24),1)
        valid,stale=carryover(p,[v1,v2],[s],datetime(2026,10,1,tzinfo=UTC))
        self.assertEqual(valid,())
        self.assertEqual([x.status for x in stale],['protocol-superseded'])
        valid,stale=carryover(p,[v1],[s],datetime(2026,10,1,tzinfo=UTC))
        self.assertEqual([a.request_key for a in valid],['R1'])
        self.assertEqual(stale,())
        valid,stale=carryover(p,[v1],[s],datetime(2028,1,1,tzinfo=UTC))
        self.assertEqual([x.status for x in stale],['protocol-expired'])
        valid,stale=carryover(p,[v1],[Sample('S1','bio',datetime(2026,6,1,tzinfo=UTC),1)],datetime(2026,10,1,tzinfo=UTC))
        self.assertEqual([x.status for x in stale],['sample-expired'])
if __name__=='__main__':unittest.main()
