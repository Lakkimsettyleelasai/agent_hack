@echo off
echo Starting Context-Aware Cybersecurity Agent...

:: Start the Python Backend API in the background
cd /d "C:\Users\leelasai\Pictures\New folder\agenthack\project_root\agent_client"
start /B "Agent API" python api.py

:: Start the React Frontend UI in the background
cd /d "C:\Users\leelasai\Pictures\New folder\agenthack\project_root\frontend"
start /B "Agent UI" npm run dev

:: Note: If you ever install Docker for Qdrant, you can uncomment the line below:
:: docker run -d --name qdrant --restart unless-stopped -p 6333:6333 -p 6334:6334 qdrant/qdrant

echo Agent successfully started! The Web UI will be available at http://localhost:5173/
