"""Constructed arithmetic fixtures only; no SUT/model/formal data access."""
from copy import deepcopy
import unittest
from .family_statistics import analyze, DIMENSIONS


def fixture(families=('f1', 'f2')):
    return [{'episodeId': f + '-' + a, 'familyId': f, 'arm': a, 'role': 'primary',
        'complete': True, 'nativeTokens': 1000 if a == 'A' else 900,
        'wholeAgentMs': 1000 if a == 'A' else 1150,
        'quality': {d: 4 if a == 'A' else 3.5 for d in DIMENSIONS},
        'qualityComplete': True, 'criticalErrors': 0, 'criticalAuditComplete': True}
        for f in families for a in 'ABC']


def calc(rows, families=('f1', 'f2'), **kwargs):
    return analyze(rows, list(families), draws=100, **kwargs)


class FamilyStatisticsTests(unittest.TestCase):
    def test_exact_boundary_point_rules_and_constant_intervals(self):
        result = calc(fixture())
        row = result['comparisons']['B']
        self.assertTrue(all(row['observedThresholds'].values()))
        self.assertAlmostEqual(row['point']['pooledTokenSavingPercent'], 10)
        self.assertAlmostEqual(row['point']['pooledTimeGrowthPercent'], 15)
        self.assertEqual(row['descriptivePercentile95']['qualityLoss_constraints'], [.5, .5])
        self.assertEqual(result['independentFamilyCount'], 2)
        self.assertFalse(result['formalAcceptance'])

    def test_just_outside_all_thresholds_fails(self):
        rows = fixture()
        for r in rows:
            if r['arm'] == 'B':
                r.update(nativeTokens=901, wholeAgentMs=1151,
                    quality={d: 3.49 for d in DIMENSIONS})
        self.assertTrue(all(v is False for v in calc(rows)['comparisons']['B']['observedThresholds'].values()))

    def test_pooled_and_equal_family_ratios_are_distinct(self):
        rows = fixture()
        for r in rows:
            r['nativeTokens'] = ({'A': 100, 'B': 80, 'C': 80} if r['familyId'] == 'f1'
                else {'A': 900, 'B': 450, 'C': 450})[r['arm']]
        point = calc(rows)['comparisons']['B']['point']
        self.assertAlmostEqual(point['pooledTokenSavingPercent'], 47)
        self.assertAlmostEqual(point['macroFamilyTokenSavingPercent'], 35)

    def test_repeat_and_recovery_costs_not_independent_families(self):
        base = fixture()
        extra = deepcopy(base[1])
        extra.update(episodeId='repeat-B', role='repeat', nativeTokens=777, wholeAgentMs=888)
        recovery = deepcopy(extra)
        recovery.update(episodeId='failed-recovery-B', role='recovery', complete=False,
            qualityComplete=False, criticalAuditComplete=False, criticalErrors=None,
            nativeTokens=None, wholeAgentMs=100)
        result = calc(base + [extra, recovery])
        self.assertEqual(result['comparisons'], calc(base)['comparisons'])
        self.assertEqual(result['independentFamilyCount'], 2)
        self.assertEqual(result['extraEpisodeCount'], 2)
        spend = result['allAttemptedEpisodeSpend']['B']
        self.assertEqual(spend['nativeTokens']['knownSubtotal'], 2577)
        self.assertIsNone(spend['nativeTokens']['completeTotal'])
        self.assertEqual(spend['nativeTokens']['unknownEpisodeIds'], ['failed-recovery-B'])

    def test_incomplete_family_is_not_silently_dropped(self):
        rows = fixture()
        rows[4].update(complete=False, qualityComplete=False)
        result = calc(rows)
        row = result['comparisons']['B']
        self.assertEqual(row['primaryIncompleteEpisodeIds'], ['f2-B'])
        self.assertTrue(all(v is None for v in row['point'].values()))
        self.assertTrue(all(v is None for v in row['descriptivePercentile95'].values()))
        self.assertIsNone(row['criticalErrorFree'])
        self.assertEqual(result['allAttemptedEpisodeSpend']['B']['nativeTokens']['completeTotal'], 1800)
        self.assertIsNotNone(result['comparisons']['C']['point']['pooledTokenSavingPercent'])

    def test_unknown_native_cost_is_not_zero(self):
        rows = fixture()
        rows[1]['nativeTokens'] = None
        result = calc(rows)
        row = result['comparisons']['B']
        self.assertIsNone(row['point']['pooledTokenSavingPercent'])
        self.assertIsNone(row['observedThresholds']['tokenAtLeast10Percent'])
        self.assertIsNotNone(row['point']['pooledTimeGrowthPercent'])
        self.assertEqual(result['allAttemptedEpisodeSpend']['B']['nativeTokens']['knownSubtotal'], 900)

    def test_unknown_time_is_not_zero(self):
        rows = fixture()
        rows[0]['wholeAgentMs'] = None
        self.assertIsNone(calc(rows)['comparisons']['B']['point']['pooledTimeGrowthPercent'])

    def test_incomplete_quality_or_audit_cannot_look_passed(self):
        rows = fixture()
        rows[1].update(qualityComplete=False, criticalAuditComplete=False, criticalErrors=None)
        row = calc(rows)['comparisons']['B']
        self.assertIsNone(row['observedThresholds']['everyQualityMeanLossAtMostHalfPoint'])
        self.assertIsNone(row['criticalErrorFree'])

    def test_critical_error_survives_good_means(self):
        rows = fixture()
        rows[0]['criticalErrors'] = 1
        row = calc(rows)['comparisons']['B']
        self.assertTrue(row['observedThresholds']['everyQualityMeanLossAtMostHalfPoint'])
        self.assertFalse(row['criticalErrorFree'])
        self.assertEqual(row['pairedCriticalErrorsKnownSubtotal'], 1)
        self.assertEqual(row['overallAcceptance'], 'NOT_DECIDED_BY_ARITHMETIC')

    def test_single_family_has_no_inferential_interval(self):
        result = calc(fixture(('f1',)), ('f1',))
        self.assertEqual(result['bootstrap']['draws'], 0)
        self.assertTrue(all(v is None for v in result['comparisons']['B']['descriptivePercentile95'].values()))

    def test_zero_baseline_is_explicitly_unavailable(self):
        rows = fixture()
        rows[0]['nativeTokens'] = 0
        self.assertIsNone(calc(rows)['comparisons']['B']['point']['pooledTokenSavingPercent'])

    def test_pairing_same_draws_and_order_independence(self):
        rows = fixture()
        rows[4]['nativeTokens'] = rows[5]['nativeTokens'] = 500
        result = calc(rows, seed=1234)
        self.assertEqual(result, calc(list(reversed(rows)), ('f2', 'f1'), seed=1234))
        self.assertEqual(result['comparisons']['B']['descriptivePercentile95'],
            result['comparisons']['C']['descriptivePercentile95'])

    def test_missing_duplicate_or_unplanned_identity_rejected(self):
        bad_sets = [fixture()[:-1], fixture() + [deepcopy(fixture()[0])]]
        changed = fixture()
        changed[0]['familyId'] = 'undeclared'
        bad_sets.append(changed)
        changed = fixture()
        changed[0]['role'] = 'posthoc_winner'
        bad_sets.append(changed)
        changed = fixture()
        extra_primary = deepcopy(changed[0])
        extra_primary['episodeId'] = 'different-id-same-primary'
        bad_sets.append(changed + [extra_primary])
        for rows in bad_sets:
            with self.assertRaises(ValueError):
                calc(rows)

    def test_invalid_numbers_and_completion_contracts_rejected(self):
        changes = [{'nativeTokens': True}, {'nativeTokens': -1}, {'wholeAgentMs': float('nan')},
            {'wholeAgentMs': float('inf')}, {'wholeAgentMs': -1}, {'criticalErrors': True},
            {'quality': {d: 4.1 for d in DIMENSIONS}}, {'quality': None},
            {'complete': False}, {'complete': 'true'}, {'criticalErrors': None}]
        for update in changes:
            rows = fixture()
            rows[0].update(update)
            with self.assertRaises(ValueError, msg=str(update)):
                calc(rows)
        for draws, seed in [(True, 1), (0, 1), (100001, 1), (100, True), (100, -1)]:
            with self.assertRaises(ValueError):
                analyze(fixture(), ['f1', 'f2'], draws=draws, seed=seed)


if __name__ == '__main__':
    unittest.main()
