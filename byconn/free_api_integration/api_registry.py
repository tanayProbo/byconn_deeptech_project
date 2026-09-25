import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("byconnx.free_api.registry")

SEED_APIS = [
    {
        "id": "api-weather-01",
        "category": "Weather",
        "name": "Open-Meteo API",
        "base_url": "https://api.open-meteo.com/v1",
        "endpoints": [
            {"path": "/forecast", "method": "GET", "description": "Retrieve weather forecasts based on coordinates"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Free weather forecast API for non-commercial use."
    },
    {
        "id": "api-finance-02",
        "category": "Finance",
        "name": "CoinGecko API",
        "base_url": "https://api.coingecko.com/api/v3",
        "endpoints": [
            {"path": "/ping", "method": "GET", "description": "Check API status"},
            {"path": "/coins/list", "method": "GET", "description": "Get lists of all supported cryptocurrencies"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Comprehensive cryptocurrency data analysis feed."
    },
    {
        "id": "api-news-03",
        "category": "News",
        "name": "HackerNews API",
        "base_url": "https://hacker-news.firebaseio.com/v0",
        "endpoints": [
            {"path": "/topstories.json", "method": "GET", "description": "Retrieve IDs of top HN postings"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Official HackerNews Firebase database endpoints wrapper."
    },
    {
        "id": "api-education-04",
        "category": "Education",
        "name": "Universities List API",
        "base_url": "http://universities.hipolabs.com",
        "endpoints": [
            {"path": "/search", "method": "GET", "description": "Find global colleges by name or country"}
        ],
        "auth_type": "none",
        "https": False,
        "description": "Simple search tool registry for worldwide universities metadata."
    },
    {
        "id": "api-gov-05",
        "category": "Government Open Data",
        "name": "Data USA API",
        "base_url": "https://datausa.io/api",
        "endpoints": [
            {"path": "/data", "method": "GET", "description": "Queries public US demographic and economic logs"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Open public data platform visualization queries."
    },
    {
        "id": "api-scientific-06",
        "category": "Scientific",
        "name": "NASA APOD API",
        "base_url": "https://api.nasa.gov/planetary",
        "endpoints": [
            {"path": "/apod", "method": "GET", "description": "Astronomy Picture of the Day feed"}
        ],
        "auth_type": "apiKey",
        "https": True,
        "description": "Fetches scientific astronomy visuals and descriptions."
    },
    {
        "id": "api-ai-07",
        "category": "AI",
        "name": "Ollama API",
        "base_url": "http://localhost:11434/api",
        "endpoints": [
            {"path": "/generate", "method": "POST", "description": "Trigger local LLM model generation loops"},
            {"path": "/embeddings", "method": "POST", "description": "Generate dynamic float vectors"}
        ],
        "auth_type": "none",
        "https": False,
        "description": "Local large language model backend console endpoint."
    },
    {
        "id": "api-mapping-08",
        "category": "Mapping",
        "name": "OSM Nominatim API",
        "base_url": "https://nominatim.openstreetmap.org",
        "endpoints": [
            {"path": "/search", "method": "GET", "description": "Reverse-geocode locations by names"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "OpenStreetMap reverse-geocoder lookup registry."
    },
    {
        "id": "api-health-09",
        "category": "Healthcare Public APIs",
        "name": "OpenFDA API",
        "base_url": "https://api.fda.gov",
        "endpoints": [
            {"path": "/drug/label.json", "method": "GET", "description": "Search drug warnings and label info"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Public US FDA research records index catalog."
    },
    {
        "id": "api-geo-10",
        "category": "Geospatial",
        "name": "USGS Earthquake API",
        "base_url": "https://earthquake.usgs.gov/fdsnws/event/1",
        "endpoints": [
            {"path": "/query", "method": "GET", "description": "Fetch real-time seismic event listings"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "US Geological Survey real-time seismic records database."
    },
    {
        "id": "api-opendata-11",
        "category": "Open Data",
        "name": "REST Countries API",
        "base_url": "https://restcountries.com/v3.1",
        "endpoints": [
            {"path": "/all", "method": "GET", "description": "List political borders and country populations"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Informational borders and geopolitical profiles lookup database."
    },
    {
        "id": "api-trans-12",
        "category": "Transportation",
        "name": "Transitland API",
        "base_url": "https://transit.land/api/v2",
        "endpoints": [
            {"path": "/operators", "method": "GET", "description": "Get transit systems operators directories"}
        ],
        "auth_type": "apiKey",
        "https": True,
        "description": "Global open transit routing and schedules index."
    },
    {
        "id": "api-astro-13",
        "category": "Astronomy",
        "name": "NASA SSCWeb API",
        "base_url": "https://sscweb.gsfc.nasa.gov/WS/ssc/2",
        "endpoints": [
            {"path": "/observatories", "method": "GET", "description": "List orbiting scientific spacecrafts"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "NASA Satellite Situation Center spacecraft orbits tracker."
    },
    {
        "id": "api-chain-14",
        "category": "Blockchain",
        "name": "Blockchain.info API",
        "base_url": "https://blockchain.info",
        "endpoints": [
            {"path": "/rawblock/{block_hash}", "method": "GET", "description": "Get transactions contained in block"}
        ],
        "auth_type": "none",
        "https": True,
        "description": "Bitcoin block details and exchange metrics api."
    },
    {
        "id": "api-sec-15",
        "category": "Cybersecurity",
        "name": "HaveIBeenPwned API",
        "base_url": "https://haveibeenpwned.com/api/v3",
        "endpoints": [
            {"path": "/breaches", "method": "GET", "description": "Gets accounts compromise breaches details"}
        ],
        "auth_type": "apiKey",
        "https": True,
        "description": "Analyzes historical database compromises for specific emails."
    }
]

class APIRegistry:
    """
    Catalog registry tracking the 15 categories of free public APIs.
    Integrates loading of initial catalogs, registers new custom APIs,
    and returns endpoint routing metadata to crawlers and data brokers.
    """
    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = storage_path
        self.registry: Dict[str, Dict[str, Any]] = {api["id"]: api for api in SEED_APIS}

    def list_categories(self) -> List[str]:
        """Lists all categories present inside the index database."""
        return list(set(api["category"] for api in self.registry.values()))

    def get_apis_by_category(self, category: str) -> List[Dict[str, Any]]:
        """Filters public APIs by specific category name."""
        return [api for api in self.registry.values() if api["category"].lower() == category.lower()]

    def search_apis(self, keyword: str) -> List[Dict[str, Any]]:
        """Searches name and descriptions by query matches."""
        kw = keyword.lower()
        return [
            api for api in self.registry.values()
            if kw in api["name"].lower() or kw in api["description"].lower()
        ]

    def register_api(self, api_data: Dict[str, Any]) -> str:
        """Saves a new custom API metadata record into the catalog database."""
        api_id = api_data.get("id") or f"api-custom-{len(self.registry) + 1}"
        api_data["id"] = api_id
        
        # Verify required keys exist
        required_keys = ["name", "base_url", "category"]
        for key in required_keys:
            if key not in api_data:
                raise ValueError(f"Missing required metadata element for API registration: {key}")
                
        if "endpoints" not in api_data:
            api_data["endpoints"] = []
            
        self.registry[api_id] = api_data
        logger.info(f"Registered new public API: {api_data['name']} (ID: {api_id})")
        return api_id

    def get_api(self, api_id: str) -> Optional[Dict[str, Any]]:
        """Fetches a specific API profile configuration."""
        return self.registry.get(api_id)
