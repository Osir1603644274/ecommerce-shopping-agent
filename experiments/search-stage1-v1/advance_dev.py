"""Score fixed dev candidates and prepare top-up packets when dependencies finish."""
import subprocess
import sys
import time
from pathlib import Path

from stage1 import log

ROOT=Path("D:/agent-datasets/search-stage1-v1")
HERE=Path(__file__).resolve().parent

def run(script,*arguments):
    name="-".join((script,*arguments)).replace(".py","").replace("--","")
    with (ROOT/"logs"/(name+".log")).open("a",encoding="utf-8") as output:
        subprocess.run([sys.executable,str(HERE/script),*arguments],stdout=output,stderr=subprocess.STDOUT,check=True)
    log("dev_stage_complete",script=script,arguments=list(arguments))

for source in ("multicpr","kuaisearch"):
    for epoch in (1,2,3):
        dependencies=[ROOT/"training/run-lora-v1"/f"epoch-{epoch}/complete.json",
                      ROOT/"pools"/f"{source}-dev.json",ROOT/"labeling/provenance"/f"{source}-dev.mapping.jsonl"]
        while not all(p.exists() for p in dependencies):time.sleep(15)
        run("evaluate.py","score","--source",source,"--split","dev","--epoch",str(epoch))
    run("evaluate.py","topups","--source",source,"--split","dev")
    run("silver.py","supplement","--source",source,"--split","dev")
