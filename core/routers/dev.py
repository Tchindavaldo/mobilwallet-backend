"""Routes dev : pilotage libre du navigateur et ping LLM."""

from fastapi import APIRouter, HTTPException

from core import runtime
from core.schemas.dev import DriveRequest

router = APIRouter()


@router.post("/drive", tags=["dev"], summary="Pilotage libre du navigateur (dev)",
             include_in_schema=False)
async def drive(req: DriveRequest):
    """Drive the browser to a URL and let the AI agent handle it."""
    browser = runtime.get_browser()
    llm = runtime.get_llm()
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    from core.reasoning_loop import ReasoningLoop

    # Acquire an isolated session for this dev run, release it when done.
    session = await browser.acquire_session()
    try:
        session.start_capture()
        await session.goto(req.url)
        loop = ReasoningLoop(session, llm, max_turns=15)
        result = await loop.run(req.objective)
        captured = session.stop_capture()
        charge = session.get_flutterwave_charge()

        return {
            "success": result.success,
            "result": result.result,
            "error": result.error,
            "turns": result.turns,
            "input_tokens": result.total_input_tokens,
            "output_tokens": result.total_output_tokens,
            "flutterwave_charge": {
                "url": charge.url if charge else "",
                "method": charge.method if charge else "",
                "request_body": charge.request_body if charge else "",
                "response_body": charge.response_body[:1000] if charge else "",
                "curl_replay": charge.to_curl() if charge else "",
            },
            "all_requests": [
                {"method": r.method, "url": r.url[:200], "status": r.status}
                for r in captured
            ],
        }
    finally:
        await browser.release_session(session)


@router.post("/test-llm", tags=["dev"], summary="Ping du LLM (dev)",
             include_in_schema=False)
async def test_llm():
    """Quick test: send a simple prompt to DeepSeek and return the response."""
    llm = runtime.get_llm()
    if not llm:
        raise HTTPException(500, "Not initialized")

    resp = await llm.send(
        system_prompt="Reponds en JSON: {\"status\": \"ok\", \"message\": \"...\"}",
        user_content=[{"type": "text", "text": "Dis bonjour en une phrase."}],
    )
    return {"success": resp.success, "error": resp.error, "text": resp.text}
