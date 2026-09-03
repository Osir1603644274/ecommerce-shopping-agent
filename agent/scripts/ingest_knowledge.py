import time

from app.knowledge import rebuild_knowledge_index


if __name__ == "__main__":
    start = time.perf_counter()
    summary = rebuild_knowledge_index()
    duration_ms = (time.perf_counter() - start) * 1000
    print(
        f"已写入 {summary['indexedCount']} 个知识块到 "
        f"{summary['collection']}，来源分布={summary['sourceCounts']}，"
        f"耗时 {duration_ms:.2f}ms。"
    )
