import os
import sys

# Enforce UTF-8 output across Windows consoles to prevent rich/smolagents charmap errors with emojis
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import json
import re
import asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from smolagents import ToolCallingAgent, OpenAIModel, Tool
from deepagents import create_deep_agent
from langchain_ollama import ChatOllama
import openai
import threading
import uuid
import httpx

load_dotenv()
_parent_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_parent_env):
    load_dotenv(_parent_env, override=True)

# Import our refactored LangChain tools
from tools import LookupTechnicalManualsTool, RunTerminalCommandTool, WriteFileTool, ReadWebpageTool, FinalAnswerTool, pending_approvals

AGENT_BACKEND = os.getenv("AGENT_BACKEND", "deepagents").lower()

class FixedChatOllama(ChatOllama):
    """
    Middleware wrapper that intercepts Ollama responses and bridges raw JSON content
    into structured tool_calls required by LangGraph / deepagents ToolNode.
    """
    def _fix_message(self, msg, messages):
        if not getattr(msg, 'tool_calls', None) and getattr(msg, 'content', None):
            raw_text = msg.content.strip()
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
                    raw_text = "\n".join(lines[1:-1]).strip()
            tc_name = None
            tc_args = {}
            try:
                # Escape unescaped backslashes before JSON decoding (e.g. dir C:\ -> dir C:\\)
                safe_text = re.sub(r'\\(?![/u"\\bfnrt])', r'\\\\', raw_text)
                data = json.loads(safe_text)
                if isinstance(data, dict):
                    if 'name' in data:
                        tc_name = data['name']
                        tc_args = data.get('arguments', data.get('args', {}))
                    elif 'tool' in data:
                        tc_name = data['tool']
                        tc_args = data.get('arguments', data.get('args', {}))
                    elif 'action' in data:
                        tc_name = data['action']
                        tc_args = data.get('action_input', data.get('arguments', {}))
                    elif any(k in ['run_terminal_command', 'lookup_technical_manuals', 'write_file', 'read_webpage', 'final_answer'] for k in data.keys()):
                        for k in ['run_terminal_command', 'lookup_technical_manuals', 'write_file', 'read_webpage', 'final_answer']:
                            if k in data:
                                tc_name = k
                                tc_args = data[k] if isinstance(data[k], dict) else {'command' if k == 'run_terminal_command' else ('query' if k == 'lookup_technical_manuals' else 'answer'): data[k]}
                                break
                    elif 'command' in data:
                        tc_name = 'run_terminal_command'
                        tc_args = {'command': data['command']}
                    elif 'query' in data:
                        tc_name = 'lookup_technical_manuals'
                        tc_args = {'query': data['query']}
                    elif 'answer' in data:
                        tc_name = 'final_answer'
                        tc_args = {'answer': data['answer']}
            except Exception:
                pass

            # Regex fallback if json.loads failed or syntax was slightly malformed
            if not tc_name:
                m_cmd = re.search(r'"command"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
                if m_cmd:
                    tc_name = 'run_terminal_command'
                    tc_args = {'command': m_cmd.group(1).replace('\\\\', '\\')}
                else:
                    m_query = re.search(r'"query"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
                    if m_query:
                        tc_name = 'lookup_technical_manuals'
                        tc_args = {'query': m_query.group(1).replace('\\\\', '\\')}
                    else:
                        m_ans = re.search(r'"answer"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
                        if m_ans:
                            tc_name = 'final_answer'
                            tc_args = {'answer': m_ans.group(1).replace('\\\\', '\\')}

            if tc_name:
                if tc_name not in ['run_terminal_command', 'lookup_technical_manuals', 'write_file', 'read_webpage', 'final_answer']:
                    cmd_str = tc_name.replace('_', ' ').strip()
                    if isinstance(tc_args, dict) and tc_args:
                        cmd_str += ' ' + ' '.join(str(v) for v in tc_args.values() if isinstance(v, (str, int)))
                    tc_name = 'run_terminal_command'
                    tc_args = {'command': cmd_str.strip()}

                if isinstance(tc_args, str):
                    try:
                        tc_args = json.loads(tc_args)
                    except Exception:
                        tc_args = {'command' if tc_name == 'run_terminal_command' else ('query' if tc_name == 'lookup_technical_manuals' else 'answer'): tc_args}
                msg.tool_calls = [{'name': tc_name, 'args': tc_args if isinstance(tc_args, dict) else {}, 'id': str(uuid.uuid4())[:8], 'type': 'tool_call'}]
                msg.content = ''

        if not getattr(msg, 'tool_calls', None):
            has_human_manual_query = False
            extract_query_text = ""
            for m in messages:
                content_str = getattr(m, 'content', str(m))
                if getattr(m, 'type', '') == 'human' or 'HumanMessage' in str(type(m)):
                    if any(k in content_str.lower() for k in ['manual', 'ntlm', 'ldap', 'relay', 'rag', 'defense', 'securing windows active directory', 'privilege escalation']):
                        has_human_manual_query = True
                        extract_query_text = content_str
                        break

            has_manual_tool_run = any('lookup_technical_manuals' in str(m) or 'technical_manuals' in str(m) or (getattr(m, 'type', '') == 'tool' and 'Relevant Manual Chunk' in getattr(m, 'content', '')) for m in messages)

            if has_human_manual_query and not has_manual_tool_run:
                rag_q = extract_query_text
                if "look up" in rag_q.lower():
                    rag_q = rag_q[rag_q.lower().find("look up"):]
                elif "technical manual" in rag_q.lower():
                    rag_q = rag_q[rag_q.lower().find("technical manual"):]
                rag_q = re.sub(r'^(?:look up(?: our)? technical manuals(?: on)?|technical manuals(?: on)?|look up(?: our)?)\s*', '', rag_q, flags=re.IGNORECASE).strip()
                if not rag_q:
                    rag_q = "general defenses against NTLM or LDAP relay attacks on Windows networks"
                msg.tool_calls = [{'name': 'lookup_technical_manuals', 'args': {'query': rag_q}, 'id': str(uuid.uuid4())[:8], 'type': 'tool_call'}]
                msg.content = ''
        if getattr(msg, 'tool_calls', None):
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    tc['type'] = 'tool_call'
        return msg

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        res = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if res.generations and res.generations[0].message:
            self._fix_message(res.generations[0].message, messages)
        return res

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        res = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if res.generations and res.generations[0].message:
            self._fix_message(res.generations[0].message, messages)
        return res

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            self._fix_message(chunk, messages)
            yield chunk

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            self._fix_message(chunk, messages)
            yield chunk

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import socket

SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1")
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434")

if SERVER_IP != "127.0.0.1" and SERVER_IP != "localhost":
    try:
        with socket.create_connection((SERVER_IP, int(OLLAMA_PORT)), timeout=3.0):
            pass
    except Exception:
        SERVER_IP = "127.0.0.1"

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    query: str
    history: list[Message] = []
    mode: str = "agent"
    auto_approve: bool = False

async def agent_stream_generator(query: str, history: list[Message], model_name: str, auto_approve: bool):
    system_prompt = """You are an elite Context-Aware Cybersecurity AI Assistant. 

CRITICAL INSTRUCTIONS:
1. Act naturally as an intelligent cybersecurity expert.
2. Provide highly detailed, comprehensive, and exhaustive answers.
3. Use rich markdown formatting (headers, bullet points, and code blocks) for readability.
4. YOU HAVE FULL, UNRESTRICTED ACCESS TO THE USER'S LOCAL FILE SYSTEM. Use your tools to access it.
5. DO NOT hallucinate tools. Only use: `run_terminal_command`, `lookup_technical_manuals`, `write_file`, `read_webpage`, `final_answer`. If you need to run a shell command like `ollama list` or `netstat`, ALWAYS call `run_terminal_command(command="ollama list")`. Never call `ollama_list` as a tool name! When checking or listing Ollama models, ALWAYS run `run_terminal_command(command="ollama list")`. DO NOT try to check or guess filesystem paths like C:\ollama\models or dir C:\ollama\models.
6. When using tools to check the local computer (like listing folders), do not hallucinate file contents.
7. Treat conversation history ONLY as read-only context to understand what the user is referring to.
8. MULTI-STEP EXECUTION: If the user's prompt asks for MULTIPLE actions or steps (e.g. "First run netstat, once verified look up our technical manuals on NTLM/LDAP relay attacks"), you MUST execute EVERY requested step sequentially before calling `final_answer` or giving your final response. DO NOT stop after completing only the first step!
"""

    task_id = str(uuid.uuid4())
    
    # Verify model availability on SERVER_IP; if not found, fall back to first available model on SERVER_IP
    try:
        import urllib.request
        tags_req = urllib.request.urlopen(f"http://{SERVER_IP}:{OLLAMA_PORT}/api/tags", timeout=2.0)
        installed = [m['name'] for m in json.loads(tags_req.read().decode())['models']]
        if installed and not any(model_name == m or model_name in m or m in model_name for m in installed):
            model_name = installed[0]
    except Exception:
        pass

    # Initialize the Ollama client for planner & fallback
    client = openai.OpenAI(
        base_url=f"http://{SERVER_IP}:{OLLAMA_PORT}/v1",
        api_key="ollama",
        http_client=httpx.Client(timeout=1200.0),
        max_retries=2
    )

    if AGENT_BACKEND == "deepagents":
        model = FixedChatOllama(
            model=model_name,
            base_url=f"http://{SERVER_IP}:{OLLAMA_PORT}",
            temperature=0.1,
            client_kwargs={'timeout': httpx.Timeout(1200.0)}
        )
        tools = [
            LookupTechnicalManualsTool(),
            RunTerminalCommandTool(task_id=task_id, auto_approve=auto_approve),
            WriteFileTool(task_id=task_id, auto_approve=auto_approve),
            ReadWebpageTool(task_id=task_id, auto_approve=auto_approve),
            FinalAnswerTool()
        ]
        agent = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt
        )
    else:
        model = OpenAIModel(
            model_id=model_name,
            client=client
        )
        tools = [
            Tool.from_langchain(LookupTechnicalManualsTool()),
            Tool.from_langchain(RunTerminalCommandTool(task_id=task_id, auto_approve=auto_approve)),
            Tool.from_langchain(WriteFileTool(task_id=task_id, auto_approve=auto_approve)),
            Tool.from_langchain(ReadWebpageTool(task_id=task_id, auto_approve=auto_approve)),
            Tool.from_langchain(FinalAnswerTool())
        ]
        agent = ToolCallingAgent(
            tools=tools,
            model=model,
            max_steps=8
        )

    yield f"data: {json.dumps({'type': 'meta', 'model': model_name})}\n\n"

    queue = asyncio.Queue()
    
    # Background task to poll pending approvals
    async def approval_poller():
        try:
            while True:
                if task_id in pending_approvals and not pending_approvals[task_id]["event"].is_set():
                    if not pending_approvals[task_id].get("notified"):
                        await queue.put(("approval", json.dumps({
                            'type': 'approval_request', 
                            'command': pending_approvals[task_id]['command'], 
                            'task_id': task_id
                        })))
                        pending_approvals[task_id]["notified"] = True
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            pass

    approval_task = asyncio.create_task(approval_poller())

    # The agent run loop
    def agent_thread(loop, q):
        try:
            import tools
            tools.last_terminal_output = ""
            
            history_str = ""
            if history and len(history) > 0:
                history_lines = [f"{('User' if m.role=='user' else 'Assistant')}: {m.content[:300]}" for m in history[-4:]]
                history_str = "<PAST_CONVERSATION_HISTORY (READ-ONLY context from earlier turns - DO NOT repeat or summarize these past outputs right now!)>\n" + "\n".join(history_lines) + "\n</PAST_CONVERSATION_HISTORY>\n\n"

            # Step 0: Use the specialist coding model as an internal Task Architect to structure the prompt into explicit steps
            asyncio.run_coroutine_threadsafe(
                q.put(("step", json.dumps({
                    'type': 'step', 
                    'content': {'thought': f'Structuring raw query into explicit execution plan via Task Architect ({model_name})...', 'tool_calls': [{'name': 'task_architect', 'arguments': {'query': query}}]}
                }))), loop)
            
            planner_prompt = (
                "You are an elite AI Task Architect & Execution Planner for a Windows system.\n"
                "Your job is to take a raw user prompt and break it down into clear, numbered, sequential steps on separate lines so that an autonomous tool-calling agent can execute them flawlessly without skipping steps.\n"
                "Available tools for the agent:\n"
                "1. 'run_terminal_command': execute shell/PowerShell/cmd commands on Windows.\n"
                "2. 'write_file': create or overwrite a file with specific content (requires 'file_path' and 'content'). Pass 'LAST_STDOUT' as content to automatically write the exact output from the previous terminal command without having to copy-paste it all.\n"
                "3. 'lookup_technical_manuals': search local RAG database for technical manuals.\n"
                "4. 'read_webpage': scrape/fetch markdown from a URL.\n"
                "5. 'final_answer': return the final answer to the user ONLY AFTER all necessary tools/files are completed.\n\n"
                 "Rules:\n"
                 " - The target operating system is Windows. Always use built-in Windows commands or PowerShell (`powershell -Command \"...\"`).\n"
                 " - When generating PowerShell commands for metrics (RAM, CPU, Disk, Network), follow these dynamic execution principles:\n"
                 "   * Convert raw bytes/KB to readable units (MB/GB) using `[math]::Round(..., 2)`.\n"
                 "   * For physical memory / RAM checks, propose EXACTLY: Step 1: run_terminal_command: powershell -Command \"$os = Get-CimInstance Win32_OperatingSystem; [PSCustomObject]@{TotalGB = [math]::Round($os.TotalVisibleMemorySize/1MB, 2); FreeGB = [math]::Round($os.FreePhysicalMemory/1MB, 2)} | Format-Table -AutoSize\", Step 2: final_answer.\n"
                 "   * Always output clearly labelled strings or use `Select-Object` / `Format-Table` so downstream tool steps never choke on unlabelled raw numbers.\n"
                 "   * If you need exact syntax verification or verified templates for Windows administrative tasks (e.g., physical memory, top processes, services, listening ports), propose checking `lookup_technical_manuals` first or generate the command using verified PowerShell design principles.\n"
                  "- GENERAL KNOWLEDGE VS TERMINAL COMMANDS: If the user asks a conceptual, definition, or general knowledge question (e.g., 'what is nmap', 'what is DNS', 'explain active directory', 'how does TCP work', 'what is python') without asking to check, run, or list anything on this specific Windows machine, DO NOT run terminal commands! Propose: Step 1: final_answer.\n"
                  "- SEARCHING FOR FILES/FOLDERS: When the user asks if they have a folder or file named X across their machine (e.g. 'do i have any folder named gandharvam in my whole machine', 'find file test.txt'), propose EXACTLY: Step 1: run_terminal_command: dir /s /b C:\\*X* 2>nul, Step 2: final_answer. (For example, dir /s /b C:\\*gandharvam* 2>nul). DO NOT use dir /s /b \\ without the wildcard pattern!\n"
                  "- If the user asks to see, check, or list installed Ollama models, you MUST propose EXACTLY: Step 1: run_terminal_command: ollama list, Step 2: final_answer. DO NOT propose checking directory paths using dir or ls!\n"
                  "- ONLY include 'write_file' if the user explicitly asks to save/write output to a file. If the user only asks to check, list, or diagnose something without asking for a file, DO NOT include 'write_file' in the plan! Make the plan: Step 1: run_terminal_command: <command>, Step 2: final_answer.\n"
                "- Output each step on a NEW LINE (e.g.,\nStep 1: run_terminal_command: ...\nStep 2: final_answer).\n"
                "- Be precise, concise, and direct. Output ONLY the numbered step-by-step execution plan without any introductory chat.\n\n"
                f"{history_str}Current User Prompt: {query}"
            )
            
            try:
                planner_response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "system", "content": "You are a concise AI task structurer for Windows systems."}, {"role": "user", "content": planner_prompt}],
                    temperature=0.1
                )
                structured_query = planner_response.choices[0].message.content.strip()
                structured_query = re.sub(r'\s+(Step \d+:)', r'\n\1', structured_query)
                asyncio.run_coroutine_threadsafe(
                    q.put(("step", json.dumps({
                        'type': 'step', 
                        'content': {'observation': f"Structured Execution Plan:\n{structured_query}"}
                    }))), loop)
            except Exception as e_planner:
                structured_query = f"Step 1: final_answer: {query}"
                asyncio.run_coroutine_threadsafe(
                    q.put(("step", json.dumps({
                        'type': 'step', 
                        'content': {'observation': f"Planner fallback (using direct query): {str(e_planner)}"}
                    }))), loop)

            # Enforce Chain of Thought and OS intelligence
            intelligent_query = (
                "CRITICAL SYSTEM PROTOCOL:\n"
                "1. You are an autonomous AI cybersecurity/developer agent running on Windows. Ensure commands are Windows/PowerShell compatible.\n"
                "2. Follow the Execution Plan below step-by-step. Execute only what is asked inside <CURRENT_TASK>. IGNORE any past outputs inside <PAST_CONVERSATION_HISTORY>.\n"
                "3. GENERAL KNOWLEDGE / CONCEPTS: If the user is asking a conceptual/definition question (e.g. 'what is nmap', 'explain DNS', 'what does ping do') without asking to inspect or run things on this local machine, DO NOT call 'run_terminal_command' or 'lookup_technical_manuals'. Immediately call 'final_answer' with the full explanation right inside 'answer'!\n"
                "4. If the user explicitly asks to save/write output to a file, use 'write_file' with content 'LAST_STDOUT' to automatically save the last terminal output.\n"
                "5. FORMATTING RULE: When the user asks to list, check, or give a table of processes/folders/memory/files, execute the terminal command, read the STDOUT observation, and IMMEDIATELY call 'final_answer'. Inside 'final_answer', you MUST present the EXACT STDOUT data fully formatted (as a Markdown table, bulleted list, or exact code block). NEVER write generic summary sentences like 'The provided output is a list of...' or 'The directory listing shows...'. You must present the complete formatted data without summarizing or truncating!\n"
                "6. DO NOT repeat commands or loop after completing the required steps. Once your task is complete or you have gathered the required STDOUT, call 'final_answer' immediately to finish.\n"
                "7. STOP CONDITION: When you call 'run_terminal_command' or 'lookup_technical_manuals', you WILL receive the STDOUT/Observation in the next turn. Once you receive that observation, DO NOT run more commands or invent tools like 'response'. Call 'final_answer' immediately containing the detailed results/summary to end the loop cleanly.\n"
                "8. If a command returns an error or unexpected output, DO NOT run exploratory commands like 'dir' or 'ls'. Immediately call 'final_answer' with your explanation and any data you gathered to complete your turn cleanly.\n\n"
                f"{history_str}"
                "<CURRENT_TASK>\n"
                f"User Request: {query}\n\n"
                f"EXECUTION PLAN:\n{structured_query}\n"
                "</CURRENT_TASK>\n\n"
                "REMINDER: Execute ONLY the steps inside <CURRENT_TASK>. When summarizing or answering, present the full formatted data cleanly without generic summary lines!"
            )

            def _format_final_output(ans_text: str) -> str:
                from tools import last_terminal_output
                last_out = last_terminal_output.strip() if last_terminal_output else ""
                if not last_out:
                    return ans_text
                summary_phrases = [
                    "provided output is", "directory listing shows", "above output shows",
                    "output shows", "list of running processes", "shows the contents of",
                    "here is the output", "here is the directory", "summary of", "highest rom storage"
                ]
                is_generic_summary = any(p in ans_text.lower() for p in summary_phrases) or len(ans_text.strip().splitlines()) <= 4
                if is_generic_summary or (len(last_out) > 40 and last_out[:35].lower() not in ans_text.lower()):
                    if last_out in ans_text:
                        return ans_text
                    return f"{ans_text.strip()}\n\n### Exact Formatted Command Output:\n```text\n{last_out}\n```"
                return ans_text
            
            if AGENT_BACKEND == "deepagents":
                gen = agent.stream({"messages": [("user", intelligent_query)]}, config={"recursion_limit": 25})
                for chunk in gen:
                    if "model" in chunk and "messages" in chunk["model"]:
                        for msg in chunk["model"]["messages"]:
                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    tc_name = tc.get("name", tc.name if hasattr(tc, "name") else "")
                                    tc_args = tc.get("args", tc.arguments if hasattr(tc, "arguments") else {})
                                    if tc_name == "final_answer":
                                        answer_text = tc_args.get("answer", str(tc_args)) if isinstance(tc_args, dict) else str(tc_args)
                                        asyncio.run_coroutine_threadsafe(
                                            q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(answer_text)}))), loop)
                                        asyncio.run_coroutine_threadsafe(q.put(("done", json.dumps({'type': 'done'}))), loop)
                                        return
                                    else:
                                        asyncio.run_coroutine_threadsafe(
                                            q.put(("step", json.dumps({
                                                'type': 'step', 
                                                'content': {'thought': f'Executing tool ({AGENT_BACKEND})...', 'tool_calls': [{'name': tc_name, 'arguments': tc_args}]}
                                            }))), loop)
                            elif hasattr(msg, "content") and msg.content and not getattr(msg, "tool_calls", None):
                                raw_text = msg.content.strip()
                                if raw_text.startswith("```"):
                                    lines = raw_text.splitlines()
                                    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
                                        raw_text = "\n".join(lines[1:-1]).strip()
                                parsed_tool_name = None
                                parsed_tool_args = {}
                                try:
                                    safe_text = re.sub(r'\\(?![/u"\\bfnrt])', r'\\\\', raw_text)
                                    data = json.loads(safe_text)
                                    if isinstance(data, dict):
                                        if 'name' in data:
                                            parsed_tool_name = data['name']
                                            parsed_tool_args = data.get('arguments', data.get('args', {}))
                                        elif 'tool' in data:
                                            parsed_tool_name = data['tool']
                                            parsed_tool_args = data.get('arguments', data.get('args', {}))
                                        elif 'command' in data:
                                            parsed_tool_name = 'run_terminal_command'
                                            parsed_tool_args = {'command': data['command']}
                                        elif 'query' in data:
                                            parsed_tool_name = 'lookup_technical_manuals'
                                            parsed_tool_args = {'query': data['query']}
                                        elif 'answer' in data:
                                            parsed_tool_name = 'final_answer'
                                            parsed_tool_args = {'answer': data['answer']}
                                except Exception:
                                    pass

                                if not parsed_tool_name:
                                    m_cmd = re.search(r'"command"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
                                    if m_cmd:
                                        parsed_tool_name = 'run_terminal_command'
                                        parsed_tool_args = {'command': m_cmd.group(1).replace('\\\\', '\\')}
                                    else:
                                        m_query = re.search(r'"query"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
                                        if m_query:
                                            parsed_tool_name = 'lookup_technical_manuals'
                                            parsed_tool_args = {'query': m_query.group(1).replace('\\\\', '\\')}
                                        else:
                                            m_ans = re.search(r'"answer"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
                                            if m_ans:
                                                parsed_tool_name = 'final_answer'
                                                parsed_tool_args = {'answer': m_ans.group(1).replace('\\\\', '\\')}

                                if parsed_tool_name:
                                    if parsed_tool_name == "final_answer":
                                        answer_text = parsed_tool_args.get("answer", str(parsed_tool_args)) if isinstance(parsed_tool_args, dict) else str(parsed_tool_args)
                                        asyncio.run_coroutine_threadsafe(
                                            q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(answer_text)}))), loop)
                                        asyncio.run_coroutine_threadsafe(q.put(("done", json.dumps({'type': 'done'}))), loop)
                                        return
                                    else:
                                        asyncio.run_coroutine_threadsafe(
                                            q.put(("step", json.dumps({
                                                'type': 'step', 
                                                'content': {'thought': f'Executing tool ({parsed_tool_name})...', 'tool_calls': [{'name': parsed_tool_name, 'arguments': parsed_tool_args}]}
                                            }))), loop)
                                        tool_obj = next((t for t in tools if getattr(t, 'name', '') == parsed_tool_name), None)
                                        if tool_obj:
                                            try:
                                                obs = tool_obj._run(**parsed_tool_args) if hasattr(tool_obj, '_run') else tool_obj.run(parsed_tool_args)
                                            except Exception as e_run:
                                                obs = f"Error running {parsed_tool_name}: {str(e_run)}"
                                            asyncio.run_coroutine_threadsafe(
                                                q.put(("step", json.dumps({
                                                    'type': 'step', 
                                                    'content': {'observation': str(obs)}
                                                }))), loop)
                                            followup_msg = f"Tool '{parsed_tool_name}' returned Observation:\n{obs}\n\nCRITICAL INSTRUCTION: Present the exact data from the Observation above cleanly formatted (e.g. table or exact code block). DO NOT write generic summaries like 'The provided output is...'. Present the complete formatted data directly right now!"
                                            try:
                                                follow_resp = model.invoke([("user", intelligent_query), ("assistant", msg.content), ("user", followup_msg)])
                                                final_txt = getattr(follow_resp, 'content', str(follow_resp))
                                                # Check if follow_resp also emitted a final_answer json
                                                try:
                                                    f_data = json.loads(final_txt.strip())
                                                    if isinstance(f_data, dict) and ('answer' in f_data or f_data.get('name') == 'final_answer'):
                                                        final_txt = f_data.get('answer', f_data.get('arguments', {}).get('answer', final_txt))
                                                except Exception:
                                                    pass
                                                asyncio.run_coroutine_threadsafe(
                                                    q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(final_txt)}))), loop)
                                            except Exception as e_fol:
                                                asyncio.run_coroutine_threadsafe(
                                                    q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': f"Command Output:\n{obs}"}))), loop)
                                            asyncio.run_coroutine_threadsafe(q.put(("done", json.dumps({'type': 'done'}))), loop)
                                            return
                                else:
                                    asyncio.run_coroutine_threadsafe(
                                        q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(msg.content)}))), loop)
                                    asyncio.run_coroutine_threadsafe(q.put(("done", json.dumps({'type': 'done'}))), loop)
                                    return
                    elif "tools" in chunk and "messages" in chunk["tools"]:
                        for msg in chunk["tools"]["messages"]:
                            if hasattr(msg, "content") and msg.content:
                                asyncio.run_coroutine_threadsafe(
                                    q.put(("step", json.dumps({
                                        'type': 'step', 
                                        'content': {'observation': str(msg.content)}
                                    }))), loop)
            else:
                gen = agent.run(intelligent_query, stream=True)
                seen_step_ids = set()
                while True:
                    try:
                        step = next(gen)
                        step_id = id(step)
                        if hasattr(step, "tool_calls") and step.tool_calls and step_id not in seen_step_ids:
                            seen_step_ids.add(step_id)
                            for tc in step.tool_calls:
                                if tc.name == "final_answer":
                                    answer_text = tc.arguments.get("answer", str(tc.arguments)) if isinstance(tc.arguments, dict) else str(tc.arguments)
                                    asyncio.run_coroutine_threadsafe(
                                        q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(answer_text)}))), loop)
                                else:
                                    asyncio.run_coroutine_threadsafe(
                                        q.put(("step", json.dumps({
                                            'type': 'step', 
                                            'content': {'thought': f'Executing tool...', 'tool_calls': [{'name': tc.name, 'arguments': tc.arguments}]}
                                        }))), loop)
                        if hasattr(step, "observations") and step.observations:
                            asyncio.run_coroutine_threadsafe(
                                q.put(("step", json.dumps({
                                    'type': 'step', 
                                    'content': {'observation': str(step.observations)}
                                }))), loop)
                        if isinstance(step, str):
                            asyncio.run_coroutine_threadsafe(
                                q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(step)}))), loop)
                    except StopIteration as e:
                        final_result = e.value
                        if final_result:
                            asyncio.run_coroutine_threadsafe(
                                q.put(("final_answer", json.dumps({'type': 'final_answer', 'content': _format_final_output(str(final_result))}))), loop)
                        break
            asyncio.run_coroutine_threadsafe(q.put(("done", json.dumps({'type': 'done'}))), loop)
        except Exception as e:
            import traceback
            traceback.print_exc()
            asyncio.run_coroutine_threadsafe(
                q.put(("error", json.dumps({'type': 'error', 'content': str(e)}))), loop)

    # Run agent in background thread
    loop = asyncio.get_running_loop()
    threading.Thread(target=agent_thread, args=(loop, queue), daemon=True).start()

    try:
        while True:
            msg_type, data = await queue.get()
            if msg_type == "done":
                break
            elif msg_type == "error":
                yield f"data: {data}\n\n"
                break
            else:
                yield f"data: {data}\n\n"
    finally:
        approval_task.cancel()
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

async def direct_stream_generator(query: str, history: list[Message], model_name: str):
    import httpx
    import openai
    client = openai.AsyncOpenAI(
        base_url=f"http://{SERVER_IP}:{OLLAMA_PORT}/v1",
        api_key="ollama",
        http_client=httpx.AsyncClient(timeout=1200.0),
        max_retries=2
    )
    
    messages = []
    if history:
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": query})
    
    yield f"data: {json.dumps({'type': 'meta', 'model': model_name})}\n\n"
    
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            stream=True
        )
        async for chunk in response:
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        
    yield f"data: {json.dumps({'type': 'done'})}\n\n"

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    query = request.query
    history = request.history
    mode = request.mode
    auto_approve = request.auto_approve
    
    if mode == "direct":
        model_name = "dolphin-llama3:8b"
        return StreamingResponse(
            direct_stream_generator(query, history, model_name), 
            media_type="text/event-stream"
        )
    else:
        # Heuristic router
        coding_keywords = ["bash script", "python", "code", "write a script", "implement"]
        is_coding = any(keyword in query.lower() for keyword in coding_keywords)
    
        if is_coding:
            model_name = "qwen2.5-coder:7b-instruct-q8_0"
        else:
            model_name = "qwen2.5-coder:7b-instruct-q8_0"
            
        return StreamingResponse(
            agent_stream_generator(query, history, model_name, auto_approve), 
            media_type="text/event-stream"
        )

class ApprovalRequestPayload(BaseModel):
    task_id: str
    approved: bool

@app.post("/chat/approve")
async def chat_approve(request: ApprovalRequestPayload):
    if request.task_id in pending_approvals:
        pending_approvals[request.task_id]["approved"] = request.approved
        pending_approvals[request.task_id]["event"].set()
        return {"status": "ok"}
    return {"status": "not_found", "message": "Task not waiting for approval"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
