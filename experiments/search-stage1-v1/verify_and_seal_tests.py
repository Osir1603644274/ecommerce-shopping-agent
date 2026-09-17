"""Run the explicit relevant suite against a frozen code snapshot and bind XML."""
import json,subprocess,sys,xml.etree.ElementTree as ET
from capture_code import capture,ROOT,REPO
from stage1 import digest,dump
from integrity import checked_file

def verify():
    capture()
    manifest=ROOT/"provenance/code-manifest.json"
    manifest_sha=digest(manifest)
    xml=ROOT/"provenance/tests.xml"
    command=[sys.executable,"-m","pytest","retrieval_judgment_pool_core/tests","experiments/search-stage1-v1/test_stage1.py","-q",f"--junitxml={xml}"]
    with (ROOT/"logs/tests.log").open("w",encoding="utf-8") as log:
        subprocess.run(command,cwd=REPO,stdout=log,stderr=subprocess.STDOUT,check=True)
    checked_file(manifest,manifest_sha)
    for item in json.loads(manifest.read_text(encoding="utf-8-sig"))["files"]:checked_file(item["source"],item["sha256"],item["bytes"])
    document=ET.parse(xml).getroot()
    tests=sorted(f"{c.attrib.get('classname','')}::{c.attrib['name']}" for c in document.iter("testcase"))
    if len(tests)!=37 or len(set(tests))!=37:raise ValueError("Expected all 37 explicit relevant cases")
    if any(int(s.attrib.get(k,0)) for s in document.iter("testsuite") for k in ("failures","errors","skipped")):raise ValueError("Suite is not entirely passing")
    dump(ROOT/"provenance/test-receipt.json",{"code_manifest_sha256":manifest_sha,"xml_sha256":digest(xml),"required_tests":tests,"command":command,"status":"PASS"})
    print("37 tests passed; code and XML receipt frozen")

if __name__=="__main__":verify()
