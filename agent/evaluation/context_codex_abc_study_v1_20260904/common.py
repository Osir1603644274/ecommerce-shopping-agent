"""Shared bounded study utilities. Native subscription auth only."""
from pathlib import Path
import importlib.metadata
import json
import tiktoken
from agent.evaluation.context_codex_abc_pilot_v1_20260904 import pilot as p

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ARMS = p.ARMS
ENC = tiktoken.get_encoding("o200k_base")
SEED = 20260904
read, write_new, append, canonical, sha, file_sha, now = p.read, p.write_new, p.append, p.canonical, p.sha, p.file_sha, p.now

def tokens(text):
    return len(ENC.encode(text, disallowed_special=()))

def tokenizer_receipt():
    # Freeze the actual vocabulary, not merely a package name.
    import hashlib
    digest = hashlib.sha256()
    for value, rank in sorted(ENC._mergeable_ranks.items(), key=lambda x: x[1]):
        digest.update(rank.to_bytes(4, "little") + len(value).to_bytes(4, "little") + value)
    return {"library": "tiktoken", "version": importlib.metadata.version("tiktoken"),
        "encoding": ENC.name, "vocabularySha256": digest.hexdigest(),
        "scope": "FIXED_APPLICATION_TEXT_LENGTH_NOT_CODEX_BILLING_OR_MODEL_NATIVE_TOKENIZER"}

def jsonl(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]

def verify():
    manifest = read(HERE / "inputs/manifest.json")
    for name, digest in manifest["sources"].items():
        if file_sha(ROOT / name) != digest: raise RuntimeError("source_drift:" + name)
    for name, digest in manifest["inputFiles"].items():
        if file_sha(HERE / "inputs" / name) != digest: raise RuntimeError("input_drift:" + name)
    if tokenizer_receipt() != manifest["tokenizer"]: raise RuntimeError("tokenizer_drift")
    return manifest
