"""Small deterministic regressions before the single first-edition cohort."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, canonical
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import HistoryPolicy, HistoryStrategies, tokens
from .static_grid import captured_archive, prefix_archive
from agent.evaluation.context_history_review_v2_20260905.run import independent_pair
from . import supervised_review, serial_long_cohort
from agent.app.schemas import ToolTrace


class FinalRouteTests(unittest.TestCase):
    def test_presentation_control_cannot_swallow_refresh_or_semantic_change(self):
        with patch.object(settings, 'context_history_v1_enabled', True):
            for query in ('请按当前条件重新检索并展示前三款手机，不足三款就按实际数量。',
                          '预算改成2000元，展示前三款', '展示前三款，并解释之前的验机安排'):
                self.assertFalse(llm._used_phone_presentation_only_request(query), query)
            self.assertTrue(llm._used_phone_presentation_only_request('请只展示前三款。'))

    def test_recorded_update_and_nonproduct_listing_need_model(self):
        with patch.object(settings, 'context_history_v1_enabled', True):
            for turn in (37, 68):
                row = json.loads((HERE / f'core72_v7_general_cohort001/A001/turn-{turn:02}.json').read_text(encoding='utf8'))
                self.assertTrue(llm._requires_contextual_final_answer(row['query']), turn)

    def test_stale_evidence_not_a_current_listing(self):
        state = SimpleNamespace(task_type='ecommerce_guide')
        class StaleView:
            answer_category = 'phone'
            @property
            def validated_results(self):
                raise AssertionError('historical_products_must_not_enable_current_template')
        view = StaleView()
        with patch.object(settings, 'context_history_v1_enabled', True):
            self.assertIsNone(llm._render_validated_used_phone_answer(state, view, user_message='请展示前三款'))
            for trace in (ToolTrace(tool='search_products', ok=False), ToolTrace(tool='history_lookup', ok=True)):
                self.assertIsNone(llm._render_validated_used_phone_answer(state, view,
                    user_message='请展示前三款', current_tool_traces=[trace]))
            with self.assertRaisesRegex(AssertionError, 'historical_products'):
                llm._render_validated_used_phone_answer(state, view, user_message='请展示前三款',
                    current_tool_traces=[ToolTrace(tool='search_products', ok=True)])

    def test_mixed_negative_historical_and_nonproduct_requests(self):
        with patch.object(settings, 'context_history_v1_enabled', True):
            for query in ('不要筛选，只调整取舍', '列出仅现场核实的信息', '请回忆上次推荐的原因',
                          '请重新检索并解释如何验机', '记录：以后再检索'):
                self.assertTrue(llm._requires_contextual_final_answer(query), query)
            self.assertFalse(llm._requires_contextual_final_answer('请重新检索并展示前三款'))
        with patch.object(settings, 'context_history_v1_enabled', False):
            self.assertFalse(llm._requires_contextual_final_answer('不要筛选，只调整取舍'))


class HistoryRetentionTests(unittest.TestCase):
    def test_recorded_general72_budget_restoration_retained_verbatim(self):
        root = HERE / 'core72_v7_general_cohort001/B001'
        request = json.loads((root / 'model_calls/call-178/request.json').read_text(encoding='utf8'))
        boundary = []
        for i, item in enumerate(request['messages']):
            try:
                value = json.loads(item.get('content') or '')
            except ValueError:
                continue
            if isinstance(value, dict) and 'history' in value:
                boundary.append((i, value))
        self.assertEqual(len(boundary), 1)
        i, payload = boundary[0]
        rows, _ = captured_archive(root / 'archive')
        archive = prefix_archive(rows, payload['history']['messages'][-1]['messageId'])
        request['messages'][i]['content'] = canonical({k: v for k, v in payload.items() if k != 'history'})
        selected = HistoryStrategies(archive, HistoryPolicy(input_budget=96000, working_budget=24000,
            fill_history_budget=True)).pack_history(payload['currentUserMessage'], fixed_tokens=tokens(request))
        original = next(r for r in archive.records() if r['turn'] == 19 and r['role'] == 'user')
        found = [r for r in selected['messages'] if r['messageId'] == original['messageId']]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['content'], original['content'])


class IndependentJudgeTests(unittest.IsolatedAsyncioTestCase):
    def test_parallel_argument_is_explicit_not_a_default_change(self):
        args = supervised_review.worker_arguments('x', ['A', 'B', 'C'], 48, 192000, 2)
        self.assertEqual(args[args.index('--judge-concurrency') + 1], '2')
        for bad in (True, 0, 3, '2'):
            with self.assertRaises(ValueError):
                supervised_review.worker_arguments('x', ['A', 'B', 'C'], 48, 192000, bad)

    def test_frozen_first_edition_schedule(self):
        _, _, turns, order, jobs = serial_long_cohort.schedule(HERE / 'NEVER_STARTED', 'firstedition48')
        self.assertEqual((turns, order), (48, 'ABC'))
        self.assertIn('24000', jobs[1]['arguments'])
        self.assertIn('.55', jobs[2]['arguments'])
        self.assertNotIn('--working-budget', jobs[0]['arguments'])

    async def test_bad_quote_waits_for_successful_sibling(self):
        completed = []
        async def bad():
            raise ValueError('bad_quote')
        async def good():
            await asyncio.sleep(.01)
            completed.append('saved')
            return {'score': 4}
        with self.assertRaisesRegex(ValueError, 'bad_quote'):
            await independent_pair(bad(), good())
        self.assertEqual(completed, ['saved'])

    async def test_success_order_preserved(self):
        async def result(value):
            return value
        self.assertEqual(await independent_pair(result(1), result(2)), [1, 2])

    async def test_external_stop_cancels_both(self):
        started, ended = [], []
        ready = asyncio.Event()
        async def waiting(number):
            started.append(number)
            if len(started) == 2:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.append(number)
        task = asyncio.create_task(independent_pair(waiting(1), waiting(2)))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(ended), [1, 2])


if __name__ == '__main__':
    unittest.main()
