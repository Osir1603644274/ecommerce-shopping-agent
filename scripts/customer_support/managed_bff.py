"""Own an isolated evaluation BFF process; never stop existing listeners."""
import os
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
import psutil

class ManagedBff:
    def __init__(self,port=18001,authority='http://127.0.0.1:18080',*,support_agent_enabled=True):
        if not 18001<=port<=18009:raise ValueError('managed evaluation ports are 18001..18009')
        parsed=urlsplit(authority)
        if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment):
            raise ValueError('managed BFF authority must be an explicit loopback origin')
        self.authority=authority.rstrip('/')
        self.port=port;self.origin=f'http://127.0.0.1:{port}';self.process=None
        self.root=Path(__file__).resolve().parents[2]
        self.model_fault_url=None
        self.source_hashes=None
        if type(support_agent_enabled) is not bool:raise ValueError('explicit boolean support switch required')
        self.support_agent_enabled=support_agent_enabled

    def fingerprint(self):
        files=list((self.root/'agent/app').rglob('*.py'))+[self.root/'agent/app/customer_support/policy_v1.json']
        return {str(p.relative_to(self.root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}

    def start(self):
        if self.process is not None and self.process.poll() is None:raise RuntimeError('owned BFF still running')
        before=self.fingerprint()
        if self.source_hashes is not None and self.source_hashes!=before:raise RuntimeError('BFF source changed across managed restart; use a new run')
        self.source_hashes=before
        with socket.socket() as probe:
            if os.name=='nt':probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            probe.bind(('127.0.0.1',self.port))
        env=os.environ.copy()
        env.update(PYTHONPATH=str(self.root/'agent'),BACKEND_BASE_URL=self.authority,
                   REDIS_URL='redis://127.0.0.1:16379/0',CUSTOMER_SUPPORT_AGENT_ENABLED=str(self.support_agent_enabled).lower(),
                   COMMERCE_DEMO_ENABLED='true',COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED='true',
                   COMMERCE_WORKSPACE_CART_ENABLED='true',COMMERCE_WORKSPACE_EPOCH='support-live-001',
                   CATALOG_WORKSPACE_ENABLED='false',EVIDENCE_CRITIC_ENABLED='false',WEB_QUERY_INTAKE_ENABLED='false',
                   KNOWLEDGE_REVIEW_HYBRID_ENABLED='false',MEMORY_PROJECTION_CLIENT_ENABLED='false',
                   PRODUCT_RETRIEVAL_MODE='bm25',PYTHONUTF8='1')
        if self.model_fault_url:
            parsed=urlsplit(self.model_fault_url)
            if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('model fault endpoint must be loopback HTTP')
            # Fault server never receives the provider credential.
            env.update(DEEPSEEK_BASE_URL=self.model_fault_url,DEEPSEEK_API_KEY='synthetic-timeout-fixture')
        self.process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(self.port),'--no-access-log'],
            cwd=self.root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+55
        with httpx.Client(timeout=2,trust_env=False,headers={'Origin':self.origin}) as client:
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RuntimeError(f'owned BFF exited: {self.process.returncode}')
                try:
                    owned={self.process.pid,*[p.pid for p in psutil.Process(self.process.pid).children(recursive=True)]}
                    listeners={c.pid for c in psutil.net_connections(kind='tcp') if c.status=='LISTEN' and c.laddr.port==self.port}
                    if listeners and listeners<=owned and client.get(self.origin+'/api/commerce-demo/workspace').status_code==200:
                        if self.fingerprint()!=before:
                            self.stop();raise RuntimeError('BFF source changed during startup')
                        return self.process.pid
                except httpx.HTTPError:pass
                time.sleep(.25)
        self.stop();raise TimeoutError('owned BFF readiness timeout')

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            # Windows venv python.exe launches the interpreter as a child.
            children=psutil.Process(self.process.pid).children(recursive=True)
            for child in reversed(children):
                try:child.kill()
                except psutil.NoSuchProcess:pass
            self.process.kill();self.process.wait(timeout=15)
            _,alive=psutil.wait_procs(children,timeout=15)
            if alive:raise RuntimeError('owned interpreter child failed to exit')

    def restart(self):
        if self.process is None or self.process.poll() is not None:raise RuntimeError('restart requires a live owned process')
        old=self.process.pid;self.stop();code=self.process.returncode;new=self.start()
        return {'oldPid':old,'exitCode':code,'newPid':new,'readiness':'HTTP workspace 200','sourceSetSha256':hashlib.sha256(json.dumps(self.source_hashes,sort_keys=True).encode()).hexdigest(),'fault':'terminate owned BFF after completed response'}

    def __enter__(self):
        try:self.start();return self
        except BaseException:
            self.stop();raise
    def __exit__(self,*args):self.stop()
