import asyncio
from agent_client.api import run_agent

async def test_agent():
    print("Testing connection to agentic backend...")
    messages = [{"role": "user", "content": "list the folders in the c drive"}]
    queue = asyncio.Queue()
    
    # Run agent in background task
    asyncio.create_task(run_agent(messages, queue))
    
    print("\n--- AGENT RESPONSE STREAM ---")
    while True:
        event_type, data = await queue.get()
        if event_type == "done":
            break
        if event_type == "message":
            print(data, end="", flush=True)
        elif event_type == "error":
            print(f"\n[ERROR] {data}")
    print("\n-----------------------------")

if __name__ == "__main__":
    asyncio.run(test_agent())
