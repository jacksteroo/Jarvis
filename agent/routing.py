from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()


async def get_driving_time(
    origin: str,
    destination: str,
    api_key: str,
) -> dict:
    """
    Get driving duration and distance using Google Maps Distance Matrix API.
    Returns dict with duration, distance, and traffic-aware duration if available.
    """
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": origin,
        "destinations": destination,
        "key": api_key,
        "departure_time": "now",   # enables live traffic
        "mode": "driving",
        "units": "imperial",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "OK":
        return {"error": f"Google Maps error: {data.get('status')}"}

    try:
        row = data["rows"][0]["elements"][0]
        if row["status"] != "OK":
            return {"error": f"Route not found: {row['status']}"}

        result = {
            "origin": data["origin_addresses"][0],
            "destination": data["destination_addresses"][0],
            "distance": row["distance"]["text"],
            "duration": row["duration"]["text"],
        }
        # duration_in_traffic only present when departure_time=now and traffic data exists
        if "duration_in_traffic" in row:
            result["duration_in_traffic"] = row["duration_in_traffic"]["text"]

        logger.info(
            "routing",
            origin=origin[:60],
            destination=destination[:60],
            duration=result.get("duration_in_traffic") or result["duration"],
        )
        return result

    except (KeyError, IndexError) as e:
        return {"error": f"Unexpected response structure: {e}"}
