"""Mattermost webhook → Internal Agent Interface (Section 37)"""
from fastapi import APIRouter
router = APIRouter()

@router.post("/mattermost/events")
async def mattermost_event(payload: dict):
    # 1. verify signature  2. map user → agent  3. create/resume session  4. forward to ACP
    return {"received": True}
