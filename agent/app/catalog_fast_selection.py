"""Load only a code-bound, development-approved retrieval configuration."""
import hashlib
import json
from pathlib import Path


def selection_file(directory, version):
    if version not in (1, 2, 3):
        raise ValueError('unsupported_catalog_index_version')
    return Path(directory)/('selected.json' if version == 1 else f'selected-v{version}.json')


def validated_selection(directory, version, model_path, *, code_directory=None):
    directory = Path(directory).resolve()
    code_directory = Path(code_directory or Path(__file__).parent)
    selected = json.loads(selection_file(directory, version).read_text('utf-8'))
    evidence = (directory/selected['evaluation']).resolve()
    if not evidence.is_relative_to(directory):
        raise ValueError('fast_selection_path')
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    report = json.loads(evidence.read_text('utf-8'))
    module = 'catalog_fast_retrieval' if version == 1 else f'catalog_fast_retrieval_v{version}'
    if (selected.get('status') != 'VERIFIED_DEV_CONFIGURATION'
        or selected.get('runtimeVersion', 1) != version
        or sha(evidence) != selected['evaluationSha256']
        or report.get('status') != 'DEV_GATE_PASS'
        or report['parameters'] != selected['parameters']
        or selected['fastCodeSha256'] != sha(code_directory/(module+'.py'))
        or Path(model_path).resolve() != Path(selected['modelPath']).resolve()
        or (version >= 2 and selected['parentFastCodeSha256'] != sha(code_directory/'catalog_fast_retrieval.py'))
        or (version == 3 and selected['indexRuntimeCodeSha256'] != sha(code_directory/'catalog_fast_retrieval_v2.py'))):
        raise ValueError('fast_selection_gate_failed')
    return selected
