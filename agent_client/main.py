import os
import sys
from dotenv import load_dotenv
from smolagents import CodeAgent, OpenAIModel
from tools import LookupTechnicalManualsTool

load_dotenv()

SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1")
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434")

def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py \"<query>\"")
        sys.exit(1)
        
    query = sys.argv[1]

    # Simple heuristic to determine if it's a coding task
    coding_keywords = ["bash script", "python", "code", "write a script", "implement"]
    is_coding = any(keyword in query.lower() for keyword in coding_keywords)

    if is_coding:
        model_name = "qwen2.5-coder:7b-instruct-q8_0"
    else:
        model_name = "qwen2.5-coder:7b-instruct-q8_0"

    print(f"[*] Directing server VRAM allocation to profile: {model_name}")

    model = OpenAIModel(
        model_id=model_name,
        api_base=f"http://{SERVER_IP}:{OLLAMA_PORT}/v1",
        api_key="ollama" # Ollama doesn't require a real API key
    )

    agent = CodeAgent(
        tools=[LookupTechnicalManualsTool()],
        model=model,
        add_base_tools=True
    )

    response = agent.run(query)
    print("\n[Agent Response]")
    print(response)

if __name__ == "__main__":
    main()
