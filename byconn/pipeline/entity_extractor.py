import os
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("byconnx.pipeline.entity_extractor")


class EntityExtractor:
    """
    AI Extraction Engine that structures unstructured markdown.
    Extracts entities, assigns topic classification, and outputs relationship triples.
    Requires OPENAI_API_KEY environment variable to be set.
    """
    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self._client = None

    def _get_client(self):
        """Lazily initialises the OpenAI async client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise EnvironmentError(
                        "OPENAI_API_KEY is not set. "
                        "Add it to your .env file or environment variables."
                    )
                self._client = AsyncOpenAI(api_key=api_key)
            except ImportError:
                raise ImportError("openai not installed. Run: pip install openai")
        return self._client

    def build_extraction_prompt(self, text_content: str) -> str:
        return f"""
You are a highly capable AI Knowledge Graph Specialist.
Given the following unstructured text, extract entities, relationships, and metadata.

TEXT:
\"\"\"
{text_content[:4000]}
\"\"\"

Return a valid JSON object matching this schema exactly:
{{
  "entities": [
    {{"name": "Entity Name", "type": "ORGANIZATION|PERSON|PRODUCT|TECHNOLOGY|LOCATION"}}
  ],
  "triples": [
    {{"subject": "Subject Entity", "predicate": "RELATION_TYPE", "object": "Object Entity"}}
  ],
  "topics": ["topic1", "topic2"],
  "summary": "One-line executive summary of the text"
}}
"""

    async def extract_knowledge(self, text_content: str) -> Dict[str, Any]:
        """Sends the constructed prompt to OpenAI and returns structured JSON."""
        prompt = self.build_extraction_prompt(text_content)
        client = self._get_client()

        try:
            logger.info("Requesting structured entity extraction from OpenAI...")
            response = await client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1500
            )
            raw = response.choices[0].message.content
            result = json.loads(raw)
            logger.info(
                f"Extracted {len(result.get('entities', []))} entities, "
                f"{len(result.get('triples', []))} triples."
            )
            return result
        except Exception as e:
            logger.error(f"Entity extraction failed: {str(e)}")
            return {"entities": [], "triples": [], "topics": [], "summary": ""}

    async def extract_batch(self, texts: List[str]) -> List[Dict[str, Any]]:
        """Runs extraction over a list of text chunks concurrently."""
        import asyncio
        tasks = [self.extract_knowledge(t) for t in texts]
        return await asyncio.gather(*tasks)
