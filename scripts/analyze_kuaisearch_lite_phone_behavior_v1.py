#!/usr/bin/env python3
"""Exact KuaiSearch-Lite item/recall phone-behavior feasibility audit.

The 16.7 GB rank_lite file is not downloaded.  A frozen random byte-range
diagnostic checks its recent-behavior schema without representing full rank.
All source files are pinned to the immutable Hugging Face dataset revision and
verified against their LFS SHA-256 values.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import random
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
BASE_URL = f"https://huggingface.co/datasets/benchen4395/KuaiSearch/resolve/{REVISION}"
SOURCES = {
    "items_lite": {
        "path": "items_lite/train.jsonl",
        "bytes": 2_789_868_956,
        "sha256": "5c04e031324a37636afb2f822a4d862378d7f54eada93875d686fa8f31bd2621",
    },
    "recall_lite": {
        "path": "recall_lite/train.jsonl",
        "bytes": 257_794_992,
        "sha256": "8949ed6b5cf685bb69067710ac70da12fd7999f66e66bafea4b9d8d78bfefe8a",
    },
    "rank_lite": {
        "path": "rank_lite/train.jsonl",
        "bytes": 16_749_137_750,
        "sha256": "5687c98a382a5e4b32a9132117e46b5c7ed452c8db86f48e185100b106a43221",
    },
}

# Cohort contract frozen before behavior inspection.
# Broad deliberately includes accessories so its pollution can be measured.
STRICT_LEAF_NAMES = {"手机", "手机设备", "二手手机", "智能手机", "功能手机"}
ACCESSORY_SIGNALS = {
    "配件", "保护套", "手机壳", "贴膜", "钢化膜", "充电", "数据线", "支架", "挂绳",
    "维修", "零件", "电池", "屏幕", "镜头", "耳机", "手表", "卡套", "存储卡",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_category(names: tuple[str, str, str]) -> tuple[bool, bool]:
    # L1 is the mixed bucket "手机/数码/电脑办公" and cannot define a phone
    # cohort by itself.  Broad phone uses L2/L3 only and intentionally retains
    # phone accessories for a strict-vs-broad comparison.
    broad = any("手机" in name for name in names[1:])
    leaf = names[2] if names[2] and names[2] != "UNKNOWN" else names[1]
    # Parent buckets such as "手机及配件" contain the word 配件 even when the
    # leaf is the phone-body class "手机设备".  Accessory exclusion therefore
    # applies to the leaf only; applying it to the full path undercounts phones.
    strict = leaf in STRICT_LEAF_NAMES and not any(signal in leaf for signal in ACCESSORY_SIGNALS)
    return strict, broad


class PeakSampler:
    def __init__(self) -> None:
        self.peak_rss = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            import psutil  # type: ignore
            self._process = psutil.Process()
        except Exception:
            self._process = None

    def start(self) -> None:
        if self._process is None:
            return
        def sample() -> None:
            while not self._stop.wait(0.25):
                self.peak_rss = max(self.peak_rss, int(self._process.memory_info().rss))
        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._process is not None:
            self.peak_rss = max(self.peak_rss, int(self._process.memory_info().rss))


def stream_jsonl(
    source: dict[str, Any],
    consume: Callable[[dict[str, Any], int], None],
) -> dict[str, Any]:
    url = f"{BASE_URL}/{source['path']}"
    started = time.perf_counter()
    digest = hashlib.sha256()
    byte_count = 0
    row_count = 0
    buffer = b""
    with requests.get(url, stream=True, timeout=(30, 600), verify=False) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
            if not chunk:
                continue
            digest.update(chunk)
            byte_count += len(chunk)
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop()
            for raw in lines:
                if not raw:
                    continue
                row_count += 1
                consume(json.loads(raw), row_count)
        if buffer:
            row_count += 1
            consume(json.loads(buffer), row_count)
    actual_hash = digest.hexdigest()
    if byte_count != source["bytes"] or actual_hash != source["sha256"]:
        raise RuntimeError(
            f"source verification failed for {source['path']}: "
            f"bytes={byte_count} sha256={actual_hash}"
        )
    return {
        "path": source["path"],
        "url": url,
        "expectedBytes": source["bytes"],
        "actualBytes": byte_count,
        "expectedSha256": source["sha256"],
        "actualSha256": actual_hash,
        "rows": row_count,
        "wallClockSeconds": round(time.perf_counter() - started, 3),
        "verification": "PASS",
    }


def flag_at(flags: bytearray, item_id: int) -> int:
    return flags[item_id] if 0 <= item_id < len(flags) else 0


def percentile(values: list[int], probability: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return int(ordered[index])


def distribution(values: list[int]) -> dict[str, Any]:
    bins = {"0": 0, "1": 0, "2": 0, "3-4": 0, "5-9": 0, "10+": 0}
    for value in values:
        if value == 0:
            bins["0"] += 1
        elif value == 1:
            bins["1"] += 1
        elif value == 2:
            bins["2"] += 1
        elif value < 5:
            bins["3-4"] += 1
        elif value < 10:
            bins["5-9"] += 1
        else:
            bins["10+"] += 1
    return {
        "population": len(values),
        "bins": bins,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "mean": round(statistics.fmean(values), 6) if values else None,
    }


def history_future_eligibility(
    timelines: dict[int, list[tuple[int, int, int, int]]],
    event_index: int,
) -> dict[str, int]:
    thresholds = (1, 2, 3, 5, 10)
    result = {str(n): 0 for n in thresholds}
    for rows in timelines.values():
        by_time: dict[int, int] = collections.defaultdict(int)
        for row in rows:
            by_time[row[0]] += row[event_index]
        history = 0
        qualified = set()
        for time_index in sorted(by_time):
            current = by_time[time_index]
            if current > 0:
                for n in thresholds:
                    if history >= n:
                        qualified.add(n)
            history += current
        for n in qualified:
            result[str(n)] += 1
    return result


def wilson(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, centre - half), 8), round(min(1.0, centre + half), 8)]


def sample_rank_ranges(flags: bytearray, blocks: int, block_bytes: int, seed: int) -> dict[str, Any]:
    source = SOURCES["rank_lite"]
    rng = random.Random(seed)
    offsets = sorted(rng.randrange(1, source["bytes"] - block_bytes - 1) for _ in range(blocks))
    seen_sessions: set[tuple[int, int]] = set()
    parsed_rows = 0
    joined_recent_clicks = phone_recent_clicks = 0
    joined_recent_purchases = phone_recent_purchases = 0
    missing_recent_clicks = missing_recent_purchases = 0
    status_counts: collections.Counter[int] = collections.Counter()
    for offset in offsets:
        headers = {"Range": f"bytes={offset}-{offset + block_bytes - 1}"}
        url = f"{BASE_URL}/{source['path']}"
        with requests.get(url, headers=headers, timeout=(30, 180), verify=False) as response:
            status_counts[response.status_code] += 1
            if response.status_code != 206 or "Content-Range" not in response.headers:
                raise RuntimeError(f"rank range request not honored at {offset}: {response.status_code}")
            raw = response.content
        parts = raw.split(b"\n")[1:-1]
        for line in parts:
            if not line:
                continue
            row = json.loads(line)
            parsed_rows += 1
            key = (int(row["user_id"]), int(row["session_id"]))
            if key in seen_sessions:
                continue
            seen_sessions.add(key)
            for item_id in row.get("recently_clicked_item_ids", []):
                flag = flag_at(flags, int(item_id))
                if flag:
                    joined_recent_clicks += 1
                    phone_recent_clicks += int(flag == 3)
                else:
                    missing_recent_clicks += 1
            for item_id in row.get("recently_purchased_item_ids", []):
                flag = flag_at(flags, int(item_id))
                if flag:
                    joined_recent_purchases += 1
                    phone_recent_purchases += int(flag == 3)
                else:
                    missing_recent_purchases += 1
    return {
        "scope": "diagnostic candidate-row-weighted random byte-range sample; not full rank_lite",
        "seed": seed,
        "blocks": blocks,
        "blockBytes": block_bytes,
        "sampledBytes": blocks * block_bytes,
        "fileBytes": source["bytes"],
        "sampleByteShare": round(blocks * block_bytes / source["bytes"], 8),
        "httpStatusCounts": {str(k): v for k, v in sorted(status_counts.items())},
        "parsedCandidateRows": parsed_rows,
        "uniqueSessionsObserved": len(seen_sessions),
        "recentClicked": {
            "joined": joined_recent_clicks,
            "missing": missing_recent_clicks,
            "strictPhone": phone_recent_clicks,
            "strictPhoneShareOfJoined": round(phone_recent_clicks / joined_recent_clicks, 8) if joined_recent_clicks else None,
            "naiveWilson95": wilson(phone_recent_clicks, joined_recent_clicks),
        },
        "recentPurchased": {
            "joined": joined_recent_purchases,
            "missing": missing_recent_purchases,
            "strictPhone": phone_recent_purchases,
            "strictPhoneShareOfJoined": round(phone_recent_purchases / joined_recent_purchases, 8) if joined_recent_purchases else None,
            "naiveWilson95": wilson(phone_recent_purchases, joined_recent_purchases),
        },
        "inferenceBoundary": "Wilson intervals treat item references as independent and are diagnostic only; range sampling is not a user-uniform estimate.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rank-blocks", type=int, default=64)
    parser.add_argument("--rank-block-bytes", type=int, default=524_288)
    parser.add_argument("--seed", type=int, default=20_260_829)
    args = parser.parse_args()

    started_at = utc_now()
    sampler = PeakSampler()
    sampler.start()
    flags = bytearray(1)
    category_counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
    strict_items = broad_items = 0

    def consume_item(row: dict[str, Any], _: int) -> None:
        nonlocal strict_items, broad_items, flags
        item_id = int(row["item_id"])
        if item_id >= len(flags):
            flags.extend(b"\0" * (item_id + 1 - len(flags)))
        names = (
            str(row.get("category_level1_name") or ""),
            str(row.get("category_level2_name") or ""),
            str(row.get("category_level3_name") or ""),
        )
        category_counts[names] += 1
        strict, broad = classify_category(names)
        flags[item_id] = 3 if strict else 2 if broad else 1
        strict_items += int(strict)
        broad_items += int(broad)

    item_receipt = stream_jsonl(SOURCES["items_lite"], consume_item)

    users: dict[int, dict[str, Any]] = {}
    session_ids: set[int] = set()
    timelines: dict[int, list[tuple[int, int, int, int]]] = collections.defaultdict(list)
    totals: collections.Counter[str] = collections.Counter()

    def consume_recall(row: dict[str, Any], _: int) -> None:
        user_id = int(row["user_id"])
        session_id = int(row["session_id"])
        time_index = int(row["time_index"])
        session_ids.add(session_id)
        state = users.setdefault(user_id, collections.defaultdict(int))
        strict_click = strict_purchase = broad_click = broad_purchase = 0
        has_strict_impression = has_broad_impression = False
        for item_id in row.get("impressed_item_ids", []):
            flag = flag_at(flags, int(item_id))
            totals["impressionRefs"] += 1
            totals["impressionJoined"] += int(flag > 0)
            totals["strictPhoneImpressions"] += int(flag == 3)
            totals["broadPhoneImpressions"] += int(flag >= 2)
            has_strict_impression = has_strict_impression or flag == 3
            has_broad_impression = has_broad_impression or flag >= 2
        for item_id in row.get("clicked_item_ids", []):
            flag = flag_at(flags, int(item_id))
            totals["clickRefs"] += 1
            totals["clickJoined"] += int(flag > 0)
            strict_click += int(flag == 3)
            broad_click += int(flag >= 2)
        for item_id in row.get("purchased_item_ids", []):
            flag = flag_at(flags, int(item_id))
            totals["purchaseRefs"] += 1
            totals["purchaseJoined"] += int(flag > 0)
            strict_purchase += int(flag == 3)
            broad_purchase += int(flag >= 2)
        click_count = len(row.get("clicked_item_ids", []))
        purchase_count = len(row.get("purchased_item_ids", []))
        state["sessions"] += 1
        state["totalClicks"] += click_count
        state["totalPurchases"] += purchase_count
        state["strictClicks"] += strict_click
        state["strictPurchases"] += strict_purchase
        state["broadClicks"] += broad_click
        state["broadPurchases"] += broad_purchase
        totals["strictPhoneClicks"] += strict_click
        totals["strictPhonePurchases"] += strict_purchase
        totals["broadPhoneClicks"] += broad_click
        totals["broadPhonePurchases"] += broad_purchase
        totals["strictPhoneExposureSessions"] += int(has_strict_impression)
        totals["broadPhoneExposureSessions"] += int(has_broad_impression)
        totals["strictPhonePositiveSessions"] += int(strict_click + strict_purchase > 0)
        totals["broadPhonePositiveSessions"] += int(broad_click + broad_purchase > 0)
        timelines[user_id].append((time_index, session_id, strict_click, strict_purchase))

    recall_receipt = stream_jsonl(SOURCES["recall_lite"], consume_recall)

    user_states = list(users.values())
    strict_click_values = [int(s["strictClicks"]) for s in user_states]
    strict_purchase_values = [int(s["strictPurchases"]) for s in user_states]
    strict_positive_users = [s for s in user_states if s["strictClicks"] + s["strictPurchases"] > 0]
    broad_positive_users = [s for s in user_states if s["broadClicks"] + s["broadPurchases"] > 0]

    def pollution(population: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
        total_clicks = sum(int(s["totalClicks"]) for s in population)
        total_purchases = sum(int(s["totalPurchases"]) for s in population)
        phone_clicks = sum(int(s[f"{prefix}Clicks"]) for s in population)
        phone_purchases = sum(int(s[f"{prefix}Purchases"]) for s in population)
        return {
            "users": len(population),
            "clickNonPhoneShare": round((total_clicks - phone_clicks) / total_clicks, 8) if total_clicks else None,
            "purchaseNonPhoneShare": round((total_purchases - phone_purchases) / total_purchases, 8) if total_purchases else None,
            "usersWithAnyNonPhonePositiveEvent": sum(
                int(s["totalClicks"] > s[f"{prefix}Clicks"] or s["totalPurchases"] > s[f"{prefix}Purchases"])
                for s in population
            ),
        }

    rank_sample = sample_rank_ranges(flags, args.rank_blocks, args.rank_block_bytes, args.seed)
    sampler.close()

    broad_paths = [
        {"categoryL1": p[0], "categoryL2": p[1], "categoryL3": p[2], "items": count,
         "strict": classify_category(p)[0]}
        for p, count in category_counts.items() if classify_category(p)[1]
    ]
    broad_paths.sort(key=lambda row: (-row["items"], row["categoryL1"], row["categoryL2"], row["categoryL3"]))

    click_eligibility = history_future_eligibility(timelines, 2)
    purchase_eligibility = history_future_eligibility(timelines, 3)
    strict_user_count = len(strict_positive_users)
    # A proposed (not preregistered) feasibility boundary: 300 eligible users
    # permits a user-isolated 60/20/20 pilot with at least 60 validation and 60
    # test users.  This does not establish production effectiveness.
    decision = "ACCEPT" if click_eligibility["3"] >= 300 else "HOLD_INSUFFICIENT_PHONE_BEHAVIOR"
    purchase_decision = "ACCEPT" if purchase_eligibility["3"] >= 300 else "HOLD_INSUFFICIENT_PHONE_BEHAVIOR"

    receipt = {
        "schemaVersion": "kuaisearch-lite-phone-behavior-feasibility-v1",
        "status": decision,
        "scope": "exact items_lite + exact recall_lite; sampled rank_lite diagnostic",
        "dataset": {"id": "benchen4395/KuaiSearch", "revision": REVISION, "variant": "Lite"},
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "sourceReceipts": {"itemsLite": item_receipt, "recallLite": recall_receipt},
        "cohortContract": {
            "frozenBeforeBehaviorScan": True,
            "strictLeafNames": sorted(STRICT_LEAF_NAMES),
            "accessoryExclusionSignals": sorted(ACCESSORY_SIGNALS),
            "strictDefinition": "leaf category exact match and no accessory signal in the leaf; parent bucket text is not an exclusion",
            "broadDefinition": "L2 or L3 contains 手机; mixed L1 手机/数码/电脑办公 alone does not qualify; accessories are included intentionally",
        },
        "catalog": {
            "items": item_receipt["rows"],
            "maxItemId": len(flags) - 1,
            "strictPhoneItems": strict_items,
            "broadPhoneItems": broad_items,
            "broadCategoryPaths": broad_paths,
        },
        "recall": {
            "rows": recall_receipt["rows"],
            "users": len(users),
            "uniqueSessions": len(session_ids),
            "clicks": totals["clickRefs"],
            "purchases": totals["purchaseRefs"],
            "strictPhone": {
                "positiveUsers": strict_user_count,
                "positiveSessions": totals["strictPhonePositiveSessions"],
                "exposureSessions": totals["strictPhoneExposureSessions"],
                "clicks": totals["strictPhoneClicks"],
                "purchases": totals["strictPhonePurchases"],
                "clickDistributionAllUsers": distribution(strict_click_values),
                "purchaseDistributionAllUsers": distribution(strict_purchase_values),
                "clickDistributionPhoneUsers": distribution([int(s["strictClicks"]) for s in strict_positive_users]),
                "purchaseDistributionPhoneUsers": distribution([int(s["strictPurchases"]) for s in strict_positive_users]),
                "historyAtLeastNWithStrictlyLaterClickTargetUsers": click_eligibility,
                "historyAtLeastNWithStrictlyLaterPurchaseTargetUsers": purchase_eligibility,
                "crossCategoryPollutionAmongPhoneUsers": pollution(strict_positive_users, "strict"),
            },
            "broadPhone": {
                "positiveUsers": len(broad_positive_users),
                "positiveSessions": totals["broadPhonePositiveSessions"],
                "exposureSessions": totals["broadPhoneExposureSessions"],
                "clicks": totals["broadPhoneClicks"],
                "purchases": totals["broadPhonePurchases"],
                "crossCategoryPollutionAmongPhoneUsers": pollution(broad_positive_users, "broad"),
            },
            "itemJoinCoverage": {
                "impressions": round(totals["impressionJoined"] / totals["impressionRefs"], 10) if totals["impressionRefs"] else None,
                "clicks": round(totals["clickJoined"] / totals["clickRefs"], 10) if totals["clickRefs"] else None,
                "purchases": round(totals["purchaseJoined"] / totals["purchaseRefs"], 10) if totals["purchaseRefs"] else None,
                "counts": dict(totals),
            },
            "purchaseSparsity": {
                "allPurchasePerClick": round(totals["purchaseRefs"] / totals["clickRefs"], 8) if totals["clickRefs"] else None,
                "strictPhonePurchasePerClick": round(totals["strictPhonePurchases"] / totals["strictPhoneClicks"], 8) if totals["strictPhoneClicks"] else None,
            },
            "timeOrdering": {
                "field": "time_index",
                "strictlyEarlierOnly": True,
                "usersWithAtLeastTwoDistinctTimeIndices": sum(len({r[0] for r in rows}) >= 2 for rows in timelines.values()),
            },
        },
        "rankLiteDiagnostic": rank_sample,
        "decision": {
            "gateNature": "post-scan architecture recommendation, not a preregistered hypothesis gate",
            "descriptiveGate": "click-only pilot ACCEPT when at least 300 users have >=3 strict-phone clicks in strictly earlier time groups and a later strict-phone click target; supports a 60/20/20 user split with >=60 validation and test users",
            "result": decision,
            "clickProfileResult": decision,
            "purchaseProfileResult": purchase_decision,
            "productionMemorySwitchChanged": False,
            "architectureBoundary": "Behavior events may support a low-authority category-scoped profile; they are not explicit preference truth.",
        },
        "resource": {
            "peakProcessRssBytes": sampler.peak_rss or None,
            "measurement": "psutil 250ms process-RSS sampling" if sampler._process is not None else "unavailable",
        },
        "exceptions": [],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": decision,
        "output": str(args.output),
        "strictPhoneUsers": strict_user_count,
        "history3FutureClickUsers": click_eligibility["3"],
        "itemsRows": item_receipt["rows"],
        "recallRows": recall_receipt["rows"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
