import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("byconnx.api_intel.proxy_sniffer")

class DiscoveredEndpoint:
    """Stores metadata of a captured network transaction."""
    def __init__(self, method: str, url: str):
        self.method = method
        self.url = url
        parsed = urlparse(url)
        self.host = parsed.netloc
        self.path = parsed.path
        self.query_params = parsed.query
        self.request_headers: Dict[str, str] = {}
        self.response_headers: Dict[str, str] = {}
        self.request_payload: Optional[str] = None
        self.response_payload: Optional[str] = None
        self.content_type = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "path": self.path,
            "method": self.method,
            "url": self.url,
            "content_type": self.content_type,
            "sample_request": self.request_payload,
            "sample_response": self.response_payload
        }


class ProxySniffer:
    """
    Sniffs outgoing and incoming HTTP requests executed during browser operations.
    Filters static assets (JS, CSS, images) to isolate actual REST, gRPC, and GraphQL web endpoints.
    """
    def __init__(self):
        self.endpoints: List[DiscoveredEndpoint] = []

    def handle_request(self, request_data: Dict[str, Any]):
        """Callback invoked when browser executes an outbound request."""
        url = request_data.get("url", "")
        method = request_data.get("method", "GET")

        if self._is_static_asset(url):
            return

        endpoint = DiscoveredEndpoint(method, url)
        endpoint.request_headers = request_data.get("headers", {})
        endpoint.request_payload = request_data.get("post_data")

        self.endpoints.append(endpoint)
        logger.debug(f"Intercepted API Call Request: {method} -> {url}")

    def handle_response(self, url: str, response_data: Dict[str, Any]):
        """Callback invoked when browser receives a server response."""
        if self._is_static_asset(url):
            return

        # Find matching endpoint in queue
        for ep in reversed(self.endpoints):
            if ep.url == url:
                ep.response_headers = response_data.get("headers", {})
                ep.content_type = response_data.get("content_type", "")
                ep.response_payload = response_data.get("body")
                logger.debug(f"Intercepted API Call Response status: {response_data.get('status')}")
                break

    def _is_static_asset(self, url: str) -> bool:
        """Excludes typical static media, styling, and script assets."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        static_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js', '.woff', '.woff2', '.ico']
        return any(path.endswith(ext) for ext in static_extensions)

    def get_summary(self) -> List[Dict[str, Any]]:
        """Summarizes all intercepted REST endpoints."""
        return [ep.to_dict() for ep in self.endpoints]
