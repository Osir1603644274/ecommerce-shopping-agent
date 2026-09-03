"""Rebuild the disposable Qdrant product catalog from the Spring Boot fact API."""

import argparse

import httpx
from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

from app.domains.ecommerce.models import product_embedding_text
from app.settings import settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", default=settings.backend_base_url)
    parser.add_argument("--qdrant-url", default=settings.qdrant_url)
    parser.add_argument("--collection", default=settings.product_collection_name)
    args = parser.parse_args()
    response = httpx.get(
        f"{args.backend_url}/api/products", params={"limit": 1500}, timeout=30
    )
    response.raise_for_status()
    products = response.json()["data"]
    if not products:
        raise SystemExit("product fact API is empty; import a snapshot first")
    # Keep the vector document aligned with the audited retrieval fields.
    # Repeating the title gives identity/use-case language more influence than
    # brand and raw attribute values without embedding seller/category noise.
    texts = [product_embedding_text(item) for item in products]
    model = TextEmbedding(
        model_name=settings.rag_embedding_model,
        cache_dir=settings.rag_model_cache_dir,
    )
    vectors = [vector.tolist() for vector in model.embed(texts)]
    client = QdrantClient(url=args.qdrant_url)
    client.recreate_collection(
        collection_name=args.collection,
        vectors_config=models.VectorParams(
            size=len(vectors[0]), distance=models.Distance.COSINE
        ),
    )
    client.upsert(
        collection_name=args.collection,
        points=[
            models.PointStruct(
                id=int(product["id"]), vector=vector,
                payload={
                    "productId": int(product["id"]),
                    "categoryL3": product.get("categoryL3"),
                    "datasetRevision": product.get("datasetRevision"),
                    "embeddingFields": ["title", "brand", "attributeText"],
                    "embeddingPolicy": "title_x2_brand_x1_attributeText_x1",
                },
            )
            for product, vector in zip(products, vectors)
        ],
        wait=True,
    )
    print({"collection": args.collection, "count": len(products)})


if __name__ == "__main__":
    main()
