"""Offline diagnosis of completed-suite setup failures, not a formal rerun."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from xml.etree import ElementTree as ET

from .common import HERE, ROOT, check_freeze, file_sha, json_new, now


def main():
    check_freeze()
    out = HERE / "p8/environment_diagnostic001"
    out.mkdir(parents=True, exist_ok=False)
    source = HERE / "p8/full001/pytest-full.xml"
    selected = []
    for case in ET.parse(source).getroot().iter("testcase"):
        if case.find("failure") is None: continue
        parts = case.attrib["classname"].split(".")
        module_index = next(i for i,p in enumerate(parts) if p.startswith("test_"))
        node = "agent/tests/" + parts[module_index] + ".py"
        for sub in parts[module_index+1:]: node += "::" + sub
        selected.append(node + "::" + case.attrib["name"])
    if len(selected) != 6: raise RuntimeError("expected_six_environment_failures")
    external = Path(tempfile.mkdtemp(prefix="context-v2-offline-diagnostic-"))
    os.environ["DEEPSEEK_API_KEY"] = "offline-test-placeholder-not-a-credential"
    os.environ["OPENAI_API_KEY"] = "offline-test-placeholder-not-a-credential"
    os.chdir(ROOT)
    import sys
    for p in (ROOT, ROOT / "agent"):
        if str(p) not in sys.path: sys.path.insert(0, str(p))
    import httpx
    import pytest
    original_async, original_sync = httpx.AsyncClient.send, httpx.Client.send
    blocked = []
    def assert_local(request):
        if request.url.host not in {"testserver", "127.0.0.1", "localhost", "::1"}:
            blocked.append({"host": request.url.host, "method": request.method})
            raise RuntimeError("external_http_disabled_in_offline_diagnostic")
    async def guarded_async(self, request, *args, **kwargs):
        assert_local(request)
        return await original_async(self, request, *args, **kwargs)
    def guarded_sync(self, request, *args, **kwargs):
        assert_local(request)
        return original_sync(self, request, *args, **kwargs)
    command = [*selected, "-q", "--junitxml=" + str(out / "pytest.xml"), "--basetemp=" + str(external / "cases")]
    json_new(out / "started.json", {"at": now(), "pid": os.getpid(), "kind": "OFFLINE_SETUP_DIAGNOSIS",
        "runnerSha256": file_sha(__file__), "sourceFailureXmlSha256": file_sha(source),
        "pytestArgs": command, "dummyKey": True, "externalHTTPBlocked": True,
        "scratchDirectoryRetained": str(external), "cwd": str(ROOT)})
    with patch.object(httpx.AsyncClient, "send", guarded_async), patch.object(httpx.Client, "send", guarded_sync):
        code = pytest.main(command)
    check_freeze()
    json_new(out / "result.json", {"exitCode": int(code), "externalHTTPAttemptsBlocked": blocked,
        "providerCalls": 0, "formalP2Reruns": 0, "originalFullSuiteUnchanged": True,
        "purpose": "attribute six setup failures; does not replace the full-suite receipt"})


if __name__ == "__main__": main()
