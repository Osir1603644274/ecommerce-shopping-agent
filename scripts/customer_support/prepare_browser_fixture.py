"""Private browser fixture; never copy credentials into test artifacts."""
import argparse
from pathlib import Path
from live_evaluation import LiveDriver,save

class BrowserFixture(LiveDriver):
    def api(self,method,path,body=None,key=None):
        result=super().api(method,path,body,key)
        if path=='/api/auth/register' and body['username'].startswith('eval-owner-'):self.browser_account=body
        if path=='/api/auth/login' and body['username'].startswith('eval-admin-'):self.simulator_token=result['accessToken']
        return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--kind',choices=['received','refund_review'],default='received');args=p.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    d=BrowserFixture(args.runtime,'http://127.0.0.1:18000')
    try:
        d.prepare({'fixture':{'kind':args.kind,'unitPriceMinor':127,'purchasedQuantity':3,'requestedQuantity':1}})
        save(args.output,{'account':d.browser_account,'adminToken':d.simulator_token,'authority':d.config['authority'],'orderId':d.order,'itemId':d.item,'caseId':d.case['id'] if d.case else None})
        print('Private isolated browser fixture ready')
    finally:d.close()
