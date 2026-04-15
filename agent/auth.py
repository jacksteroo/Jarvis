from fastapi import Header, HTTPException
from agent.config import settings


async def require_api_key(x_api_key: str = Header(...)):
    if not settings.API_KEY or x_api_key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
