import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from . import review_closeout as closeout


class CompleteReviewClosure(unittest.TestCase):
    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.review = self.root / "review"
        self.output = self.root / "closed.json"
        self.samples, self.mapping, self.packets, self.decisions = [], [], [], []
        self.request = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "fixture"}]}
        for index, arm in enumerate(("A_FULL_HISTORY", "B_PACK_VIEW", "C_LLM_THRESHOLD")):
            identifier = f"sample{index}"
            attempt = self.root / f"attempt{index}"
            self.save(attempt / "started.json", {"arm": arm})
            self.mapping.append({"sampleId": identifier, "directory": str(attempt)})
            self.samples.append({"sampleId": identifier, "turns": [
                {"user": "request", "assistant": "answer"} for _ in range(2)]})
            self.packets.append({"sampleId": identifier, "targetTurns": [1, 2]})
            self.decisions.append({"sampleId": identifier, "turns": [1, 2], "disputed": []})
            for number in (1, 2):
                grade = 3 if index == 1 and number == 1 else 2 if index == 2 else 4
                scores = {"scores": [{"sampleId": identifier, "turn": turn,
                    **{dim: grade for dim in closeout.DIMENSIONS}, "rationale": "fixture", "seriousErrors": []}
                    for turn in (1, 2)]}
                stem = f"{identifier}-chunk-01"
                self.save(self.review / f"{stem}-judgment-{number}.json", scores)
                call = self.review / f"{stem}-judge-{number}" / "call-001"
                self.save(call / "request.json", self.request)
                (call / "prompt.txt").write_text("fixture", encoding="utf-8")
                usage = {"input_tokens": 100, "output_tokens": 20}
                thread = f"thread-{index}-{number}"
                events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.completed", "usage": usage}]
                (call / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
                record = {"ordinal": 1, "status": "COMPLETED", "requestSha256": closeout.sha(self.request),
                    "promptSha256": closeout.sha("fixture"), "usage": usage, "threadId": thread,
                    "answer": {"content": json.dumps(scores), "toolCalls": []}}
                self.save(call / "result.json", record)
                (call.parent / "ledger.jsonl").write_text(json.dumps(record), encoding="utf-8")
        self.save(self.review / "started.json", {"kind": "DEVELOPMENT_LOSSLESS_SINGLE_TRAJECTORY_REVIEW_V2",
            "sources": {}, "rubricHash": closeout.sha(closeout.RUBRIC), "turns": 2, "chunkSize": 2})
        self.save(self.review / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", self.mapping)
        self.save(self.review / "packet_audit.json", self.packets)
        self.save(self.review / "result.json", {"status": "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT",
            "decisions": list(reversed(self.decisions))})
        for mocker in (patch.object(closeout, "HERE", self.root),
                patch.object(closeout, "load_samples", return_value=(self.samples, self.mapping)),
                patch.object(closeout, "prepare", side_effect=lambda sample, targets:
                    (sample, self.request, {"sampleId": sample["sampleId"], "targetTurns": targets}))):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_complete_cost_and_score_gate_still_require_semantics(self):
        closeout.run(self.review, self.output)
        result = closeout.read(self.output)
        self.assertEqual(result["completeJudgeTokenTotal"], 720)
        self.assertTrue(result["comparisons"]["B_PACK_VIEW"]["eachDimensionLossAtMostPoint5"])
        self.assertFalse(result["comparisons"]["C_LLM_THRESHOLD"]["eachDimensionLossAtMostPoint5"])
        self.assertFalse(result["qualityAcceptance"])
        self.assertTrue(result["semanticAuditRequired"])

    def test_running_review_cannot_unblind(self):
        (self.review / "result.json").unlink()
        with self.assertRaises(FileNotFoundError):
            closeout.run(self.review, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_mandatory_judge_rejected(self):
        (self.review / "sample0-chunk-01-judgment-2.json").unlink()
        with self.assertRaisesRegex(ValueError, "mandatory_judge_missing"):
            closeout.run(self.review, self.output)

    def test_score_not_equal_native_answer_rejected(self):
        path = self.review / "sample0-chunk-01-judgment-1.json"
        value = closeout.read(path)
        value["scores"][0]["correctness"] = 1
        self.save(path, value)
        with self.assertRaisesRegex(ValueError, "not_native_answer"):
            closeout.run(self.review, self.output)

    def test_packet_drift_rejected(self):
        self.packets[0]["unexpected"] = True
        self.save(self.review / "packet_audit.json", self.packets)
        with self.assertRaisesRegex(ValueError, "packet_not_exactly"):
            closeout.run(self.review, self.output)

    def test_declared_disputes_recomputed(self):
        self.decisions[0]["disputed"] = [["sample0", 1]]
        self.save(self.review / "result.json", {"status": "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT",
            "decisions": self.decisions})
        with self.assertRaisesRegex(ValueError, "disputes_do_not_recompute"):
            closeout.run(self.review, self.output)

    def test_required_third_judge_cannot_be_omitted(self):
        path = self.review / "sample0-chunk-01-judgment-1.json"
        value = closeout.read(path)
        value["scores"][0]["correctness"] = 2
        self.save(path, value)
        call = self.review / "sample0-chunk-01-judge-1/call-001"
        record = closeout.read(call / "result.json")
        record["answer"]["content"] = json.dumps(value)
        self.save(call / "result.json", record)
        (call.parent / "ledger.jsonl").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "third_judge_policy"):
            closeout.run(self.review, self.output)

    def add_failed_call(self, unknown=False):
        original = self.review / "sample0-chunk-01-judge-1"
        extra = self.review / "failed-extra-judge-1"
        shutil.copytree(original, extra)
        call = extra / "call-001"
        record = closeout.read(call / "result.json")
        record.update(status="FAILED", threadId="failed-thread")
        events = [{"type": "thread.started", "thread_id": "failed-thread"}]
        if unknown:
            record["usage"] = None
        else:
            events.append({"type": "turn.completed", "usage": record["usage"]})
        self.save(call / "result.json", record)
        (extra / "ledger.jsonl").write_text(json.dumps(record), encoding="utf-8")
        (call / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")

    def test_unused_failed_call_cost_is_not_dropped(self):
        self.add_failed_call()
        closeout.run(self.review, self.output)
        self.assertEqual(closeout.read(self.output)["completeJudgeTokenTotal"], 840)

    def test_failed_unknown_call_makes_total_unknown(self):
        self.add_failed_call(unknown=True)
        closeout.run(self.review, self.output)
        result = closeout.read(self.output)
        self.assertIsNone(result["completeJudgeTokenTotal"])
        self.assertEqual(result["knownJudgeTokenSubtotal"], 720)

    def test_exact_resume_does_not_double_charge_reused_calls(self):
        resume = self.root / "resume"
        self.save(self.review / "failure.json", {"error": "fixture_interruption"})
        start = closeout.read(self.review / "started.json")
        self.save(resume / "started.json", {**start, "kind": "APPEND_ONLY_EXACT_REVIEW_CONTINUATION",
            "parent": str(self.review), "parentFailureHash": closeout.file_sha(self.review / "failure.json")})
        self.save(resume / "result.json", closeout.read(self.review / "result.json"))
        (self.review / "result.json").unlink()
        self.save(resume / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", self.mapping)
        for source in self.review.glob("*-judgment-*.json"):
            stem, number = source.stem.rsplit("-judgment-", 1)
            request = self.review / f"{stem}-judge-{number}/call-001/request.json"
            self.save(resume / source.name, closeout.read(source))
            self.save(resume / f"{stem}-receipt-{number}.json", {"kind": "EXACT_REUSE",
                "source": str(source), "sourceHash": closeout.file_sha(source),
                "requestSource": str(request), "requestSourceHash": closeout.file_sha(request)})
        closeout.run(resume, self.output)
        result = closeout.read(self.output)
        self.assertEqual(result["completeJudgeTokenTotal"], 720)
        self.assertEqual(len(result["nativeJudgeCosts"]), 6)


if __name__ == "__main__":
    unittest.main()
