from __future__ import annotations

import json


class FakeRedis:
    """满足会话记忆测试所需的最小异步 Redis 接口。"""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.sorted_sets: dict[str, dict[str, int]] = {}
        self.sets: dict[str, set[str]] = {}

    @staticmethod
    def _slice(values: list[str], start: int, end: int) -> list[str]:
        length = len(values)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        if start >= length or start > end:
            return []
        return values[start : end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self._slice(self.lists.get(key, []), start, end)

    async def rpush(self, key: str, value: str) -> int:
        values = self.lists.setdefault(key, [])
        values.append(value)
        return len(values)

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def set(self, key: str, value: str, **_kwargs) -> bool:
        self.strings[key] = value
        return True

    async def eval(self, _script: str, numkeys: int, *args):
        """Implement the TaskState JSON revision CAS used by production Lua."""
        if "PAUSE_REQUEST_CAS" in _script and numkeys == 1 and len(args) == 8:
            key, run_id, thread_id, owner, policy, policy_revision, payload, _ttl = args
            raw = self.strings.get(key)
            if raw is not None:
                current = json.loads(raw)
                matches = (
                    current.get("runId") == run_id
                    and current.get("threadId") == thread_id
                    and current.get("sessionOwnerHash") == owner
                    and current.get("controlPolicy") == policy
                    and current.get("policyRevision") == policy_revision
                )
                if matches:
                    return raw
                if current.get("state") == "pause_requested":
                    self.strings[key] = payload
                    return payload
                return ""
            self.strings[key] = payload
            return payload
        if "PAUSE_TRANSITION_CAS" in _script and numkeys == 1 and len(args) == 4:
            key, expected, payload, _ttl = args
            raw = self.strings.get(key)
            if raw == payload:
                return raw
            if raw != expected:
                return ""
            self.strings[key] = payload
            return payload
        if "PAUSE_CLEAR_CAS" in _script and numkeys == 1 and len(args) == 3:
            key, request_id, expected_state = args
            raw = self.strings.get(key)
            if raw is None:
                return 0
            current = json.loads(raw)
            if current.get("requestId") != request_id or current.get("state") != expected_state:
                return 0
            del self.strings[key]
            return 1
        if numkeys == 2 and len(args) == 6:
            key, side_key, expected_raw, payload, _ttl, side_payload = args
            raw = self.strings.get(key)
            if raw is None:
                return [-1, -1]
            actual = int(json.loads(raw)["revision"])
            expected = int(expected_raw)
            if actual != expected:
                return [0, actual]
            existing = self.strings.get(side_key)
            if existing is not None and existing != side_payload:
                return [-2, actual]
            self.strings[side_key] = side_payload
            self.strings[key] = payload
            return [1, expected + 1]
        if numkeys != 1 or len(args) != 4:
            raise NotImplementedError("FakeRedis supports TaskState CAS eval")
        key, expected_raw, payload, _ttl = args
        raw = self.strings.get(key)
        if raw is None:
            return [-1, -1]
        actual = int(json.loads(raw)["revision"])
        expected = int(expected_raw)
        if actual != expected:
            return [0, actual]
        self.strings[key] = payload
        return [1, expected + 1]

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self._slice(self.lists.get(key, []), start, end)

    async def expire(self, key: str, seconds: int) -> bool:
        return (
            key in self.lists
            or key in self.strings
            or key in self.sorted_sets
            or key in self.sets
        )

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.lists.pop(key, None) is not None)
            deleted += int(self.strings.pop(key, None) is not None)
            deleted += int(self.sorted_sets.pop(key, None) is not None)
            deleted += int(self.sets.pop(key, None) is not None)
        return deleted

    async def flushdb(self) -> None:
        self.lists.clear()
        self.strings.clear()
        self.sorted_sets.clear()
        self.sets.clear()

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        values = self.sorted_sets.setdefault(key, {})
        new_members = sum(member not in values for member in mapping)
        values.update(mapping)
        return new_members

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [member for member, _ in sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: item[1])]
        return self._slice(members, start, end)

    async def zrem(self, key: str, *members: str) -> int:
        values = self.sorted_sets.get(key, {})
        removed = sum(values.pop(member, None) is not None for member in members)
        return removed

    async def sadd(self, key: str, *members: str) -> int:
        values = self.sets.setdefault(key, set())
        size = len(values)
        values.update(members)
        return len(values) - size

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, *members: str) -> int:
        values = self.sets.get(key, set())
        before = len(values)
        values.difference_update(members)
        return before - len(values)
