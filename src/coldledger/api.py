from datetime import timedelta
from .inventory.fefo import reserve
from .quality.release import may_release
from .transport.connections import choose
from .packaging.validation_planner import plan_batches
class ColdLedgerService:
    def reserve_inventory(self,*args,**kwargs): return reserve(*args,**kwargs)
    def release_decision(self,*args,**kwargs): return may_release(*args,**kwargs)
    def next_connection(self,inbound,candidates,minutes): return choose(inbound,candidates,timedelta(minutes=minutes))
    def plan_validation_batches(self,*args,**kwargs): return plan_batches(*args,**kwargs)
    def health(self): return {'service':'coldledger','status':'ok','runtime_dependencies':0}
