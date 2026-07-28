import os
import sys
import asyncio
from dotenv import load_dotenv

# We must be in agent_client directory to import tools correctly
sys.path.append(os.path.join(os.path.dirname(__file__), "agent_client"))

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama
from deepagents import create_deep_agent
from agent_client.tools import LookupTechnicalManualsTool, RunTerminalCommandTool, WriteFileTool, ReadWebpageTool

load_dotenv()
SERVER_IP = os.getenv("SERVER_IP", "100.116.59.48")
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434")
MODEL_NAME = "qwen2.5-coder:7b"

async def main():
    print(f"Connecting to remote Ollama at {SERVER_IP}:{OLLAMA_PORT}...")
    llm = ChatOllama(
        model=MODEL_NAME,
        base_url=f"http://{SERVER_IP}:{OLLAMA_PORT}",
        temperature=0.0
    )
    
    agent = create_deep_agent(
        model=llm,
        tools=[LookupTechnicalManualsTool(), RunTerminalCommandTool(), WriteFileTool(), ReadWebpageTool()],
        system_prompt="You are a helpful assistant."
    )
    
    query = "list the folders in the c drive"
    print(f"\nSending query: '{query}'")
    
    try:
        messages = [HumanMessage(content=query)]
        async for event in agent.astream_events({"messages": messages}, version="v2"):
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if hasattr(chunk, "content") and chunk.content:
                    print(chunk.content, end="", flush=True)
            elif event["event"] == "on_tool_start":
                print(f"\n[Tool Executing: {event['name']}]")
            elif event["event"] == "on_tool_end":
                print(f"\n[Tool Finished: {event['name']}]")
        print("\n\nFinished successfully.")
    except Exception as e:
        print(f"\nError occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
