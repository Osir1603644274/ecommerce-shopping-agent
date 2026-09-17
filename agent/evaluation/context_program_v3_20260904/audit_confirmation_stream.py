"""Same audit predicates/digests, streaming file hashes for the large state log."""
import hashlib
from pathlib import Path
from . import audit_multiturn as audit_module
from .common import HERE, json_new, now
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
if __name__=='__main__':
    audit_module.file_sha=digest
    audit_module.audit('confirm001')
    json_new(HERE/'p4/confirm001/audit_execution.json',{'at':now(),
        'auditorSha256':digest(audit_module.__file__),'streamingWrapperSha256':digest(__file__),
        'change':'streaming file IO for SHA256 only; predicates and digest algorithm unchanged'})
