"""Resume only Inbox cases after invalid test event ID; preserve original campaign error."""
import asyncio,json,uuid,subprocess
import aiohttp
from run import ROOT,sql,command,req
from faults import ready
rows=[]
async def run():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as c:
        for mode in ('before-handler','after-handler'):
            pending=sql("SELECT event_id FROM inbox_event WHERE consumer_name='lab-audit' AND status='PROCESSING';").strip().splitlines()
            assert len(pending)<=1
            event=pending[0] if pending else str(uuid.uuid4())
            if not pending:
                try:await req(c,f'/api/message-lab/inbox/{event}?mode={mode}')
                except aiohttp.ClientError:pass
            for _ in range(40):
                code=subprocess.run(['docker','inspect','reliability-lab-20260910-app1-1','--format','{{.State.ExitCode}}'],capture_output=True,text=True,check=True).stdout.strip()
                if code=='86':break
                await asyncio.sleep(.1)
            processing=sql(f"SELECT status FROM inbox_event WHERE consumer_name='lab-audit' AND event_id='{event}';").strip()
            assert processing=='PROCESSING' and code=='86',(processing,code)
            await asyncio.sleep(2)
            await req(c,f'/api/message-lab/inbox/{event}',38483)
            for _ in range(10):await req(c,f'/api/message-lab/inbox/{event}',38483)
            receipt=sql(f"SELECT status,attempts FROM inbox_event WHERE consumer_name='lab-audit' AND event_id='{event}';").strip()
            rows.append({'name':'inbox_crash_'+mode,'status':'PASS' if receipt=='PROCESSED\t2' else 'FINDING','before':processing,'after':receipt,'exitCode':code,'duplicateRedeliveries':10})
            (ROOT/'inbox-results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
            print(json.dumps(rows[-1]),flush=True)
            command('start','app1');await ready(c,38482)
        violations=int(sql('SELECT COUNT(*) FROM inventory_stock WHERE available_quantity<0 OR reserved_quantity<0 OR sold_quantity<0 OR available_quantity+reserved_quantity+sold_quantity<>total_quantity;'))
        rows.append({'name':'final_stock_invariants','status':'PASS' if violations==0 else 'FINDING','violations':violations})
try:asyncio.run(run())
finally:(ROOT/'inbox-results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
