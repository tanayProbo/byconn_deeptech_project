import json
import logging
from typing import Dict, Any, List
from playwright.async_api import Page

logger = logging.getLogger("byconnx.visual_agent.dom_parser")

class DOMParser:
    """
    Parses the current Playwright Page's live DOM tree.
    Extracts interactable nodes (buttons, links, textboxes) with coordinates,
    stripping raw boilerplate to yield a clean layout mapping compatible with LLM windows.
    """
    def __init__(self):
        # JavaScript script to evaluate inside page context
        self.extraction_js = """
        () => {
            const interactables = [];
            const walk = (node, depth) => {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    
                    const rect = node.getBoundingClientRect();
                    const isVisible = rect.width > 0 && rect.height > 0;
                    
                    const tagName = node.tagName.toLowerCase();
                    const role = node.getAttribute('role') || '';
                    const isClickable = (
                        tagName === 'a' || 
                        tagName === 'button' || 
                        node.onclick || 
                        style.cursor === 'pointer' ||
                        role === 'button' ||
                        role === 'link'
                    );
                    const isInput = (
                        tagName === 'input' || 
                        tagName === 'textarea' || 
                        tagName === 'select'
                    );

                    if (isVisible && (isClickable || isInput)) {
                        interactables.push({
                            id: interactables.length,
                            tag: tagName,
                            text: (node.innerText || node.placeholder || node.ariaLabel || '').trim().substring(0, 50),
                            x: Math.round(rect.left + rect.width / 2),
                            y: Math.round(rect.top + rect.height / 2),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                            role: role || (isInput ? 'input' : 'clickable')
                        });
                    }
                }
                for (let child of node.childNodes) {
                    walk(child, depth + 1);
                }
            };
            walk(document.body, 0);
            return interactables;
        }
        """

    async def get_interactables(self, page: Page) -> List[Dict[str, Any]]:
        """Evaluates JavaScript script to gather node details and positions."""
        try:
            nodes = await page.evaluate(self.extraction_js)
            logger.info(f"Successfully extracted {len(nodes)} interactable visual nodes from page.")
            return nodes
        except Exception as e:
            logger.error(f"Error parsing DOM node elements: {str(e)}")
            return []
