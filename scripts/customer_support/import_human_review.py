"""Validate externally completed review forms without editing original run evidence."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from export_human_review import digest, read


def import_review(packet, completed, output, *, reviewer_type='HUMAN'):
    if reviewer_type not in {'HUMAN', 'AGENT'}:
        raise ValueError('reviewer type must be HUMAN or AGENT')
    review_key = 'humanReview' if reviewer_type == 'HUMAN' else 'agentReview'
    manifest = read(packet/'MANIFEST.json')
    source = Path(manifest['sourceRun']).resolve()
    template = packet/'review.jsonl'
    if digest(template) != manifest['reviewTemplateSha256']:
        raise ValueError('original review template changed')
    if digest(source/'observations.json') != manifest['observationsSha256']:
        raise ValueError('original observations changed')
    originals = [json.loads(line) for line in template.read_text(encoding='utf-8').splitlines()]
    edited = [json.loads(line) for line in completed.read_text(encoding='utf-8').splitlines()]
    key = lambda row: (row['caseId'], row['repetition'])
    by_key = {key(row): row for row in edited}
    if len(by_key) != len(edited) or set(by_key) != {key(row) for row in originals}:
        raise ValueError('complete unique review rows required')
    reviews = {}
    for original in originals:
        row = by_key[key(original)]
        if {k: v for k, v in row.items() if k != review_key} != {k: v for k, v in original.items() if k != review_key}:
            raise ValueError('answer or evidence fields changed')
        evidence = {e['path']: e for e in original['evidence']}
        for relative, entry in evidence.items():
            path = (source/relative).resolve()
            if not path.is_relative_to(source) or digest(path) != entry['sha256']:
                raise ValueError('original evidence changed or escaped source')
        review = row[review_key]
        if reviewer_type == 'AGENT' and review.get('reviewerType') != 'AGENT':
            raise ValueError('agent review must explicitly identify its reviewer type')
        if review.get('status') != 'REVIEWED' or not str(review.get('reviewer') or '').strip():
            raise ValueError('explicit reviewer and completed status required')
        timestamp = datetime.fromisoformat(review.get('reviewedAt') or '')
        if timestamp.tzinfo is None:
            raise ValueError('review time requires timezone')
        assertions = review.get('assertions')
        if not isinstance(assertions, list):
            raise ValueError('assertion records required')
        if not assertions and not str(review.get('noAssertionsReason') or '').strip():
            raise ValueError('zero assertions require an explanation')
        answers = [a.get('answer') or '' for a in original['answers']]
        for assertion in assertions:
            if reviewer_type == 'AGENT' and type(assertion.get('critical')) is not bool:
                raise ValueError('agent assertions require explicit critical classification')
            quote = assertion.get('quote')
            if not isinstance(quote, str) or not quote.strip() or not any(quote in answer for answer in answers):
                raise ValueError('assertion quote absent from original answers')
            if assertion.get('evidencePath') not in evidence:
                raise ValueError('assertion must cite registered evidence')
            if not all(str(assertion.get(k) or '').strip() for k in ('evidenceLocation', 'reason')):
                raise ValueError('evidence location and judgment reason required')
            if assertion.get('supported') is not None and type(assertion.get('supported')) is not bool:
                raise ValueError('support must be true, false, or null')
        reviews[key(row)] = dict(status='REVIEWED', reviewer=review['reviewer'], reviewedAt=review['reviewedAt'],
                                 reviewerType=reviewer_type,
                                 assertions=len(assertions), supported=sum(a.get('supported') is True for a in assertions),
                                 unknown=sum(a.get('supported') is None for a in assertions),
                                 criticalComplete=all(type(a.get('critical')) is bool for a in assertions),
                                 criticalTotal=sum(a.get('critical') is True for a in assertions),
                                 criticalSupported=sum(a.get('critical') is True and a.get('supported') is True for a in assertions))
    observations = deepcopy(read(source/'observations.json'))
    if {key(row) for row in observations} != set(reviews):
        raise ValueError('review does not match original observations')
    for row in observations:
        review = reviews[key(row)]
        row[review_key] = review
        row['independentFactReview'] = dict(status='REVIEWED', reviewerType=reviewer_type,
            sourceReviewSha256=digest(completed), complete=review['criticalComplete'],
            criticalTotal=review['criticalTotal'], criticalSupported=review['criticalSupported'])
    output.mkdir(parents=True, exist_ok=False)
    (output/'observations.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'review.completed.jsonl').write_bytes(completed.read_bytes())
    report = dict(status='EXTERNAL_REVIEW_IMPORTED_NOT_AUTOMATIC_ACCEPTANCE', reviewerType=reviewer_type, sourceRun=str(source),
                  sourceObservationsSha256=manifest['observationsSha256'], completedReviewSha256=digest(completed),
                  note='Importer validates provenance and structure; reviewer identity and actual human work require external attestation.')
    (output/'MANIFEST.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--completed', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reviewer-type', choices=('HUMAN', 'AGENT'), default='HUMAN')
    args = parser.parse_args()
    print(json.dumps(import_review(args.packet, args.completed, args.output, reviewer_type=args.reviewer_type)))
