import httpx
import asyncio

async def test():
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post("http://localhost:8000/chat/stream", json={
            "query": "list the folders in the c drive",
            "history": [],
            "mode": "agent",
            "auto_approve": True
        })
        async for line in response.aiter_lines():
            if line:
                print(line)

asyncio.run(test())
