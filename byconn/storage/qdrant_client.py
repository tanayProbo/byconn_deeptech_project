import logging
import uuid
from typing import List, Dict, Any, Optional

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct,
        Filter, FieldCondition, MatchValue
    )
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

logger = logging.getLogger("byconnx.storage.qdrant")


class QdrantAdapter:
    """
    Adapter for Qdrant Vector Database.
    Indexes extracted text chunks with dense embeddings and supports metadata filtering.
    """
    def __init__(self, host: str = "localhost", port: int = 6333):
        self.host = host
        self.port = port
        self.client: Optional["QdrantClient"] = None

    def connect(self):
        """Initializes connection to Qdrant cluster endpoint."""
        if not QDRANT_AVAILABLE:
            raise ImportError("qdrant-client is not installed. Run: pip install qdrant-client")
        self.client = QdrantClient(host=self.host, port=self.port)
        logger.info(f"Connected to Qdrant at {self.host}:{self.port}")

    def create_collection_if_missing(self, collection_name: str, vector_size: int = 1536):
        """Creates collection configured for cosine similarity if it doesn't already exist."""
        if self.client is None:
            self.connect()
        existing = [c.name for c in self.client.get_collections().collections]
        if collection_name not in existing:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
            )
            logger.info(f"Qdrant: Created collection '{collection_name}' (dim={vector_size})")
        else:
            logger.info(f"Qdrant: Collection '{collection_name}' already exists.")

    def upsert_document_chunks(self, collection_name: str, points: List[Dict[str, Any]]):
        """
        Indexes dense vectors and payload to Qdrant.
        Each point must have keys: 'vector' (List[float]) and 'payload' (Dict).
        """
        if self.client is None:
            self.connect()
        qdrant_points = [
            PointStruct(
                id=p.get("id", str(uuid.uuid4())),
                vector=p["vector"],
                payload=p.get("payload", {})
            )
            for p in points
        ]
        self.client.upsert(collection_name=collection_name, points=qdrant_points)
        logger.info(f"Qdrant: Upserted {len(qdrant_points)} points into '{collection_name}'")

    def hybrid_search(
        self,
        collection_name: str,
        dense_vector: List[float],
        filter_metadata: Optional[Dict[str, Any]] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Executes cosine similarity search with optional metadata filter."""
        if self.client is None:
            self.connect()

        query_filter = None
        if filter_metadata:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filter_metadata.items()
            ]
            query_filter = Filter(must=conditions)

        results = self.client.search(
            collection_name=collection_name,
            query_vector=dense_vector,
            query_filter=query_filter,
            limit=limit
        )
        return [
            {"id": str(r.id), "score": r.score, "payload": r.payload}
            for r in results
        ]

    def close(self):
        """Closes the Qdrant client connection."""
        if self.client:
            self.client.close()
            logger.info("Qdrant connection closed.")
