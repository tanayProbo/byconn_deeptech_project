import json
import logging
from typing import Any, Dict

logger = logging.getLogger("byconnx.free_api.schema_detector")

class APISchemaDetector:
    """
    Automatic Schema Detector that parses raw response payloads (JSON lists/dictionaries)
    and constructs strict structural JSON Schema definitions mapping data types.
    """
    def __init__(self):
        pass

    def detect_schema(self, payload: Any) -> Dict[str, Any]:
        """Parses payload inputs and returns structured JSON schema draft configurations."""
        try:
            if isinstance(payload, str):
                data = json.loads(payload)
            else:
                data = payload
        except Exception as e:
            logger.error(f"Failed parsing raw string payload to JSON: {str(e)}")
            return {"type": "string", "description": "Invalid structured response payload."}

        return self._infer_type(data)

    def _infer_type(self, val: Any) -> Dict[str, Any]:
        """Recursively parses types to output schema blocks."""
        if val is None:
            return {"type": "null"}
        elif isinstance(val, bool):
            return {"type": "boolean"}
        elif isinstance(val, int):
            return {"type": "integer"}
        elif isinstance(val, float):
            return {"type": "number"}
        elif isinstance(val, str):
            return {"type": "string"}
        elif isinstance(val, list):
            item_schemas = []
            # Gather schema inputs from list entries (sample first 5 items to limit complexity)
            for item in val[:5]:
                item_schemas.append(self._infer_type(item))

            # Combine items layout schema
            if not item_schemas:
                items_schema = {"type": "string"}
            else:
                # Merge logic: if there is only 1 type or they are uniform
                first = item_schemas[0]
                if all(x == first for x in item_schemas):
                    items_schema = first
                else:
                    items_schema = {"anyOf": item_schemas}
            return {"type": "array", "items": items_schema}
        elif isinstance(val, dict):
            properties = {}
            required = []
            for k, v in val.items():
                properties[k] = self._infer_type(v)
                required.append(k)
            return {
                "type": "object",
                "properties": properties,
                "required": required
            }
        return {"type": "string"}
