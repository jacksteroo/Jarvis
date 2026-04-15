from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"


async def brave_image_search(query: str, api_key: str, count: int = 3) -> list[str]:
    """Call Brave Image Search API and return a list of image URLs."""
    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }
    params = {"q": query, "count": count}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(BRAVE_IMAGE_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    urls = []
    for item in data.get("results", []):
        src = item.get("thumbnail", {}).get("src") or item.get("url", "")
        if src:
            urls.append(src)

    logger.info("image_search", query=query, n_results=len(urls))
    return urls


async def brave_search(query: str, api_key: str, count: int = 5) -> list[dict]:
    """Call Brave Search API and return simplified result list."""
    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }
    params = {"q": query, "count": count}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(BRAVE_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
        })

    logger.info("web_search", query=query, n_results=len(results))
    return results
