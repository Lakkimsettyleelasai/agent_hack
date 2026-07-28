import asyncio
import os
from deepagents import create_deep_agent
from langchain_community.chat_models import ChatOllama
from langchain_core.tools import tool

@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

async def main():
    model = ChatOllama(model="qwen2.5-coder:7b-instruct-q8_0", base_url="http://100.116.59.48:11434", temperature=0)
    agent = create_deep_agent(
        model=model,
        tools=[add],
        system_prompt="You are a helpful assistant."
    )
    
    print("Agent type:", type(agent))
    
    async for chunk in agent.astream({"messages": [("user", "what is 2 + 3?")]}, stream_mode="values"):
        print("Chunk:", chunk)

if __name__ == "__main__":
    asyncio.run(main())
