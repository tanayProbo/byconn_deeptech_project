import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("byconnx.pipeline.embedder")


class DocumentEmbedder:
    """
    Chunks extracted markdown text and generates vector embeddings.
    Uses sentence-transformers for dense vectors (no GPU required for small batches).
    """
    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        chunk_size: int = 500,
        chunk_overlap: int = 50
    ):
        self.model_name = model_name
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._model = None  # Lazy-loaded on first use

    def _get_model(self):
        """Lazily loads the sentence-transformers model."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info(f"Loading embedding model: {self.model_name}")
                self._model = SentenceTransformer(self.model_name)
                logger.info("Embedding model loaded successfully.")
            except ImportError:
                raise ImportError(
                    "sentence-transformers not installed. "
                    "Run: pip install sentence-transformers"
                )
        return self._model

    def split_into_chunks(self, text: str) -> List[str]:
        """Splits long text using a sliding window to preserve context across chunks."""
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunk_words = words[i: i + self.chunk_size]
            chunks.append(" ".join(chunk_words))
            i += self.chunk_size - self.chunk_overlap
        logger.info(f"Split document into {len(chunks)} chunks.")
        return chunks

    async def generate_dense_embeddings(self, chunks: List[str]) -> List[List[float]]:
        """
        Generates dense vector embeddings for each chunk using sentence-transformers.
        Returns a list of float vectors (dim=384 for all-MiniLM-L6-v2).
        """
        try:
            import asyncio
            model = self._get_model()
            logger.info(f"Embedding {len(chunks)} chunks with '{self.model_name}'...")
            # Run the CPU-bound encoding in a thread so we don't block the event loop
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(
                None,
                lambda: model.encode(chunks, show_progress_bar=False).tolist()
            )
            logger.info(f"Generated {len(embeddings)} embeddings (dim={len(embeddings[0]) if embeddings else 0}).")
            return embeddings
        except Exception as e:
            logger.error(f"Embedding generation failed: {str(e)}")
            return []

    def generate_sparse_tokens(self, text: str) -> Dict[str, float]:
        """Generates BM25-style term frequency weights for keyword search."""
        words = text.lower().split()
        total = len(words)
        if total == 0:
            return {}
        freqs: Dict[str, int] = {}
        for word in words:
            freqs[word] = freqs.get(word, 0) + 1
        return {word: round(count / total, 6) for word, count in freqs.items()}

    def embed_dimension(self) -> int:
        """Returns the embedding dimension for the loaded model."""
        return self._get_model().get_sentence_embedding_dimension()
