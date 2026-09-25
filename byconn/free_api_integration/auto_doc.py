import logging
from typing import Dict, Any, List

logger = logging.getLogger("byconnx.free_api.autodoc")

class AutoDocumenter:
    """
    Documentation Generator engine. Builds clean, formatted Markdown reference files
    out of registered public API structures and their generated connectors specifications.
    """
    def __init__(self):
        pass

    def generate_api_markdown(self, api_meta: Dict[str, Any]) -> str:
        """Generates comprehensive markdown manuals for the API connector."""
        name = api_meta["name"]
        category = api_meta["category"]
        base_url = api_meta["base_url"]
        auth_type = api_meta.get("auth_type", "none")
        desc = api_meta.get("description", "No description provided.")
        endpoints: List[Dict[str, Any]] = api_meta.get("endpoints", [])

        # Format class name
        class_name = "".join(x.capitalize() for x in api_meta["name"].replace("-", " ").replace("_", " ").split() if x.isalnum())
        if not class_name.endswith("Client"):
            class_name += "Client"

        endpoints_section = []
        for ep in endpoints:
            endpoints_section.append(f"""### `{ep.get('method', 'GET').upper()}` `{ep['path']}`
* **Description**: {ep.get('description', 'No description.')}
""")

        endpoints_md = "\n".join(endpoints_section) if endpoints_section else "No endpoints documented."

        usage_example = f"""```python
import asyncio
import aiohttp
from free_api_integration.connector_{api_meta['id'].replace('-', '_')} import {class_name}

async def main():
    async with aiohttp.ClientSession() as session:
        # Initialize the generated client connector
        client = {class_name}(session=session, api_key="YOUR_API_KEY_IF_NEEDED")

        # Execute query
        try:
            # Invokes first endpoint dynamically
            result = await client.{endpoints[0]['path'].strip('/').replace('/', '_').replace('.', '_').replace('{', '').replace('}', '').lower() if endpoints else 'get_root'}()
            print("Response:", result)
        except Exception as e:
            print("API error:", e)

asyncio.run(main())
```"""

        md = f"""# {name} — API Documentation Reference
**Category**: {category}
**Base Target URL**: `{base_url}`
**Authentication Scheme**: `{auth_type}`

## Overview
{desc}

## Interface Endpoints Catalog
{endpoints_md}

## Programmatic Code Integration Usage
Below is an integration snippet demonstration showing how to initialize and invoke the generated client wrapper:

{usage_example}

---
*Created automatically by the BYCONN-X Data Engine Auto-Documenter subsystem.*
"""
        return md

    def write_doc_file(self, api_meta: Dict[str, Any], output_dir: str) -> str:
        """Writes markdown documentation into target directory files."""
        import os
        os.makedirs(output_dir, exist_ok=True)
        md = self.generate_api_markdown(api_meta)
        filename = f"doc_{api_meta['id'].replace('-', '_')}.md"
        target_path = os.path.join(output_dir, filename)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(md)
        logger.info(f"Wrote generated markdown document to: {target_path}")
        return target_path
