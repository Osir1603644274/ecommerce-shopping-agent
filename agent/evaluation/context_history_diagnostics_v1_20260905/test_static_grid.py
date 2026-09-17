import asyncio
import unittest

from .static_grid import c_target_candidate, prefix_archive
from agent.app.context_history import HistoryArchiveError
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import HistoryPolicy, HistoryStrategies, message, tokens


class StaticGridTests(unittest.TestCase):
    def test_prefix_cannot_see_later_archive_messages(self):
        records = [{"messageId": "one", "turn": 1, "content": "old"},
                   {"messageId": "two", "turn": 2, "content": "future"}]
        archive = prefix_archive(records, "one")
        self.assertEqual([r["messageId"] for r in archive.records()], ["one"])
        with self.assertRaises(ValueError):
            prefix_archive(records, "missing")
        archive._records[0]["content"] = "changed in isolated copy"
        self.assertEqual(records[0]["content"], "old")

    def test_initial_empty_history_does_not_read_future_archive(self):
        archive = prefix_archive([{"messageId": "future", "turn": 1}], None)
        self.assertEqual(archive.records(), [])

    def test_recorded_negative_budget_is_not_serialized_wire_estimate(self):
        row = c_target_candidate(32000, .45, 12386, 2545, 14000)
        self.assertEqual(row['summaryTargetTokens'], -531)
        self.assertTrue(row['lessThan128SummaryHeadroom'])
        self.assertFalse(row['protectedLowerBoundExceedsTarget'])

    def test_exact_128_boundary_matches_runtime_before_model(self):
        records = [{'messageId': str(i), 'turn': i + 1, 'role': 'user',
                    'content': ('old ' * 1000) if i == 0 else 'recent'} for i in range(5)]
        archive = prefix_archive(records, '4')
        policy = HistoryPolicy(input_budget=1000, working_budget=1000,
                               trigger_fraction=.6, target_fraction=.45)
        recent_tokens = tokens([message(row) for row in records[-4:]])
        for headroom in (127, 128):
            fixed = 450 - recent_tokens - headroom
            predicted = c_target_candidate(1000, .45, fixed, recent_tokens, 1)
            calls = []
            async def capture(sources, target):
                calls.append(target)
                raise RuntimeError('captured_no_model_call')
            strategy = HistoryStrategies(archive, policy)
            if headroom == 127:
                with self.assertRaisesRegex(HistoryArchiveError, 'protected_context_leaves_no_summary_budget'):
                    asyncio.run(strategy._llm_history(capture, fixed_tokens=fixed))
                self.assertEqual(calls, [])
            else:
                with self.assertRaisesRegex(RuntimeError, 'captured_no_model_call'):
                    asyncio.run(strategy._llm_history(capture, fixed_tokens=fixed))
                self.assertEqual(calls, [128])
            self.assertEqual(predicted['lessThan128SummaryHeadroom'], headroom < 128)

    def test_target_is_floored_before_subtracting_fixed_and_recent(self):
        row = c_target_candidate(1001, .45, 200, 122, 0)
        self.assertEqual(row['summaryTargetTokens'], 128)
        self.assertFalse(row['lessThan128SummaryHeadroom'])


if __name__ == "__main__":
    unittest.main()
