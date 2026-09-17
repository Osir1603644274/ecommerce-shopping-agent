"""Pure fixtures and actual SDK/controller interfaces over MockTransport only.

No real API, model, service, corpus search or GPU is called. Temporary synthetic
receipts are removed at test teardown and never count as experimental results.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import run_agent_pairs as runner


PROFILE = {"provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
           "temperature": 0, "max_tokens": 512, "sdk_timeout_seconds": 30, "max_retries": 2,
           "agent_deadline_seconds": 120, "tool_timeout_seconds": 60}


def query_fixture():
    return {f"s1-{short}-dev-{i:016x}": {"query_id": f"s1-{short}-dev-{i:016x}", "source": source,
            "query": f"synthetic query {source} {i}", "query_cohort": "main"} for source, short in (("kuaisearch", "ku"), ("multicpr", "mu")) for i in range(20)}


def lock_fixture():
    arms = {arm: {"method": arm + "method", "profile": "w111", "model_path": "F:/synthetic/" + arm,
                  "model_binding_sha256": "a" * 64} for arm in runner.ARMS}
    lock = {"status": "FINAL_SEARCH_CONFIGURATION_FROZEN", "seed": runner.SEED,
            "final_dev_qrels": {"path": "unused", "sha256": "b" * 64}, "arms": arms, "llm": deepcopy(PROFILE)}
    selection = {"status": "FINAL_SEARCH_SELECTION_FROZEN", "qrels_sha256": "b" * 64,
                 "selected": deepcopy(arms["winner"]), "strongest_old": deepcopy(arms["baseline"])}
    return lock, selection


class PureContractTests(unittest.TestCase):
    def test_twenty_queries_are_first_ten_by_id_per_original_source(self):
        queries = query_fixture()
        result = runner.selected_twenty(dict(reversed(list(queries.items()))))
        self.assertEqual(len(result), 20)
        for source in runner.evaluation.SOURCES:
            ids = [row["query_id"] for row in result if row["source"] == source]
            expected = sorted(q for q, row in queries.items() if row["source"] == source)[:10]
            self.assertEqual(ids, expected)

    def test_deterministic40_adjacent_pairs_alternating_first_arm(self):
        rows = runner.selected_twenty(query_fixture())
        schedule = runner.schedule_queries(rows)
        self.assertEqual(schedule, runner.schedule_queries(list(reversed(rows))))
        self.assertEqual(len(schedule), 40)
        self.assertEqual(len({(r["query_id"], r["arm"]) for r in schedule}), 40)
        for i in range(0, 40, 2):
            self.assertEqual(schedule[i]["query_id"], schedule[i + 1]["query_id"])
            self.assertNotEqual(schedule[i]["arm"], schedule[i + 1]["arm"])
            if i >= 2: self.assertNotEqual(schedule[i]["arm"], schedule[i - 2]["arm"])

    def test_selection_lock_cannot_name_arbitrary_winner_or_change_model_profile(self):
        lock, selection = lock_fixture()
        runner.validate_selection_lock(lock, selection)
        for field, value in (("model_path", "F:/unselected/model"), ("profile", "w211"), ("method", "made-up-winner")):
            altered = deepcopy(lock); altered["arms"]["winner"][field] = value
            with self.assertRaisesRegex(ValueError, "actual final selection"):
                runner.validate_selection_lock(altered, selection)
        altered = deepcopy(lock); altered["llm"]["max_retries"] = 9
        with self.assertRaisesRegex(ValueError, "execution settings"):
            runner.validate_selection_lock(altered, selection)

    def test_inner_inference_hash_cannot_replace_whole_model_binding_hash(self):
        value = {"path": "F:/model", "inference": {"model": "synthetic"}, "inference_sha256": "a" * 64}
        runner.assert_whole_model_binding(runner.canonical_hash(value), value)
        with self.assertRaisesRegex(ValueError, "inner inference"):
            runner.assert_whole_model_binding(value["inference_sha256"], value)

    def test_secrets_redacted_without_dumping_authorization_or_environment(self):
        redactor = runner.Redactor(["synthetic-secret-key"])
        clean = redactor.clean({"authorization": "Bearer synthetic-secret-key", "body": "error mentions synthetic-secret-key",
                               "nested": [{"api_key": "another-key", "content": "original evidence"}]})
        raw = json.dumps(clean)
        self.assertNotIn("synthetic-secret-key", raw)
        self.assertNotIn("another-key", raw)
        self.assertIn("original evidence", raw)

    def test_settings_scope_restores_all_changed_values_even_on_failure(self):
        binding = SimpleNamespace(data_root="F:/corpus", run_id="run", manifest_sha256="a" * 64)
        fields = ("catalog_evidence_enabled", "catalog_evidence_data_root", "catalog_evidence_run_id", "catalog_evidence_manifest_sha256",
                  "catalog_evidence_source", "catalog_evidence_answer_max_tokens", "catalog_evidence_timeout_seconds",
                  "agent_request_deadline_seconds", "deepseek_model", "deepseek_base_url", "model_call_receipts_enabled", "backend_observer_enabled")
        settings = SimpleNamespace(**{name: "before-" + name for name in fields})
        before = deepcopy(vars(settings))
        with self.assertRaises(RuntimeError):
            with runner.settings_scope(settings, PROFILE, binding, "kuaisearch"):
                self.assertEqual(settings.deepseek_model, "deepseek-chat")
                self.assertFalse(settings.backend_observer_enabled)
                raise RuntimeError("synthetic failure")
        self.assertEqual(vars(settings), before)

    def test_latency_uses_actual_per_query_and_retains_failure_denominators(self):
        receipts = []
        for arm in runner.ARMS:
            for i in range(20):
                outcome = "FAILED" if i == 18 else "INTERRUPTED_AMBIGUOUS" if i == 19 else "SUCCEEDED"
                receipts.append({"identity": {"slot": {"arm": arm}}, "outcome": outcome,
                                 "wall_seconds": i + 1 if i != 19 else None,
                                 "provider_results": [{"single_query_search_seconds": (i + 1) * .2}] if i < 19 else []})
        summary = runner.summarize(receipts)["arms"]["baseline"]
        self.assertEqual((summary["slot_count"], summary["succeeded"], summary["failed"], summary["ambiguous"]), (20, 18, 1, 1))
        self.assertEqual(summary["completed_wall"], {"n": 19, "p50_seconds": 10, "p95_seconds": 19})
        self.assertEqual(summary["actual_single_query_search"]["p95_seconds"], 19 * .2)


class ActualRouteFixtureTests(unittest.TestCase):
    def setUp(self):
        self.modules = runner.app_modules()

    def completion(self, query, stage, *, valid=True):
        if stage == "select":
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-synthetic", "type": "function",
                       "function": {"name": "search_catalog_evidence", "arguments": json.dumps({"query": query if valid else "rewritten",
                                     "source": "kuaisearch", "limit": 10})}}]}
        else:
            message = {"role": "assistant", "content": "kuaisearch:1 的原文相关；价格与库存未知。"}
        return {"id": "synthetic-" + stage, "object": "chat.completion", "created": 1, "model": PROFILE["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if stage == "select" else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    def sdk_factory(self, query, *, valid=True, retry=False):
        import httpx
        from openai import AsyncOpenAI
        requests = []
        async def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            if retry and len(requests) == 1:
                return httpx.Response(503, headers={"retry-after-ms": "1", "x-request-id": "synthetic-retry"},
                                      json={"error": {"message": "synthetic-secret-key temporary error"}})
            stage = "select" if body.get("tools") else "answer"
            return httpx.Response(200, json=self.completion(query, stage, valid=valid), headers={"x-request-id": "synthetic-http-id"})
        def factory():
            return AsyncOpenAI(api_key="synthetic-secret-key", base_url="https://api.deepseek.com", max_retries=2, timeout=30.0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=30.0))
        return factory, requests

    def fixture_runtime(self):
        runtime = SimpleNamespace(calls=0, asset_validation_seconds=123.0)
        def provider_factory(binding, *, manifest_path, profile, model_path, on_result):
            async def provider(request):
                runtime.calls += 1
                result = {"hits": [{"source": request.source, "docid": request.source + ":1", "text": "合成商品完整原文，没有核实价格库存。",
                                    "rank": 1, "score": -2.0, "unknown": ["brand"], "provenance": {"synthetic": True, "profile": profile}}],
                          "timings": {"synthetic": True}}
                on_result(request, result)
                return {"binding": request.binding.model_dump(by_alias=True), "query": request.query, "source": request.source, "hits": result["hits"]}
            return provider
        runtime.agent_provider = provider_factory
        return runtime

    def fixture_manifest(self, directory):
        slot = {"slot": 1, "query_id": "s1-ku-dev-0000000000000001", "source": "kuaisearch", "query": "合成检索查询", "arm": "baseline", "pair_position": 1}
        lock, _ = lock_fixture()
        strategy = {"path": str(directory / "strategy.json"), "binding": {"dataRoot": str(directory), "runId": "synthetic-run", "manifestSha256": "a" * 64}}
        manifest = {"lock": lock, "strategies": {"baseline": strategy, "winner": strategy}}
        (directory / "PREPARED.json").write_bytes(runner.encode(manifest))
        return slot, manifest

    def test_real_entry_dispatch_sdk_and_runtime_callback_capture_full_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); slot, manifest = self.fixture_manifest(directory)
            factory, requests = self.sdk_factory(slot["query"])
            runtime = self.fixture_runtime()
            original_model = self.modules.settings.deepseek_model
            with mock.patch.object(runner, "ROOT", directory), mock.patch.object(self.modules.llm, "get_client", factory):
                receipt = asyncio.run(runner.execute_slot(slot, manifest, directory, runtime, self.modules, runner.Redactor(["synthetic-secret-key"])))
                self.assertIs(self.modules.llm.get_client, factory)
            self.assertEqual(self.modules.settings.deepseek_model, original_model)
            self.assertEqual(receipt["outcome"], "SUCCEEDED")
            self.assertEqual(runtime.calls, 1)
            self.assertEqual((len(requests), receipt["http_attempt_count"], len(receipt["model_calls"])), (2, 2, 2))
            self.assertEqual(requests[0]["tool_choice"], "auto")
            self.assertIn("合成商品完整原文", requests[1]["messages"][-1]["content"])
            self.assertGreater(receipt["provider_results"][0]["single_query_search_seconds"], 0)
            self.assertNotIn("synthetic-secret-key", json.dumps(receipt, ensure_ascii=False))
            for path in directory.rglob("*.json"):
                self.assertNotIn("synthetic-secret-key", path.read_text(encoding="utf-8"))

    def test_existing_sdk_retry_is_observed_not_an_extra_agent_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); slot, manifest = self.fixture_manifest(directory)
            factory, requests = self.sdk_factory(slot["query"], retry=True)
            runtime = self.fixture_runtime()
            with mock.patch.object(runner, "ROOT", directory), mock.patch.object(self.modules.llm, "get_client", factory):
                receipt = asyncio.run(runner.execute_slot(slot, manifest, directory, runtime, self.modules, runner.Redactor(["synthetic-secret-key"])))
            self.assertEqual(receipt["outcome"], "SUCCEEDED")
            self.assertEqual((len(requests), receipt["http_attempt_count"], len(receipt["model_calls"]), runtime.calls), (3, 3, 2, 1))
            self.assertFalse(receipt["logical_agent_call_reissued"])

    def test_invalid_model_selection_fails_once_no_scripted_search_or_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); slot, manifest = self.fixture_manifest(directory)
            factory, requests = self.sdk_factory(slot["query"], valid=False)
            runtime = self.fixture_runtime()
            with mock.patch.object(runner, "ROOT", directory), mock.patch.object(self.modules.llm, "get_client", factory):
                first = asyncio.run(runner.execute_slot(slot, manifest, directory, runtime, self.modules, runner.Redactor()))
                second = asyncio.run(runner.execute_slot(slot, manifest, directory, runtime, self.modules, runner.Redactor()))
            self.assertEqual(first, second)
            self.assertEqual((first["outcome"], runtime.calls, len(requests)), ("FAILED", 0, 1))

    def test_started_without_receipt_is_ambiguous_and_never_reissued(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); slot, manifest = self.fixture_manifest(directory)
            attempt = directory / "attempts/slot-001"
            with mock.patch.object(runner, "ROOT", directory):
                runner.write_once(attempt / "STARTED.json", {"slot": slot, "prepared_sha256": runner.evaluation.sha(directory / "PREPARED.json")})
                runner.write_once(attempt / "events/0001-model-request.json", {"synthetic": "request may have been sent"})
                first = asyncio.run(runner.execute_slot(slot, manifest, directory, None, None, runner.Redactor()))
                second = asyncio.run(runner.execute_slot(slot, manifest, directory, None, None, runner.Redactor()))
            self.assertEqual(first, second)
            self.assertEqual(first["outcome"], "INTERRUPTED_AMBIGUOUS")
            self.assertIsNone(first["wall_seconds"])

    def test_frozen_event_tampering_rejected_before_more_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); slot, manifest = self.fixture_manifest(directory)
            factory, requests = self.sdk_factory(slot["query"], valid=False)
            runtime = self.fixture_runtime()
            with mock.patch.object(runner, "ROOT", directory), mock.patch.object(self.modules.llm, "get_client", factory):
                receipt = asyncio.run(runner.execute_slot(slot, manifest, directory, runtime, self.modules, runner.Redactor()))
                event = directory / "attempts/slot-001" / receipt["events"][0]["name"]
                event.write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "event hash mismatch"):
                    asyncio.run(runner.execute_slot(slot, manifest, directory, runtime, self.modules, runner.Redactor()))
            self.assertEqual(len(requests), 1)

    def test_blind_packet_strips_arm_model_profile_paths_and_keeps_original_text(self):
        receipts = []
        for arm in runner.ARMS:
            receipts.append({"identity": {"slot": {"query_id": "q", "query": "original query", "arm": arm}}, "outcome": "SUCCEEDED", "answer": "answer",
                "traces": [{"tool": "search_catalog_evidence", "ok": True, "detail": {"hits": [{"source": "kuaisearch", "docid": "kuaisearch:1", "text": "full original evidence", "rank": 1,
                "unknown": ["inventory"], "provenance": {"model_path": "F:/secret-model", "profile": "w211"}}]}}]})
        packet, mapping = runner.blind_packet(receipts)
        raw = json.dumps(packet)
        for forbidden in ("baseline", "winner", "F:/secret-model", "w211", "model_path"):
            self.assertNotIn(forbidden, raw)
        self.assertIn("full original evidence", raw)
        self.assertEqual({row["arm"] for row in mapping}, set(runner.ARMS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
