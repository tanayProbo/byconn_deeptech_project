"""Knowledge-extraction contract for the BYCONN-X pipeline.

This module defines the prompt and the response schema that every extractor
must satisfy. :class:`~byconn.pipeline.llm_extractor.LLMExtractor` is the
concrete implementation and performs a real provider call; use it rather than
instantiating :class:`EntityExtractor` directly.

For backwards compatibility, ``LLMExtractor`` is also reachable from this
module (resolved lazily to avoid a circular import)::

    from byconn.pipeline.entity_extractor import LLMExtractor
"""

import logging
from typing import Any, Dict

logger = logging.getLogger("byconnx.pipeline.entity_extractor")

# Response contract shared by every extractor implementation.
ENTITY_TYPES = (
    "ORGANIZATION", "PERSON", "PRODUCT", "TECHNOLOGY",
    "LOCATION", "EVENT", "CONCEPT", "ENTITY",
)

EXTRACTION_PROMPT_TEMPLATE = """
You are a highly capable AI Knowledge Graph Specialist.
Given the following unstructured text, extract entities, relationships, and metadata.

TEXT:
\"\"\"
{text_content}
\"\"\"

Return a valid JSON object matching this schema:
{{
  "entities": [
    {{"name": "Entity Name", "type": "ORGANIZATION/PERSON/PRODUCT/TECHNOLOGY"}}
  ],
  "triples": [
    {{"subject": "Subject Entity", "predicate": "RELATION", "object": "Object Entity"}}
  ],
  "topics": ["topic1", "topic2"],
  "summary": "One-line executive summary of text"
}}
"""


class EntityExtractor:
    """Prompt and schema definition for structured knowledge extraction.

    This class is a base contract, not a usable extractor. Subclasses (see
    :class:`~byconn.pipeline.llm_extractor.LLMExtractor`) must implement
    :meth:`extract_knowledge`. Import ``LLMExtractor`` for real extraction.
    """

    def __init__(self, llm_client: Any = None):
        self.llm_client = llm_client

    def build_extraction_prompt(self, text_content: str) -> str:
        """Renders the extraction prompt for the given page text."""
        return EXTRACTION_PROMPT_TEMPLATE.format(text_content=text_content or "")

    def extract_knowledge(self, text_content: str) -> Dict[str, Any]:
        """Not implemented here.

        Raises:
            NotImplementedError: Always. Use
                :class:`~byconn.pipeline.llm_extractor.LLMExtractor`, which
                performs a real OpenAI/Gemini call, or pass a provider client
                and subclass this type.
        """
        raise NotImplementedError(
            "EntityExtractor defines the extraction contract only. Use "
            "byconn.pipeline.llm_extractor.LLMExtractor for real extraction."
        )


def __getattr__(name: str):
    """Resolves ``LLMExtractor`` lazily so this module can be imported first."""
    if name == "LLMExtractor":
        from .llm_extractor import LLMExtractor  # deferred: avoids a cycle

        return LLMExtractor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
