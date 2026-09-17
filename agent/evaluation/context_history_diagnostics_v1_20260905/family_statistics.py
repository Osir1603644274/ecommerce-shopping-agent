"""Pure, deterministic family-paired arithmetic for the prospective study.

Callers must separately verify manifests, episode identities and native/judge
artifact hashes. This module neither loads evidence nor declares it verified.
Primary comparisons never discard an incomplete family. Extra repetitions and
recoveries enter spend accounting, not the primary independent-family sample.
"""
import math
import random

ARMS = ('A', 'B', 'C')
DIMENSIONS = ('correctness', 'constraints', 'relevance', 'usefulness')
REQUIRED = {'episodeId', 'familyId', 'arm', 'role', 'complete', 'nativeTokens',
    'wholeAgentMs', 'quality', 'qualityComplete', 'criticalErrors', 'criticalAuditComplete'}


def _number(value, upper):
    return type(value) in (int, float) and 0 <= value <= upper and math.isfinite(value)


def validate(rows, family_ids):
    if (not isinstance(family_ids, (list, tuple)) or not family_ids or
            any(not isinstance(f, str) or not f for f in family_ids) or len(set(family_ids)) != len(family_ids)):
        raise ValueError('explicit_unique_planned_family_ids_required')
    if not isinstance(rows, list) or not rows:
        raise ValueError('episode_rows_required')
    seen, primary = set(), {}
    for row in rows:
        if not isinstance(row, dict) or not REQUIRED <= row.keys():
            raise ValueError('episode_fields_missing')
        if not isinstance(row['episodeId'], str) or not row['episodeId'] or row['episodeId'] in seen:
            raise ValueError('unique_episode_identity_required')
        seen.add(row['episodeId'])
        if row['familyId'] not in family_ids or row['arm'] not in ARMS or row['role'] not in ('primary', 'repeat', 'recovery'):
            raise ValueError('unplanned_family_arm_or_role')
        if any(type(row[k]) is not bool for k in ('complete', 'qualityComplete', 'criticalAuditComplete')):
            raise ValueError('explicit_boolean_completion_required')
        for key in ('nativeTokens', 'criticalErrors'):
            value = row[key]
            if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                raise ValueError('nonnegative_integer_or_unknown_required:' + key)
        if row['wholeAgentMs'] is not None and not _number(row['wholeAgentMs'], 1e15):
            raise ValueError('finite_nonnegative_agent_time_or_unknown_required')
        quality = row['quality']
        if quality is not None and (not isinstance(quality, dict) or set(quality) != set(DIMENSIONS) or
                any(not _number(v, 4) for v in quality.values())):
            raise ValueError('four_bounded_quality_means_or_unknown_required')
        if row['qualityComplete'] and (quality is None or not row['complete']):
            raise ValueError('full_episode_quality_cannot_be_partial')
        if row['criticalAuditComplete'] and row['criticalErrors'] is None:
            raise ValueError('completed_critical_audit_requires_count')
        if row['role'] == 'primary':
            key = (row['familyId'], row['arm'])
            if key in primary:
                raise ValueError('duplicate_primary_family_arm')
            primary[key] = row
    if set(primary) != {(f, a) for f in family_ids for a in ARMS}:
        raise ValueError('every_planned_primary_family_arm_must_be_present')
    return primary


def _spend(rows):
    result = {}
    for arm in ARMS:
        selected = [r for r in rows if r['arm'] == arm]
        values = {}
        for key in ('nativeTokens', 'wholeAgentMs'):
            unknown = sorted(r['episodeId'] for r in selected if r[key] is None)
            subtotal = sum(r[key] for r in selected if r[key] is not None)
            values[key] = {'knownSubtotal': subtotal, 'completeTotal': None if unknown else subtotal,
                'unknownEpisodeIds': unknown}
        result[arm] = {**values, 'episodes': len(selected),
            'incompleteEpisodeIds': sorted(r['episodeId'] for r in selected if not r['complete']),
            'roles': {role: sum(r['role'] == role for r in selected) for role in ('primary', 'repeat', 'recovery')}}
    return result


def _pair_points(primary, families, arm):
    pairs = [(primary[f, 'A'], primary[f, arm]) for f in families]
    complete = all(a['complete'] and b['complete'] for a, b in pairs)
    point = {'pooledTokenSavingPercent': None, 'macroFamilyTokenSavingPercent': None,
        'pooledTimeGrowthPercent': None, 'macroFamilyTimeGrowthPercent': None,
        **{'qualityLoss_' + dim: None for dim in DIMENSIONS}}
    for field, pooled, macro, saving in (
        ('nativeTokens', 'pooledTokenSavingPercent', 'macroFamilyTokenSavingPercent', True),
        ('wholeAgentMs', 'pooledTimeGrowthPercent', 'macroFamilyTimeGrowthPercent', False)):
        if not complete or any(a[field] is None or b[field] is None or a[field] <= 0 for a, b in pairs):
            continue
        sign = -1 if saving else 1
        point[pooled] = sign * 100 * (sum(b[field] for a, b in pairs) / sum(a[field] for a, b in pairs) - 1)
        point[macro] = sum(sign * 100 * (b[field] / a[field] - 1) for a, b in pairs) / len(pairs)
    if complete and all(a['qualityComplete'] and b['qualityComplete'] for a, b in pairs):
        for dim in DIMENSIONS:
            point['qualityLoss_' + dim] = sum(a['quality'][dim] - b['quality'][dim] for a, b in pairs) / len(pairs)
    return point


def _quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def analyze(rows, family_ids, *, draws=10000, seed=20260906):
    """Point thresholds are prospective; percentile intervals are descriptive.

    Resource primary estimand = ratio of pooled complete-episode totals; also
    report the distinct equal-family mean ratio. Quality = equal-family paired
    mean loss. All arms use the SAME family resampling indices. No p-values,
    causality assertion, per-turn independent resampling or overall PASS.
    """
    if type(draws) is not int or not 10 <= draws <= 100000 or type(seed) is not int or seed < 0:
        raise ValueError('bounded_integer_draws_and_nonnegative_seed_required')
    primary = validate(rows, family_ids)
    families = sorted(family_ids)
    rng = random.Random(seed)
    resamples = [tuple(rng.choice(families) for _ in families) for _ in range(draws)] if len(families) >= 2 else []
    comparisons = {}
    for arm in ('B', 'C'):
        point = _pair_points(primary, families, arm)
        distributions = {key: [] for key, value in point.items() if value is not None}
        for sample in resamples if distributions else ():
            for key, value in _pair_points(primary, sample, arm).items():
                if key in distributions:
                    distributions[key].append(value)
        intervals = {key: ([_quantile(distributions[key], .025), _quantile(distributions[key], .975)]
            if key in distributions and resamples else None) for key in point}
        paired_rows = [primary[f, a] for f in families for a in ('A', arm)]
        known_critical = sum(r['criticalErrors'] or 0 for r in paired_rows)
        audit_complete = all(r['criticalAuditComplete'] and r['complete'] for r in paired_rows)
        critical_free = False if known_critical else (True if audit_complete else None)
        quality_known = all(point['qualityLoss_' + d] is not None for d in DIMENSIONS)
        token_known = point['pooledTokenSavingPercent'] is not None
        time_known = point['pooledTimeGrowthPercent'] is not None
        a_tokens = sum(primary[f, 'A']['nativeTokens'] for f in families) if token_known else None
        b_tokens = sum(primary[f, arm]['nativeTokens'] for f in families) if token_known else None
        a_time = sum(primary[f, 'A']['wholeAgentMs'] for f in families) if time_known else None
        b_time = sum(primary[f, arm]['wholeAgentMs'] for f in families) if time_known else None
        comparisons[arm] = {'point': point, 'descriptivePercentile95': intervals,
            'primaryIncompleteEpisodeIds': sorted(r['episodeId'] for r in paired_rows if not r['complete']),
            'observedThresholds': {
                'tokenAtLeast10Percent': b_tokens * 100 <= a_tokens * 90 if token_known else None,
                'timeIncreaseAtMost15Percent': b_time * 100 <= a_time * 115 + 1e-6 if time_known else None,
                'everyQualityMeanLossAtMostHalfPoint': all(point['qualityLoss_' + d] <= .5 + 1e-12 for d in DIMENSIONS) if quality_known else None},
            'pairedCriticalErrorsKnownSubtotal': known_critical,
            'pairedCriticalAuditComplete': audit_complete, 'criticalErrorFree': critical_free,
            'overallAcceptance': 'NOT_DECIDED_BY_ARITHMETIC'}
    return {'kind': 'FAMILY_PAIRED_ARITHMETIC_NOT_EVIDENCE_OR_FORMAL_ACCEPTANCE',
        'familyIds': families, 'independentFamilyCount': len(families),
        'primaryEpisodeCount': len(primary), 'extraEpisodeCount': len(rows) - len(primary),
        'primaryEpisodeIds': sorted(r['episodeId'] for r in primary.values()),
        'allAttemptedEpisodeSpend': _spend(rows),
        'comparisons': comparisons,
        'bootstrap': {'unit': 'source_family', 'pairedAcrossArms': True,
            'draws': draws if resamples else 0, 'seed': seed,
            'intervalMethod': 'percentile_linear_interpolation_2.5_97.5',
            'singleFamilyInterval': 'UNAVAILABLE_NOT_INDEPENDENT_REPETITION',
            'pValues': None, 'smallSyntheticFamilySampleCaveat': True},
        'evidenceVerification': 'REQUIRED_UPSTREAM_NOT_PERFORMED_BY_THIS_MODULE',
        'extrasPolicy': 'All repeated/recovery spend retained; never increase primary independent families or substitute primary rows.',
        'formalAcceptance': False}
