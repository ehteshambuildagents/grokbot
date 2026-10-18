"""MOCK OpenAI-compatible API for tests (no real provider, no network). Streams scripted replies as SSE, splitting
tool-call arguments into fragments like real APIs do, and records every request for assertions."""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
STATE = {"script": [], "requests": [], "status": 200, "delay": 0.0}


@app.get("/v1/models")
async def models(request: Request):
    if request.headers.get("authorization") != "Bearer test-remote-key":
        return JSONResponse({"error": {"message": "bad key"}}, 401)
    return {"data": [{"id": "fake-model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATE["requests"].append({"headers": dict(request.headers), "body": body})
    if STATE["status"] != 200:
        return JSONResponse({"error": {"message": f"status {STATE['status']}"}}, STATE["status"])
    if body.get("response_format"):  # LocalCrew's planning call (JSON mode)
        step = {"content": json.dumps({"summary": "fake plan", "steps": ["follow the script"]})}
    else:
        step = STATE["script"].pop(0) if STATE["script"] else {"content": "done"}

    async def gen():
        await asyncio.sleep(STATE["delay"])
        if step.get("content"):
            for i in range(0, len(step["content"]), 7):
                yield f"data: {json.dumps({'choices': [{'delta': {'content': step['content'][i:i + 7]}}]})}\n\n"
        for idx, call in enumerate(step.get("tool_calls", [])):
            args = json.dumps(call["arguments"])
            first = {"index": idx, "id": f"c{idx}", "type": "function", "function": {"name": call["name"], "arguments": ""}}
            yield f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [first]}}]})}\n\n"
            for i in range(0, len(args), 5):  # fragmented JSON arguments
                part = {"index": idx, "function": {"arguments": args[i:i + 5]}}
                yield f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [part]}}]})}\n\n"
        yield f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
