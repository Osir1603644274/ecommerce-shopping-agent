import json
from run import ROOT,sql
values=[]
for _ in range(3):
    before=int(sql("SHOW GLOBAL STATUS LIKE 'Com_select';").strip().split('\t')[1])
    after=int(sql("SHOW GLOBAL STATUS LIKE 'Com_select';").strip().split('\t')[1])
    values.append({'before':before,'after':after,'delta':after-before})
(ROOT/'sql-counter-calibration.json').write_text(json.dumps(values,indent=2),encoding='utf-8')
print(values)
