import time

from app.rag import ingest_reviews


if __name__ == "__main__":
    start = time.perf_counter()
    count = ingest_reviews()
    duration_ms = (time.perf_counter() - start) * 1000
    print(
        f"已从 MySQL 获取并写入 {count} 条商户评论到 Qdrant，"
        f"耗时 {duration_ms:.2f}ms。"
    )
