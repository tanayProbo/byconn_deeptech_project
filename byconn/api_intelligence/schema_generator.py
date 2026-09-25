import json
import genson
from typing import Dict, Any, List
from .proxy_sniffer import DiscoveredEndpoint

class SchemaGenerator:
    """
    Analyzes API payloads and automatically builds JSON Schema and OpenAPI 3.0 definitions.
    Uses GenSON to merge multiple request bodies into generalized schemas.
    """
    def __init__(self, service_title: str = "Discovered API Engine", version: str = "1.0.0"):
        self.service_title = service_title
        self.version = version

    def generate_json_schema(self, payload: Any) -> Dict[str, Any]:
        """Infers JSON schema structure from sample JSON data."""
        builder = genson.SchemaBuilder()
        try:
            if isinstance(payload, str):
                data = json.loads(payload)
            else:
                data = payload
            builder.add_object(data)
            return builder.to_schema()
        except Exception:
            return {"type": "string", "description": "Failed to parse payload structured schema"}

    @staticmethod
    def is_document(endpoint: DiscoveredEndpoint) -> bool:
        """Reports whether a captured request was a page load, not an API call.

        A crawl also records the HTML document itself; including it in a
        generated API spec would misrepresent navigation as an endpoint.
        """
        content_type = (endpoint.content_type or "").lower()
        return content_type.startswith("text/html") or "xml" in content_type

    def generate_openapi_spec(
        self,
        endpoints: List[DiscoveredEndpoint],
        skip_documents: bool = True,
    ) -> Dict[str, Any]:
        """Constructs an OpenAPI 3.0 spec configuration out of crawled endpoints.

        Args:
            endpoints: Captured endpoints from a :class:`ProxySniffer`.
            skip_documents: Drop page-load requests (HTML/XML) so the spec
                describes only the discovered API surface.
        """
        spec: Dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {
                "title": self.service_title,
                "version": self.version,
                "description": "Auto-generated OpenAPI draft from network sniffing"
            },
            "paths": {}
        }

        for ep in endpoints:
            if skip_documents and self.is_document(ep):
                continue
            if ep.path not in spec["paths"]:
                spec["paths"][ep.path] = {}

            method_lower = ep.method.lower()

            # Formulate JSON schemas for Request and Response payloads
            req_schema = None
            if ep.request_payload:
                req_schema = self.generate_json_schema(ep.request_payload)

            resp_schema = None
            if ep.response_payload:
                resp_schema = self.generate_json_schema(ep.response_payload)

            operation: Dict[str, Any] = {
                "summary": f"Discovered {ep.method} endpoint on {ep.host}",
                "responses": {
                    "200": {
                        "description": "Captured successful API response",
                    }
                }
            }

            if req_schema and method_lower in ["post", "put", "patch"]:
                operation["requestBody"] = {
                    "content": {
                        "application/json": {
                            "schema": req_schema
                        }
                    }
                }

            if resp_schema:
                operation["responses"]["200"]["content"] = {
                    "application/json": {
                        "schema": resp_schema
                    }
                }

            spec["paths"][ep.path][method_lower] = operation

        return spec
