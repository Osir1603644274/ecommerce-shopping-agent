"""Meaningful leakage, cleaning, candidate and evaluation-denominator tests."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from amazon_pilot import (
    ARMS, SOURCE, boundaries, check_public, clean_metadata, clean_reviews,
    dev_requests, evaluate, partition, predict_files, write_json, write_rows,
)


class AmazonPilotTests(unittest.TestCase):
    def source_file(self, root, name, rows):
        path = Path(root) / name
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        return path

    def test_duplicates_conflicting_ratings_and_cross_time_events(self):
        row = {"reviewerID": "u", "asin": "a", "overall": 4, "unixReviewTime": 1, "reviewText": "text"}
        records = [row, dict(row), {**row, "reviewText": "another"}, {**row, "unixReviewTime": 2},
                   {**row, "asin": "b"}, {**row, "asin": "b", "overall": 1}]
        with tempfile.TemporaryDirectory() as root:
            events, audit, quarantine = clean_reviews(self.source_file(root, "reviews.gz", records))
        self.assertEqual([(r["item_id"], r["timestamp"]) for r in events], [("a", 1), ("a", 2)])
        self.assertEqual(audit["exact_duplicate_rows"], 1)
        self.assertEqual(audit["same_rating_nonidentical_rows_collapsed"], 1)
        self.assertEqual(audit["rating_conflict_raw_rows"], 2)
        self.assertEqual(quarantine[0]["ratings"], [1.0, 4.0])
        self.assertTrue(all("reviewText" not in event and event["event_kind"] == "review" for event in events))

    def test_metadata_any_full_record_conflict_quarantines_id(self):
        row = {"asin": "a", "title": "Title", "rank": "1"}
        records = [row, dict(row), {"asin": "b", "title": "Other", "rank": "1"},
                   {"asin": "b", "title": "Other", "rank": "2"}, {"asin": "c", "title": ""}]
        with tempfile.TemporaryDirectory() as root:
            catalog, audit, quarantine, excluded = clean_metadata(self.source_file(root, "metadata.gz", records))
        self.assertEqual([r["item_id"] for r in catalog], ["a"])
        self.assertEqual(audit["exact_duplicate_rows"], 1)
        self.assertEqual(excluded, {"b": "conflicting_metadata", "c": "missing_title"})
        self.assertEqual(len(quarantine), 1)
        self.assertNotIn("rank", catalog[0])

    def test_global_partition_never_splits_equal_time(self):
        events = [{"timestamp": timestamp} for timestamp in [1] * 7 + [2] * 2 + [3]]
        threshold = boundaries(events)
        self.assertEqual(threshold["fit_last_inclusive"], 1)
        self.assertEqual(threshold["dev_last_inclusive"], 2)
        self.assertEqual([partition(t, threshold) for t in (1, 2, 3)], ["fit", "dev", "final"])

    def request(self, request_id="r", user="u"):
        return {"request_id": request_id, "source": SOURCE, "user_namespace": SOURCE, "user_id": user,
                "cutoff_timestamp": 10, "seen_all_fit_item_ids": ["a"],
                "history": [{"item_id": "a", "timestamp": 1, "event_kind": "review", "timestamp_kind": "unix_seconds"}]}

    def fit(self):
        return {"user_positive_items": {"u": ["a"], "v": ["a", "b"]},
                "user_reviewed_items": {"u": ["a"], "v": ["a", "b"]},
                "positive_item_user_counts": {"a": 2, "b": 1}, "fit_last_inclusive": 5}

    def test_equal_timestamp_history_and_embedded_target_are_rejected(self):
        request = self.request()
        request["history"][0]["timestamp"] = 10
        with self.assertRaisesRegex(ValueError, "strictly earlier"):
            check_public(request, self.fit())
        request = {**self.request(), "target": "b"}
        with self.assertRaisesRegex(ValueError, "forbidden"):
            check_public(request, self.fit())

    def test_prediction_runs_with_no_label_file_and_ignores_private_target(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            write_rows(out / "catalog.jsonl", [{"item_id": "a", "title": "beauty cream"}, {"item_id": "b", "title": "beauty cream"}])
            write_json(out / "fit_artifacts.json", self.fit())
            write_rows(out / "public_histories.jsonl", [self.request()])
            predict_files(out)
            before = (out / "predictions.jsonl").read_bytes()
            # A malformed private label cannot affect the independent predictor.
            (out / "labels.private.jsonl").write_text("not valid JSON", encoding="utf-8")
            predict_files(out)
            self.assertEqual(before, (out / "predictions.jsonl").read_bytes())

    def test_unretrieved_and_out_of_catalog_targets_remain_in_denominator(self):
        public = [self.request("r1"), self.request("r2")]
        labels = [{"request_id": "r1", "target_item_ids": ["b"]}, {"request_id": "r2", "target_item_ids": ["outside"]}]
        predictions = [{"request_id": request["request_id"], "source": SOURCE, "arm": arm, "item_ids": ["b"]}
                       for request in public for arm in ARMS]
        _, result = evaluate(predictions, public, labels, self.fit(), [{"item_id": "a"}, {"item_id": "b"}])
        self.assertEqual(result["eligible_dev_users"], 2)
        self.assertEqual(result["targets_outside_catalog"], 1)
        for arm in ARMS:
            self.assertEqual(result["metrics"][arm]["all"]["recall_at_100"], .5)
            self.assertEqual(result["metrics"][arm]["cold_item"]["users"], 1)

    def test_all_eligible_users_selected_and_fit_only_history(self):
        events = []
        for user in ("u", "v"):
            for item, timestamp in (("a", 1), ("b", 6), ("c", 6), ("d", 9)):
                events.append({"user_id": user, "item_id": item, "timestamp": timestamp, "rating": 4,
                               "event_kind": "review", "timestamp_kind": "unix_seconds"})
        public, labels, _, _ = dev_requests(events, {"fit_last_inclusive": 5, "dev_last_inclusive": 8})
        self.assertEqual(len(public), 2)
        self.assertEqual([row["target_item_ids"] for row in labels], [["b", "c"], ["b", "c"]])
        self.assertTrue(all([event["item_id"] for event in row["history"]] == ["a"] for row in public))

    def test_low_rating_fit_items_are_not_new_targets(self):
        events = [{"user_id": "u", "item_id": item, "timestamp": timestamp, "rating": rating,
                   "event_kind": "review", "timestamp_kind": "unix_seconds"}
                  for item, timestamp, rating in (("good", 1, 5), ("bad", 2, 1), ("bad", 6, 5), ("new", 7, 4))]
        public, labels, _, _ = dev_requests(events, {"fit_last_inclusive": 5, "dev_last_inclusive": 8})
        self.assertEqual(public[0]["seen_all_fit_item_ids"], ["bad", "good"])
        self.assertEqual([event["item_id"] for event in public[0]["history"]], ["good"])
        self.assertEqual(labels[0]["target_item_ids"], ["new"])


if __name__ == "__main__":
    unittest.main()
