"""Container-only smoke. All credentials/models are disposable fixtures; network is disabled."""
import json, os, pathlib, queue, shutil, signal, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, '/source/runtime')
from fixture import KEY, ENV
receipt={'network':'none','architecture':os.uname().machine,'model':'loopback fixture only'}
requests=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  data=json.dumps({'object':'list','data':[{'id':'local-fixture-model','object':'model'}]}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if not self.path.endswith('/chat/completions'): self.send_error(404);return
  tools=body.get('tools',[]); names=[v.get('function',{}).get('name') for v in tools]
  roles=[v.get('role') for v in body.get('messages',[])]
  requests.append({'tools':names,'roles':roles})
  if 'tool' not in roles:
   message={'role':'assistant','content':None,'tool_calls':[{'id':'call-local-fixture','type':'function','function':{'name':'shell','arguments':json.dumps({'command':'pwd'})}}]}
   finish='tool_calls'
  else: message={'role':'assistant','content':'LOCAL_FORK_TOOL_PROOF'};finish='stop'
  answer={'id':'local-fixture','object':'chat.completion','created':int(time.time()),'model':'local-fixture-model','choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
  if body.get('stream'):
   payload={'id':'local-fixture','object':'chat.completion.chunk','model':'local-fixture-model','choices':[{'index':0,'delta':message,'finish_reason':None}]}
   data=('data: '+json.dumps(payload)+'\n\ndata: '+json.dumps({'id':'local-fixture','object':'chat.completion.chunk','model':'local-fixture-model','choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':answer['usage']})+'\n\ndata: [DONE]\n\n').encode();kind='text/event-stream'
  else: data=json.dumps(answer).encode();kind='application/json'
  self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('127.0.0.1',8099),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
fixture=pathlib.Path('/tmp/fork-smoke-agent');shutil.copytree('/source/agent',fixture/'agent')
(fixture/'.env').write_text(ENV)
run=pathlib.Path('/run/agent');run.mkdir(mode=0o700,exist_ok=True);run.chmod(0o700)
data=pathlib.Path('/tmp/fork-smoke-data')
# Prepare the unprivileged fixture without adding CHOWN to the hardened runtime.
subprocess.run(['python3','-c','from pathlib import Path;p=Path("/tmp/fork-smoke-data");p.mkdir();(p/"config.toml").write_text("[analytics]\\nenabled=false\\nshare_usage=false\\n")'],user=1000,group=1000,extra_groups=[],check=True)
token='spec021-local-fixture-core-token'
env={**os.environ,'AGENT_DIR':str(fixture),'OPENHUMAN_WORKSPACE':str(data),'OPENHUMAN_CORE_PORT':'7788','OPENHUMAN_CORE_TOKEN':token,'MODEL_BASE_URL':'http://127.0.0.1:8099/v1','MODEL_API_KEY':'fixture-model-token','MODEL':'local-fixture-model','AGENT_STOP_GRACE_SECONDS':'1'}
log=open('/tmp/fork-smoke-supervisor.log','w')
process=subprocess.Popen(['python3','/opt/agent-runtime/supervisor.py'],env=env,stdout=log,stderr=log)
id=0
base='http://127.0.0.1:7788'
def rpc(method,params={}):
 global id
 id+=1
 req=urllib.request.Request(base+'/rpc',json.dumps({'jsonrpc':'2.0','id':id,'method':'openhuman.'+method,'params':params}).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+token})
 result=json.load(urllib.request.urlopen(req,timeout=30))
 assert 'error' not in result,(method,result.get('error'))
 return result.get('result')
def wait_status(lock,reload=None):
 until=time.monotonic()+70
 while time.monotonic()<until:
  assert process.poll() is None,'supervisor exited'
  try:
   value=json.loads((run/'status.json').read_text())
   if value.get('state')=='running' and value.get('lock')==lock and value.get('reload_id')==reload:return value
   assert value.get('state')!='configuration_failed','configuration failed'
  except FileNotFoundError:pass
  time.sleep(.2)
 raise AssertionError('status timeout')
try:
 wait_status('locked');receipt['locked_boot']='passed'
 checked=subprocess.run(['python3','/opt/agent-runtime/supervisor.py','check-key'],env=env,input=KEY,text=True,capture_output=True)
 assert checked.returncode==0,'fixture check-key failed';receipt['fixture_check_key']=0
 (run/'unlock').write_text(KEY);(run/'unlock').chmod(0o600);(run/'reload').write_text('fork-smoke-unlock')
 process.send_signal(signal.SIGHUP);wait_status('unlocked','fork-smoke-unlock');receipt['unlock_reload']='passed'
 denial=subprocess.run(['python3','-c','from pathlib import Path;Path("/run/agent/unlock").read_text()'],user=1000,group=1000,extra_groups=[],capture_output=True)
 assert denial.returncode!=0;receipt['uid1000_key_read']='denied'
 # Subscribe before submitting; a successful HTTP header means subscription exists.
 response=urllib.request.urlopen(urllib.request.Request(base+'/events?client_id=fork-smoke',headers={'Authorization':'Bearer '+token}),timeout=70)
 events=queue.Queue()
 def collect():
  try:
   lines=[]
   for raw in response:
    line=raw.decode().strip()
    if line.startswith('data:'):lines.append(line[5:].lstrip())
    elif not line and lines:
     value=json.loads('\n'.join(lines));lines=[];events.put(value)
  except Exception as e:events.put({'reader_error':type(e).__name__})
 threading.Thread(target=collect,daemon=True).start()
 rpc('channel_web_chat',{'client_id':'fork-smoke','thread_id':'fork-smoke-thread','message':'Run pwd with your shell tool once, then say LOCAL_FORK_TOOL_PROOF.','model_override':'local-fixture-model'})
 terminal=None;until=time.monotonic()+65
 while time.monotonic()<until:
  try:event=events.get(timeout=.5)
  except queue.Empty:continue
  if event.get('event') in ('chat_done','chat_error'):terminal=event;break
 assert terminal and terminal.get('event')=='chat_done',('terminal',terminal)
 assert 'LOCAL_FORK_TOOL_PROOF' in terminal.get('full_response',''),'missing reply'
 assert requests and 'shell' in requests[0]['tools'],'native shell schema not sent'
 assert any('tool' in request['roles'] for request in requests),'no actual tool result in next model round'
 receipt['native_shell_tool_and_reply']='passed';receipt['model_requests']=len(requests);receipt['first_request_tool_count']=len(requests[0]['tools'])
 response.close()
 workspace=data/'workspace';store=workspace/'mcp_clients'
 assert store.is_symlink() and str(store.resolve()).startswith('/dev/shm/');receipt['tool_store_tmpfs']='passed'
 # Verify raw key is absent from UID1000 process environments and persistent fixture data.
 scan="""import pathlib,sys
count=0
for proc in pathlib.Path('/proc').iterdir():
 if not proc.name.isdecimal():continue
 try:
  if b'openhuman-core' not in (proc/'cmdline').read_bytes():continue
  env=(proc/'environ').read_bytes();sys.stdout.buffer.write(env+b'\\0');count+=1
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
sys.stderr.write(str(count))
"""
 observed=subprocess.run(['python3','-c',scan],user=1000,group=1000,extra_groups=[],capture_output=True,check=True)
 assert int(observed.stderr or b'0')>=1,'could not inspect any core environment'
 assert KEY.encode() not in observed.stdout,'key leaked to core environment'
 receipt['core_environments_inspected']=int(observed.stderr)
 for file in data.rglob('*'):
  if file.is_file() and not file.is_symlink():
   read=subprocess.run(['python3','-c','import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())',str(file)],user=1000,group=1000,extra_groups=[],capture_output=True,check=True)
   assert KEY.encode() not in read.stdout,'key leaked to persistent data'
 receipt['key_absent_child_env_and_data']='passed'
finally:
 process.terminate()
 try:process.wait(timeout=8)
 except subprocess.TimeoutExpired:process.kill();process.wait()
 server.shutdown();log.close()
receipt['stop_exit']=process.returncode
assert process.returncode==0,'stop not clean'
print(json.dumps(receipt,indent=2))
