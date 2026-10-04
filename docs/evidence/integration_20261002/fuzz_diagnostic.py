import sys,unittest,faulthandler,sqlite3,time,json,hashlib
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
oldconnect=sqlite3.connect
class Observed(sqlite3.Connection):
 def commit(self):
  start=time.monotonic()
  try:return super().commit()
  finally:
   elapsed=time.monotonic()-start
   if elapsed>.1:print('SLOW_COMMIT',elapsed,flush=True)
 def execute(self,*args,**kwargs):
  start=time.monotonic()
  try:return super().execute(*args,**kwargs)
  finally:
   elapsed=time.monotonic()-start
   if elapsed>.1:print('SLOW_SQL',args[0][:90],elapsed,flush=True)
sqlite3.connect=lambda *a,**kw:oldconnect(*a,**{**kw,'factory':Observed})
suite=unittest.defaultTestLoader.loadTestsFromName('tests.test_action_dispatcher.DispatcherTests.test_1000_real_dispatcher_state_sequences_with_resources_and_rearm')
files={str(Path(m.__file__).relative_to(root)):hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in list(sys.modules.values()) if getattr(m,'__file__',None) and Path(m.__file__).is_relative_to(root) and Path(m.__file__).is_file()}
print('IMPORTED_SOURCE_HASHES',json.dumps(files,sort_keys=True),flush=True)
faulthandler.dump_traceback_later(8,repeat=True)
r=unittest.TextTestRunner(verbosity=2).run(suite);faulthandler.cancel_dump_traceback_later();sys.exit(not r.wasSuccessful())
