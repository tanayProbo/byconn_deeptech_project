import re
import xml.etree.ElementTree as ET
import logging
from typing import Dict, Any, List, Tuple
from .adb_client import ADBClient

logger = logging.getLogger("byconnx.mobile.inspector")

class AndroidUIInspector:
    """
    Parses Accessibility Nodes on Android screens to map interactable buttons,
    forms, and labels to physical coordinates.
    """
    def __init__(self, adb_client: ADBClient):
        self.adb = adb_client

    def _parse_bounds(self, bounds_str: str) -> Tuple[int, int, int, int]:
        """Converts uiautomator bounds string format '[x1,y1][x2,y2]' to coordinates."""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return x1, y1, x2, y2
        return 0, 0, 0, 0

    def dump_layout_nodes(self) -> List[Dict[str, Any]]:
        """Dumps accessibility XML trees and maps visual components."""
        # Dump UI XML to device storage
        self.adb._execute(["shell", "uiautomator", "dump", "/data/local/tmp/uidump.xml"])
        
        # Pull XML to workspace
        xml_content, _ = self.adb._execute(["shell", "cat", "/data/local/tmp/uidump.xml"])
        if not xml_content or "<hierarchy" not in xml_content:
            logger.error("Failed to dump accessibility nodes from Android device.")
            return []

        interactables = []
        try:
            root = ET.fromstring(xml_content.strip())
            for node in root.iter("node"):
                attrib = node.attrib
                clickable = attrib.get("clickable") == "true"
                focusable = attrib.get("focusable") == "true"
                text = attrib.get("text", "").strip()
                resource_id = attrib.get("resource-id", "")
                
                # Check for active elements
                if clickable or focusable or text:
                    bounds_str = attrib.get("bounds", "")
                    x1, y1, x2, y2 = self._parse_bounds(bounds_str)
                    
                    # Compute center coordinate
                    cx = (x1 + x2) // 2
                    cy = (y1 + y2) // 2
                    
                    interactables.append({
                        "text": text,
                        "resource_id": resource_id,
                        "class": attrib.get("class", ""),
                        "x": cx,
                        "y": cy,
                        "bounds": (x1, y1, x2, y2),
                        "clickable": clickable,
                        "checkable": attrib.get("checkable") == "true"
                    })
            logger.info(f"Parsed {len(interactables)} interactable accessibility widgets from Android screen.")
            return interactables
        except ET.ParseError as e:
            logger.error(f"Error parsing UI XML: {str(e)}")
            return []
