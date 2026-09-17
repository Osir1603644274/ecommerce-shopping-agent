"""Continue this authorized execution as soon as both complete indices exist."""
import subprocess
import sys
import time
from pathlib import Path

from stage1 import log

ROOT=Path("D:/agent-datasets/search-stage1-v1")
HERE=Path(__file__).resolve().parent

while not all((ROOT/"indexes/kuaisearch"/name).exists() for name in ("lexical.manifest.json","dense.manifest.json")):
    time.sleep(15)
log("kuai_indices_ready_advancing")
commands=[("retrieve.py","retrieve","--source","kuaisearch")]
for split in ("dev","test"):
    commands.extend([("retrieve.py","pool","--source","kuaisearch","--split",split),
                     ("silver.py","packets","--source","kuaisearch","--split",split)])
for command in commands:
    name="-".join(command).replace(".py","").replace("--","")
    with (ROOT/"logs"/(name+".log")).open("a",encoding="utf-8") as output:
        subprocess.run([sys.executable,str(HERE/command[0]),*command[1:]],stdout=output,stderr=subprocess.STDOUT,check=True)
    log("kuai_stage_complete",command=list(command))
