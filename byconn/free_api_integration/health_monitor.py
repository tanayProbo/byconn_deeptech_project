import time
import asyncio
import logging
import aiohttp
from typing import Dict, Any, List, Optional
from .api_registry import APIRegistry

logger = logging.getLogger("byconnx.free_api.monitor")

# Path-parameter placeholders in the seed catalogue need a concrete value to probe.
PLACEHOLDER_VALUES = {
    "{block_hash}": "00000000000000000003c2ffb514c3a9f0e1fb719eb7664d6fa9e1d88cc2e37f",
    "{page}": "1",
    "{id}": "1",
    "{query}": "test",
}


class APIHealthMonitor:
    """
    Health tracking scheduler probing public endpoints.

    Measures latency and maps availability indicators. Every result is always
    emitted as a structured log line, and is additionally persisted to
    PostgreSQL when a :class:`~byconn.storage.adapters.PostgresAdapter` is
    supplied.

    The adapter is duck-typed on purpose: only ``record_api_health`` is called,
    so this package stays importable without the database drivers installed.

    Args:
        registry: Source catalogue of public APIs to probe.
        postgres: Optional adapter exposing
            ``record_api_health(api_id, url, status_code, latency_ms, is_up,
            error_message)``. Health data is logged regardless.
    """

    def __init__(self, registry: APIRegistry, postgres: Optional[Any] = None):
        self.registry = registry
        self.postgres = postgres

    async def probe_endpoint(self, base_url: str, path: str = "") -> Dict[str, Any]:
        """Probes a specific API endpoint to fetch latency and status code."""
        url = base_url + path
        start_time = time.time()
        timeout = aiohttp.ClientTimeout(total=5)  # 5 seconds limit

        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                # Use GET or HEAD to inspect status
                async with session.get(url) as response:
                    latency = int((time.time() - start_time) * 1000)
                    logger.info(f"Health Probe completed: {url} -> Status: {response.status} ({latency}ms)")
                    return {
                        "is_up": 1 if response.status < 500 else 0,
                        "status_code": response.status,
                        "latency_ms": latency,
                        "error_message": ""
                    }
            except asyncio.TimeoutError:
                latency = int((time.time() - start_time) * 1000)
                logger.error(f"Health Probe Timeout for URL: {url}")
                return {
                    "is_up": 0,
                    "status_code": 408,
                    "latency_ms": latency,
                    "error_message": "Request Timeout"
                }
            except Exception as e:
                latency = int((time.time() - start_time) * 1000)
                logger.error(f"Health Probe Connection Error for {url}: {str(e)}")
                return {
                    "is_up": 0,
                    "status_code": 0,
                    "latency_ms": latency,
                    "error_message": str(e)
                }

    async def check_api(self, api_id: str) -> Dict[str, Any]:
        """Runs health validation across the first listed endpoint of an API ID."""
        api = self.registry.get_api(api_id)
        if not api:
            raise ValueError(f"No API found matching registry ID: {api_id}")

        base_url = api["base_url"]
        endpoints = api.get("endpoints", [])
        path = endpoints[0]["path"] if endpoints else ""

        # Substitute any path-template placeholders with probe-safe values.
        for placeholder, value in PLACEHOLDER_VALUES.items():
            path = path.replace(placeholder, value)

        res = await self.probe_endpoint(base_url, path)

        report = {
            "api_id": api_id,
            "api_name": api["name"],
            "url": base_url + path,
            "status_code": res["status_code"],
            "latency_ms": res["latency_ms"],
            "is_up": res["is_up"] == 1,
            "error_message": res["error_message"]
        }

        self._record(report)
        return report

    def _record(self, report: Dict[str, Any]) -> None:
        """Emits a structured log line and persists to PostgreSQL when wired.

        Persistence is best-effort: a database outage must not fail a health
        probe, so errors are logged and swallowed.
        """
        level = logging.INFO if report["is_up"] else logging.WARNING
        logger.log(
            level,
            "api_health api_id=%s status=%s latency_ms=%s up=%s url=%s error=%s",
            report["api_id"],
            report["status_code"],
            report["latency_ms"],
            report["is_up"],
            report["url"],
            report["error_message"] or "-",
        )

        if self.postgres is None:
            return
        recorder = getattr(self.postgres, "record_api_health", None)
        if recorder is None:
            logger.debug("postgres adapter has no record_api_health; skipping persistence")
            return
        try:
            recorder(**report)
        except Exception as exc:
            logger.warning("Failed to persist api health for %s: %s", report["api_id"], exc)

    async def check_all_apis(self) -> List[Dict[str, Any]]:
        """Concurrently probes all catalogued APIs in the registry."""
        tasks = []
        for api_id in self.registry.registry.keys():
            tasks.append(self.check_api(api_id))
        reports = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter successful checks
        valid_reports = []
        for r in reports:
            if isinstance(r, dict):
                valid_reports.append(r)
            else:
                logger.error(f"Task failed during concurrent health probe: {r}")
        return valid_reports
