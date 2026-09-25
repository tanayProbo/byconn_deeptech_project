import time
import asyncio
import logging
import aiohttp
from typing import Dict, Any, List, Optional
from .api_registry import APIRegistry

logger = logging.getLogger("byconnx.free_api.monitor")

class APIHealthMonitor:
    """
    Health tracking scheduler probing public endpoints.
    Measures latency metrics and maps availability indicators. Logs results
    to Postgres or ClickHouse analytical tables.
    """
    def __init__(self, registry: APIRegistry, clickhouse_client: Optional[Any] = None):
        self.registry = registry
        self.ch = clickhouse_client

    async def probe_endpoint(self, base_url: str, path: str = "") -> Dict[str, Any]:
        """Probes a specific API endpoint to fetch latency and status code."""
        url = base_url + path
        start_time = time.time()
        timeout = aiohttp.ClientTimeout(total=5) # 5 seconds limit
        
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
        """Runs health validations across first listed endpoint of selected API ID."""
        api = self.registry.get_api(api_id)
        if not api:
            raise ValueError(f"No API found matching registry ID: {api_id}")
            
        base_url = api["base_url"]
        endpoints = api.get("endpoints", [])
        path = endpoints[0]["path"] if endpoints else ""
        
        # Format block hash parameter mock value if required by path template
        path = path.replace("{block_hash}", "00000000000000000003c2ffb514c3a9f0e1fb719eb7664d6fa9e1d88cc2e37f")
        
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
        
        # Log to analytical warehouse if adapter client is bound
        if self.ch:
            self.ch.write_crawl_event({
                "time": time.strftime('%Y-%m-%d %H:%M:%S'),
                "job_id": "api-health-cron-job",
                "url": report["url"],
                "status_code": report["status_code"],
                "response_time_ms": report["latency_ms"],
                "bytes_downloaded": 0,
                "proxy_used": "direct",
                "error_message": report["error_message"]
            })
            
        return report

    async def check_all_apis(self) -> List[Dict[str, Any]]:
        """Concurrently probes all cataloged APIs in the registry."""
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
