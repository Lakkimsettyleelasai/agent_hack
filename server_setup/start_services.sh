#!/bin/bash
# Start services
echo "Starting Qdrant..."
docker run -d -p 6333:6333 -p 6334:6334 qdrant/qdrant
echo "Pulling Ollama models..."
ollama pull llama3.1:8b
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
echo "Services started."
