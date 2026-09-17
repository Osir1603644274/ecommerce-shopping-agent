"""Generated-file NTFS -> LZX -> decoded-byte round trip; no model calls."""
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new


def allocated_bytes(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.GetCompressedFileSizeW
    query.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    query.restype = ctypes.c_uint32
    high = ctypes.c_uint32()
    ctypes.set_last_error(0)
    low = query(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return (high.value << 32) | low


def run(output):
    if output.exists() or not output.is_absolute() or output.parent.resolve() != HERE.resolve():
        raise ValueError("new_direct_child_probe_required")
    began = now()
    output.mkdir()
    # Synthetic test fixture only, not a benchmark dialogue or existing artifact.
    payload = b"".join((json.dumps({"turn": n, "synthetic": True,
        "history": ["storage roundtrip fixture only; no shopping evidence"] * 80},
        sort_keys=True) + "\n").encode("utf-8") for n in range(256))
    fixture = output / "generated_fixture.jsonl"
    fixture.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    write_new(output / "before.json", {"beganAt": began, "fixture": str(fixture),
        "bytes": len(payload), "sha256": expected, "allocatedBytes": allocated_bytes(fixture)})
    stages = []
    for label, flags in (("ntfs", ["/c"]), ("lzx", ["/c", "/f", "/exe:lzx"]),
            ("uncompressed_exe", ["/u", "/exe"]), ("restored_ntfs", ["/c", "/f"])):
        command = ["compact.exe", *flags, "/q", str(fixture)]
        done = subprocess.run(command, capture_output=True, text=True,
            encoding="oem", errors="replace", timeout=60)
        actual = hashlib.sha256(fixture.read_bytes()).hexdigest()
        stages.append({"stage": label, "command": command, "exitCode": done.returncode,
            "sha256": actual, "bytesUnchanged": actual == expected,
            "allocatedBytes": allocated_bytes(fixture), "stdout": done.stdout, "stderr": done.stderr})
        write_new(output / (label + ".json"), stages[-1])
        if done.returncode or actual != expected:
            break
    success = len(stages) == 4 and all(s["exitCode"] == 0 and s["bytesUnchanged"] for s in stages)
    result = {"beganAt": began, "at": now(), "status": "ROUNDTRIP_PASS" if success else "HOLD",
        "stages": stages, "deletedFiles": 0, "modelCalls": 0,
        "scope": "Generated fixture only; does not prove old-log compatibility, savings, or scientific acceptance."}
    write_new(output / "result.json", result)
    print(json.dumps({"status": result["status"], "stages": [{k: s[k] for k in
        ("stage", "exitCode", "bytesUnchanged", "allocatedBytes")} for s in stages]}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
