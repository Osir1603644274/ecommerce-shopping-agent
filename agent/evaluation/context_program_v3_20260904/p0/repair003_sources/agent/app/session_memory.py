import json
import time

import redis.asyncio as redis

from .settings import settings

# 一个会话最多保留多少轮（一轮 = 这次用户消息 + 助手回答，中间可能还夹着工具消息）
MAX_SESSION_TURNS = 10
# Redis 中最多保留多少个会话，超过后淘汰最久未活动的会话。
MAX_SESSIONS = 1000
# 一个会话最多保留多少个任务的独立对话历史。
MAX_TASK_HISTORIES_PER_SESSION = 10
# 会话多久没人说话就自动过期（秒）。这里设 1 小时；活跃会话每次保存都会重新续期。
SESSION_TTL_SECONDS = 60 * 60
_SESSION_INDEX_KEY = "session:index"

# 懒加载的 Redis 客户端：第一次真正用到时才创建，方便测试时替换成“假 Redis”。
_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        # decode_responses=True 让 Redis 直接返回字符串而不是字节，省去手动 decode。
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _key(session_id: str, task_id: str | None = None) -> str:
    # task_id 为空时返回旧版 key，既保留原 API，也用于升级时迁移旧历史。
    if task_id is None:
        return f"session:{session_id}"
    return f"session:{session_id}:task:{task_id}"


def _task_index_key(session_id: str) -> str:
    return f"session:{session_id}:task-index"


async def _register_task_history(
    client: redis.Redis,
    session_id: str,
    task_id: str,
) -> None:
    """记录该会话有哪些任务历史，并淘汰最久未使用的任务历史。"""
    index_key = _task_index_key(session_id)
    await client.zadd(index_key, {task_id: time.time_ns()})
    expired_task_ids = await client.zrange(
        index_key,
        0,
        -MAX_TASK_HISTORIES_PER_SESSION - 1,
    )
    if expired_task_ids:
        await client.delete(
            *[_key(session_id, expired_task_id) for expired_task_id in expired_task_ids]
        )
        await client.zrem(index_key, *expired_task_ids)
    await client.expire(index_key, SESSION_TTL_SECONDS)


async def _delete_session_histories(
    client: redis.Redis,
    session_id: str,
) -> None:
    """删除旧版历史、全部任务历史及任务历史索引。"""
    index_key = _task_index_key(session_id)
    task_ids = await client.zrange(index_key, 0, -1)
    keys = [_key(session_id), index_key]
    keys.extend(_key(session_id, task_id) for task_id in task_ids)
    await client.delete(*keys)


async def get_history(
    session_id: str,
    task_id: str | None = None,
    *,
    migrate_legacy: bool = False,
) -> list[dict]:
    """读出指定任务的历史；不传 task_id 时兼容旧的会话级调用。"""
    client = _get_client()
    key = _key(session_id, task_id)
    raw_turns = await client.lrange(key, 0, -1)
    if not raw_turns and task_id is not None and migrate_legacy:
        # 升级兼容：旧版本只有 session 级历史。仅由调用方确认适合时，
        # 将它一次性归入当前任务，避免之后继续跨任务共享。
        legacy_key = _key(session_id)
        raw_turns = await client.lrange(legacy_key, 0, -1)
        if raw_turns:
            for turn_json in raw_turns[-MAX_SESSION_TURNS:]:
                await client.rpush(key, turn_json)
            await client.expire(key, SESSION_TTL_SECONDS)
            await client.delete(legacy_key)
            await _register_task_history(client, session_id, task_id)
    if not raw_turns:
        index_key = _task_index_key(session_id)
        if task_id is not None:
            # 任务历史可能已经因 TTL 过期；清掉任务索引中的残留项。
            await client.zrem(index_key, task_id)
        task_ids = await client.zrange(index_key, 0, -1)
        legacy_turns = (
            await client.lrange(_key(session_id), 0, -1)
            if task_id is not None
            else []
        )
        if not task_ids and not legacy_turns:
            await client.zrem(_SESSION_INDEX_KEY, session_id)
        return []
    # raw_turns 是一串 JSON 字符串，每个字符串是一轮（轮里又是若干条消息）。
    # 先把每一轮 json.loads 还原成列表，再拍平成一个大列表。
    return [message for turn_json in raw_turns for message in json.loads(turn_json)]


async def save_turn(
    session_id: str,
    messages: list[dict],
    task_id: str | None = None,
) -> None:
    """把一轮对话写入指定任务；不传 task_id 时兼容旧版调用。"""
    key = _key(session_id, task_id)
    client = _get_client()
    # 1) 把这一轮序列化成 JSON 字符串，push 到列表尾部
    await client.rpush(key, json.dumps(messages, ensure_ascii=False))
    # 2) 只保留最后 MAX_SESSION_TURNS 轮（下标 -N 到 -1 表示“倒数第 N 个到最后一个”）
    await client.ltrim(key, -MAX_SESSION_TURNS, -1)
    # 3) 续期：从现在起再活 SESSION_TTL_SECONDS 秒，闲置到期后 Redis 自动删除
    await client.expire(key, SESSION_TTL_SECONDS)
    if task_id is not None:
        await _register_task_history(client, session_id, task_id)
    # 4) 用有序集合记录最后活动时间，淘汰超过总数上限的最旧会话。
    await client.zadd(_SESSION_INDEX_KEY, {session_id: time.time_ns()})
    expired_session_ids = await client.zrange(_SESSION_INDEX_KEY, 0, -MAX_SESSIONS - 1)
    if expired_session_ids:
        for expired_session_id in expired_session_ids:
            await _delete_session_histories(client, expired_session_id)
        await client.zrem(_SESSION_INDEX_KEY, *expired_session_ids)


async def clear_session(session_id: str) -> None:
    """删除某个会话（比如用户点“开启新对话”）。"""
    client = _get_client()
    await _delete_session_histories(client, session_id)
    await client.zrem(_SESSION_INDEX_KEY, session_id)


async def clear_all_sessions() -> None:
    """清空当前 Redis 库的所有数据，主要给测试用（每个用例开始前重置环境）。"""
    await _get_client().flushdb()
