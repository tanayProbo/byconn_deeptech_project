import logging
import os
from typing import Dict, Any, List

logger = logging.getLogger("byconnx.free_api.generator")

class ConnectorGenerator:
    """
    Code generator engine that dynamically writes Python client wrapper libraries
    for any registered public API. Implements async request routing, credentials injection,
    and automatic rate limiting politeness rules.
    """
    def __init__(self, output_dir: str = ""):
        self.output_dir = output_dir

    def generate_connector_code(self, api_meta: Dict[str, Any]) -> str:
        """Generates dynamic class template code from target API definitions."""
        class_name = "".join(x.capitalize() for x in api_meta["name"].replace("-", " ").replace("_", " ").split() if x.isalnum())
        if not class_name.endswith("Client"):
            class_name += "Client"
            
        base_url = api_meta["base_url"]
        auth_type = api_meta.get("auth_type", "none")
        
        # Build methods code snippet blocks
        methods_code = []
        endpoints: List[Dict[str, Any]] = api_meta.get("endpoints", [])
        
        for ep in endpoints:
            path = ep["path"]
            method = ep.get("method", "GET").upper()
            desc = ep.get("description", "Dynamic API call endpoint.")
            
            # Clean path to write clean python method names
            method_name = path.strip("/").replace("/", "_").replace(".", "_").replace("{", "").replace("}", "")
            if not method_name:
                method_name = "get_root"
            method_name = f"{method.lower()}_{method_name}"
            
            # Check if path contains path parameters like {block_hash}
            params_args = []
            url_format_string = f"f'{{self.base_url}}{path}'"
            if "{" in path:
                path_params = [p.split("}")[0] for p in path.split("{")[1:]]
                for param in path_params:
                    params_args.append(param)
                # Format parameters inside class method args
                url_format_string = url_format_string.replace("{", "{").replace("}", "}")
                
            args_str = ", ".join(["self"] + [f"{arg}: str" for arg in params_args] + ["params: dict = None", "headers: dict = None", "data: dict = None"])
            
            method_snippet = f"""    async def {method_name}({args_str}) -> dict:
        \"\"\"
        {desc}
        \"\"\"
        url = {url_format_string}
        # Injects authorization headers
        req_headers = self._build_headers(headers)
        
        # Respect rate limits before firing requests
        await asyncio.sleep(self.rate_limit_delay)
        
        async with self.session.request(
            method="{method}",
            url=url,
            params=params,
            headers=req_headers,
            json=data if "{method}" in ["POST", "PUT", "PATCH"] else None
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                response_text = await response.text()
                raise RuntimeError(f"API Request Failed: {{response.status}} - {{response_text}}")"""
            methods_code.append(method_snippet)
            
        methods_str = "\n\n".join(methods_code)

        template = f"""# AUTO-GENERATED CLIENT CONNECTOR FOR {api_meta['name'].upper()}
# Generated dynamically by BYCONN-X Data Engine Connector Generator

import asyncio
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger("byconnx.connectors.{api_meta['id']}")

class {class_name}:
    \"\"\"
    {api_meta.get('description', 'Auto-generated API Connector library.')}
    \"\"\"
    def __init__(self, session: aiohttp.ClientSession, api_key: Optional[str] = None, rate_limit_delay: float = 1.0):
        self.session = session
        self.base_url = "{base_url}"
        self.api_key = api_key
        self.auth_type = "{auth_type}"
        self.rate_limit_delay = rate_limit_delay

    def _build_headers(self, headers: Optional[dict] = None) -> dict:
        req_headers = dict(headers or {{}})
        if self.auth_type == "apiKey" and self.api_key:
            # Custom api key injection layout
            req_headers["Authorization"] = f"Bearer {{self.api_key}}"
        return req_headers

{methods_str}
"""
        return template

    def write_connector_file(self, api_meta: Dict[str, Any]) -> str:
        """Writes the generated class block to output target path file."""
        code = self.generate_connector_code(api_meta)
        filename = f"connector_{api_meta['id'].replace('-', '_')}.py"
        
        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)
            target_path = os.path.join(self.output_dir, filename)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(code)
            logger.info(f"Successfully generated and wrote client connector script: {target_path}")
            return target_path
            
        return filename
