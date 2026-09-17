"""Snapshot already closed arms for lossless review-capacity diagnostics only."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, sha, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import tokens
from agent.evaluation.context_history_review_v2_20260905.run import load_samples, prepare
from agent.evaluation.context_history_review_v2_20260905.evidence import encode


def run(cohort, output):
    if output.exists() or output.parent.resolve() != HERE.resolve():
        raise ValueError('new_direct_study_output_required')
    started = json.loads((cohort / 'started.json').read_text(encoding='utf8'))
    verify_sources(started['sources'])
    jobs = {job['label']: job for job in started['jobs']}
    for label in ('B', 'C'):
        closed = json.loads((cohort / (label + '001_closed.json')).read_text(encoding='utf8'))
        if closed['dataCollectionComplete'] is not True or closed['childExitCode'] != 0:
            raise ValueError('closed_arm_required')
        if Path(closed['attempt']).resolve() != Path(jobs[label]['output']).resolve():
            raise ValueError('closed_arm_binding_mismatch')
    output.mkdir()
    reports = []
    for number, label in enumerate(('B', 'C'), start=1):
        directory = Path(jobs[label]['output']).resolve()
        name = directory.relative_to(HERE.resolve()).as_posix()
        samples, mapping = load_samples([name], started['plannedTurnsEach'])
        sample = samples[0]
        sample['sampleId'] = 'sample-' + sha(['lossless-review-v2', number, name])[:12]
        sample_path = output / (label + '_sample.json')
        binding_path = output / (label + '_binding.json')
        write_new(sample_path, sample)
        write_new(binding_path, mapping[0])
        packet = encode([sample])
        audits = [prepare(sample, list(range(first, min(first + 6, started['plannedTurnsEach'] + 1))))[2]
                  for first in range(1, started['plannedTurnsEach'] + 1, 6)]
        write_new(output / (label + '_v2_packet_audit.json'), audits)
        evidence = packet['samples'][0]['verifiedEvidenceByTurn']
        fields = sorted({key for row in evidence for key in row})
        report = {'arm': label, 'sampleSha256': file_sha(sample_path),
            'bindingSha256': file_sha(binding_path), 'dialogueHash': sha(sample['turns']),
            'maxRequestTokens': max(a['applicationRequestTokens'] for a in audits),
            'dialogueTokens': tokens(sample['turns']),
            'packetTopLevelTokens': {key: tokens(value) for key, value in packet.items()},
            'evidenceFieldTokens': {key: tokens([row.get(key) for row in evidence]) for key in fields},
            'modelCalls': 0}
        reports.append(report)
        print(json.dumps(report), flush=True)
    verify_sources(started['sources'])
    write_new(output / 'result.json', {'kind': 'CLOSED_BC_REVIEW_CAPACITY_DIAGNOSTIC_NOT_JUDGMENTS',
        'reports': reports, 'modelCalls': 0, 'formalAcceptance': False,
        'diagnosticSourceSha256': file_sha(__file__),
        'note': 'No A sample and no ratings. Immutable diagnostic snapshots for codec tests; full three-arm preparation still required before real review.'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('cohort', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    run(args.cohort.resolve(), args.output.resolve())
