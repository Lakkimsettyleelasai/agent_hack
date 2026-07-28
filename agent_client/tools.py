from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from typing import Type, Optional, Set
from qdrant_client import QdrantClient
import requests
import os
import subprocess
from dotenv import load_dotenv
import threading
import platform
import re
import socket
import json

load_dotenv()
_parent_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_parent_env):
    load_dotenv(_parent_env, override=True)

# Global state for approvals
# Maps task_id -> {"command": str, "event": threading.Event(), "approved": bool}
pending_approvals = {}

SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1")
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))

# Auto-detect if remote SERVER_IP is reachable; if not, immediately fall back to localhost
if SERVER_IP != "127.0.0.1" and SERVER_IP != "localhost":
    try:
        with socket.create_connection((SERVER_IP, int(OLLAMA_PORT)), timeout=3.0):
            pass
    except Exception:
        SERVER_IP = "127.0.0.1"

def _search_local_git_manuals(query: str) -> list[str]:
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_pipeline", "raw_markdown_docs")
    if not os.path.exists(docs_dir):
        return []
    try:
        import glob
        query_lower = query.lower()
        keywords = [kw for kw in re.findall(r'\w+', query_lower) if len(kw) > 2 and kw not in ('the', 'and', 'for', 'how', 'what', 'check', 'get', 'use', 'with')]
        if not keywords:
            return []
        
        is_win = any(k in query_lower for k in ["windows", "powershell", "ram", "memory", "cpu", "process", "service", "active directory"])
        md_files = glob.glob(os.path.join(docs_dir, "*.md"))
        scored_files = []
        for filepath in md_files:
            filename = os.path.basename(filepath).lower()
            file_score = 0
            for kw in keywords:
                if kw in filename:
                    file_score += 5
            if is_win and ("windows" in filename or "powershell" in filename or "useful-commands" in filename):
                file_score += 3
            if file_score > 0:
                scored_files.append((file_score, filepath))
        
        if not scored_files:
            subset = [f for f in md_files if any(sub in os.path.basename(f).lower() for sub in ["windows", "useful", "commands", "privilege", "enumeration", "post"])][:50]
            for filepath in subset:
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                    hits = sum(1 for kw in keywords if kw in text.lower())
                    if hits >= 2:
                        scored_files.append((hits, filepath))
                except Exception:
                    pass

        scored_files.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, filepath in scored_files[:3]:
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                paragraphs = content.split("\n\n")
                relevant_paras = [p for p in paragraphs if any(kw in p.lower() for kw in keywords)]
                if not relevant_paras:
                    relevant_paras = paragraphs[:3]
                snippet = "\n\n".join(relevant_paras[:3])
                if len(snippet) > 2000:
                    snippet = snippet[:2000] + "...\n[Truncated]"
                results.append(f"[Git Manual Reference | Source: {os.path.basename(filepath)}]\n{snippet}")
            except Exception:
                pass
        return results
    except Exception:
        return []

class LookupTechnicalManualsInput(BaseModel):
    query: str = Field(description="The search query to look up in the technical manuals.")

class LookupTechnicalManualsTool(BaseTool):
    name: str = "lookup_technical_manuals"
    description: str = "Searches the Git repository manuals (HackTricks, PayloadsAllTheThings, GTFOBins) AND remote Qdrant database for verified Windows/PowerShell commands, system diagnostics, and security manuals. Pass the search query as input."
    args_schema: Type[BaseModel] = LookupTechnicalManualsInput
    past_queries: Set[str] = Field(default_factory=set)

    def _run(self, query: str) -> str:
        if query.lower().strip() in self.past_queries:
            return "SYSTEM WARNING: You have already executed this exact search query. DO NOT search for this again. Use your existing knowledge to provide your final_answer."
        self.past_queries.add(query.lower().strip())

        # Strip task/payload prefixes before computing embedding
        clean_query = re.sub(r'^(?:RAG-[A-Z0-9\-]+|Step \d+:|lookup_technical_manuals:|\#\d+|\d+[\.\)])\s*[-:]*\s*', '', query.strip(), flags=re.IGNORECASE).strip()
        if not clean_query:
            clean_query = query.strip()

        local_results = _search_local_git_manuals(clean_query)

        # Generate embedding using nomic-embed-text via Ollama
        url = f"http://{SERVER_IP}:{OLLAMA_PORT}/api/embeddings"
        try:
            resp = requests.post(url, json={"model": "nomic-embed-text", "prompt": clean_query}, timeout=30.0)
            if resp.status_code == 200:
                embedding = resp.json().get("embedding")
            else:
                if local_results:
                    return "\n\n---\n\n".join(local_results)
                return f"Error generating embedding (`{url}` returned status {resp.status_code}): {resp.text}"
        except Exception as e:
            if local_results:
                return "\n\n---\n\n".join(local_results)
            return f"Error connecting to Ollama embedding service (`{url}`): {str(e)}"

        # Search Qdrant
        try:
            client = QdrantClient(host=SERVER_IP, port=QDRANT_PORT, timeout=5.0)
            search_result = client.query_points(
                collection_name="technical_manuals",
                query=embedding,
                limit=25
            ).points
        except Exception as e:
            if local_results:
                return "\n\n---\n\n".join(local_results)
            return f"Error connecting to Qdrant vector database (`http://{SERVER_IP}:{QDRANT_PORT}`): {str(e)}. Please ensure Qdrant is running on the server (`docker start qdrant`)."

        if not search_result and not local_results:
            return "No relevant documents found."

        # Re-rank results using query keyword alignment to filter out mismatched OS/domain chunks
        query_lower = clean_query.lower()
        query_keywords = set(re.findall(r'\w+', query_lower))
        
        # Define OS specificity checks
        is_windows_query = any(k in query_lower for k in ["windows", "active directory", "ad", "powershell", "kerberos", "ntlm", "domain controller"])
        is_linux_query = any(k in query_lower for k in ["linux", "bash", "linpeas", "sudo", "cron", "systemd", "root"])

        scored_hits = []
        for hit in search_result:
            text = hit.payload.get("text", "")
            source = hit.payload.get("source", "Unknown Source")
            base_score = getattr(hit, "score", 0.0)
            
            # Keyword alignment boost
            content_lower = (source + " " + text).lower()
            keyword_matches = sum(1 for kw in query_keywords if len(kw) > 3 and kw in content_lower)
            adjusted_score = base_score + (keyword_matches * 0.05)

            # OS mismatch penalty and match boost
            if is_windows_query:
                if any(non_win in source.lower() for non_win in ["linux", "macos", "ios", "android", "darwin", "apple"]):
                    adjusted_score -= 0.4
                if any(win_kw in source.lower() for win_kw in ["windows", "active-directory", "active_directory", "powershell", "mssql"]):
                    adjusted_score += 0.15
            elif is_linux_query:
                if any(non_lin in source.lower() for non_lin in ["windows", "macos", "ios", "android", "active-directory"]):
                    adjusted_score -= 0.4

            scored_hits.append((adjusted_score, hit))

        scored_hits.sort(key=lambda x: x[0], reverse=True)
        top_hits = [hit for _, hit in scored_hits[:4]]

        results = list(local_results)
        for i, hit in enumerate(top_hits):
            text = hit.payload.get("text", "")
            source = hit.payload.get("source", "Unknown Source")
            results.append(f"[Relevant Manual Chunk {i+1} | Source: {source}]\n{text}")

        return "\n\n---\n\n".join(results)

class ReadWebpageInput(BaseModel):
    url: str = Field(description="The URL of the webpage to read.")

class ReadWebpageTool(BaseTool):
    name: str = "read_webpage"
    description: str = "Downloads and extracts readable text from a webpage URL. Use this to read the full content of an article after finding it with web_search."
    args_schema: Type[BaseModel] = ReadWebpageInput
    task_id: Optional[str] = None
    auto_approve: bool = False

    def _run(self, url: str) -> str:
        if self.task_id:
            needs_approval = not self.auto_approve
            if needs_approval:
                event = threading.Event()
                pending_approvals[self.task_id] = {
                    "command": f"Read webpage: {url}",
                    "dir": os.getcwd(),
                    "event": event,
                    "approved": False,
                    "notified": False
                }
                event.wait()
                
                approved = pending_approvals[self.task_id]["approved"]
                del pending_approvals[self.task_id]
                
                if not approved:
                    return "SYSTEM ERROR: The user DENIED permission to read this webpage."

        try:
            from bs4 import BeautifulSoup
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.extract()
                
            text = soup.get_text(separator=' ', strip=True)
            import re
            text = re.sub(r'\s+', ' ', text).strip()
            
            if len(text) > 10000:
                text = text[:10000] + "\n...[TRUNCATED. Document too long]..."
                
            return text
        except Exception as e:
            return f"Failed to read webpage: {str(e)}"

class RunTerminalCommandInput(BaseModel):
    command: str = Field(description="The shell command to execute.")

class RunTerminalCommandTool(BaseTool):
    name: str = "run_terminal_command"
    description: str = f"Executes a terminal/shell command on the local system. SYSTEM CONTEXT: You are currently running on a {platform.system()} operating system. Ensure you use the correct commands for this OS (e.g. use 'dir' instead of 'ls' on Windows). Use this tool to inspect the system, run scripts, navigate the filesystem, or perform tasks. Maintains the current working directory between calls."
    args_schema: Type[BaseModel] = RunTerminalCommandInput
    task_id: Optional[str] = None
    auto_approve: bool = False
    cwd: str = Field(default_factory=os.getcwd)

    def _run(self, command: str) -> str:
        global last_terminal_output
        # Block destructive disk/file wiping commands without blocking PowerShell cmdlets like Format-Table / Format-List
        cmd_l = command.lower()
        if "rm -rf" in cmd_l or "del /s" in cmd_l or re.search(r'\bformat\s+[a-z]:', cmd_l):
            return f"SYSTEM ERROR: Command '{command}' is blocked for safety reasons."
                
        if command.strip().startswith("cd "):
            target_dir = command.strip()[3:].strip()
            target_dir = target_dir.strip("\"'")
            new_dir = os.path.abspath(os.path.join(self.cwd, target_dir))
            if os.path.isdir(new_dir):
                self.cwd = new_dir
                return f"Changed directory to {self.cwd}"
            else:
                return f"Error: Directory '{target_dir}' does not exist."

        # Intercept and auto-bridge model listing checks BEFORE approval checks
        cmd_strip = command.strip().lower()
        if cmd_strip in ["ollama list", "ollama --list", "ollama -l"] or any(k in cmd_strip for k in ["ollama_models", "ollama\\models", "ollama/models"]) or (any(k in cmd_strip for k in ["dir ", "ls ", "get-childitem "]) and ("ollama" in cmd_strip or "openluna" in cmd_strip or "windows kits" in cmd_strip)):
            local_out = ""
            try:
                local_out = subprocess.run("ollama list", shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:
                local_out = "No local ollama CLI found."
            remote_out = ""
            remote_ip = os.getenv("SERVER_IP", SERVER_IP)
            if remote_ip not in ("127.0.0.1", "localhost"):
                try:
                    import urllib.request
                    req = urllib.request.urlopen(f"http://{remote_ip}:{OLLAMA_PORT}/api/tags", timeout=10.0)
                    models = [m['name'] for m in json.loads(req.read().decode())['models']]
                    remote_out = "\n".join([f"{m} (Remote on {remote_ip})" for m in models])
                except Exception as err:
                    remote_out = f"Could not fetch tags from remote {remote_ip}:{OLLAMA_PORT} ({err})"
            res_str = f"OLLAMA MODELS AVAILABLE:\n--- Local Machine (127.0.0.1) ---\n{local_out if local_out else 'No models found locally.'}"
            if remote_out:
                res_str += f"\n\n--- Remote Server ({remote_ip}:{OLLAMA_PORT}) ---\n{remote_out}"
            last_terminal_output = res_str
            return res_str

        if not self.auto_approve:
            needs_approval = True
            cmd_lower = command.lower()
            if hasattr(self, 'task_id') and self.task_id and self.task_id in pending_approvals and pending_approvals[self.task_id].get("approved", False):
                needs_approval = False
            else:
                # Check dangerous keywords as distinct command tokens / word boundaries (exclude 'format' so Format-Table/Format-List works)
                dangerous_tokens = ["del", "rm", "rmdir", "rd", "erase", "curl", "wget", "invoke-webrequest"]
                tokens = re.findall(r'\b[a-z0-9_-]+\b|>', cmd_lower)
                if not any(token in dangerous_tokens or token == '>' for token in tokens) and not re.search(r'\bformat\s+[a-z]:', cmd_lower) and not (">" in command or ">>" in command):
                    needs_approval = False
        else:
            needs_approval = False

        if needs_approval:
            event = threading.Event()
            pending_approvals[self.task_id] = {
                "command": command,
                "dir": self.cwd,
                "event": event,
                "approved": False,
                "notified": False
            }
            event.wait()
            
            approved = pending_approvals[self.task_id]["approved"]
            del pending_approvals[self.task_id]
            
            if not approved:
                return "SYSTEM ERROR: The user DENIED permission to run this command. You must think of an alternative or explain why you cannot proceed."

        try:
            # Intercept hallucinated conversational commands (e.g. 'response', 'answer') before executing on OS
            if command.strip().lower() in ["response", "answer", "summary", "done", "exit", "quit", "stop"] or command.strip().lower().startswith(("response ", "answer ")):
                return "OBSERVATION: Data gathering is already complete! Do not run more terminal commands. Immediately call the final_answer tool with your summary right now."

            exec_command = command
            if platform.system() == "Windows":
                # Repair common hallucinated taskkill/tasklist CPU check syntax errors
                if "taskkill" in command.lower() and ("cpu" in command.lower() or "/fo" in command.lower()):
                    command = "Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 6"
                elif "tasklist" in command.lower() and "sort" in command.lower():
                    command = "Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 6"

                # Translate common Unix pipelines to Windows PowerShell equivalents
                translated_cmd = re.sub(r'\|\s*head\s+(?:-n\s*)?(\d+)', r'| Select-Object -First \1', command, flags=re.IGNORECASE)
                translated_cmd = re.sub(r'\|\s*tail\s+(?:-n\s*)?(\d+)', r'| Select-Object -Last \1', translated_cmd, flags=re.IGNORECASE)
                translated_cmd = re.sub(r'\|\s*grep\s+(?:-i\s+)?["\']?([^"\'|\n]+)["\']?', r'| Select-String -Pattern "\1"', translated_cmd, flags=re.IGNORECASE)
                
                cmd_stripped = translated_cmd.strip()
                exec_command = translated_cmd
                use_shell = True
                if not cmd_stripped.lower().startswith(("powershell", "pwsh", "cmd ", "cmd.exe")):
                    ps_indicators = [
                        "get-", "set-", "new-", "remove-", "start-", "stop-", "test-",
                        "select-object", "sort-object", "where-object", "measure-object",
                        "format-table", "format-list", "out-string", "-property",
                        "-descending", "invoke-", "$_.", "get-process", "get-service",
                        "head", "tail", "grep", "cat ", "ls ", "awk", "sed", "| head", "| tail", "| grep", "taskkill", "select-string", "get-wmiobject", "get-ciminstance", "$last_stdout"
                    ]
                    if any(indicator in cmd_stripped.lower() for indicator in ps_indicators) or "|" in cmd_stripped or ";" in cmd_stripped:
                        exec_command = ["powershell", "-NoProfile", "-Command", translated_cmd]
                        use_shell = False

            kwargs = {
                'shell': use_shell if isinstance(exec_command, str) else False,
                'cwd': self.cwd,
                'capture_output': True,
                'text': True,
                'timeout': 90
            }
            
            # Guard against dumping the entire C: drive with no filter which times out
            if isinstance(exec_command, str) and re.match(r'^(?:dir\s+/s\s+/b|dir\s+/b\s+/s)\s+(?:[c-z]:\\|\\|/\s*)$', exec_command.strip(), re.IGNORECASE):
                return "STDOUT:\nError: Recursive listing of entire root drive without a file/folder filter (`dir /s /b \\`) is blocked due to 100+ GB volume size. Please specify a filter pattern like `dir /s /b C:\\*foldername* 2>nul` or check inside `C:\\Users`."

            # Prevent the black command prompt window from flashing on Windows
            if platform.system() == "Windows":
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(exec_command, **kwargs)
            
            # If command failed under Windows cmd.exe, automatically retry with PowerShell
            if platform.system() == "Windows" and result.returncode != 0 and isinstance(exec_command, str) and exec_command == command and not command.strip().lower().startswith(("powershell", "pwsh")):
                ps_retry_cmd = ["powershell", "-NoProfile", "-Command", command]
                ps_kwargs = dict(kwargs)
                ps_kwargs['shell'] = False
                ps_result = subprocess.run(ps_retry_cmd, **ps_kwargs)
                if ps_result.returncode == 0 or (ps_result.stdout.strip() and not ps_result.stderr.strip()):
                    result = ps_result

            output_parts = []
            if result.stdout.strip():
                output_parts.append(f"STDOUT:\n{result.stdout.strip()}")
            if result.stderr.strip():
                output_parts.append(f"STDERR (Exit Code {result.returncode}):\n{result.stderr.strip()}")
                
            output = "\n\n".join(output_parts)
            
            if not output.strip():
                if result.returncode != 0:
                    output = f"Command failed with exit code {result.returncode} and no output."
                else:
                    output = "Command executed successfully with no output."
                
            if "netstat" in command.lower() and len(output.splitlines()) > 30:
                lines = output.splitlines()
                output = "\n".join(lines[:30]) + f"\n\n...[NETSTAT TRUNCATED: Showing top 30 active listening ports/connections out of {len(lines)} total lines to keep context clean]..."
            elif len(output) > 3000:
                head = output[:1500]
                tail = output[-1500:]
                output = f"{head}\n\n...[OUTPUT TRUNCATED: {len(output) - 3000} characters hidden to prevent context overflow]...\n\n{tail}"
            
            last_terminal_output = output
            return output
        except subprocess.TimeoutExpired:
            return "Error: Command timed out after 30 seconds. If this is a long running process, consider running it in the background."
        except Exception as e:
            return f"Error executing command: {str(e)}"

last_terminal_output = ""

class WriteFileInput(BaseModel):
    file_path: str = Field(..., description="The absolute or relative path to the file to write.")
    content: str = Field(default="LAST_STDOUT", description="The text content to write into the file. Tip: Pass 'LAST_STDOUT' to automatically write the exact full STDOUT from your previous run_terminal_command without having to copy-paste it all into JSON!")

class WriteFileTool(BaseTool):
    name: str = "write_file"
    description: str = "Write or overwrite a file with specific content. Pass 'file_path' and 'content' (or pass 'LAST_STDOUT' as content to save the last terminal output directly without huge JSON blocks)."
    args_schema: Type[BaseModel] = WriteFileInput
    task_id: Optional[str] = None
    auto_approve: bool = False
    cwd: str = Field(default_factory=os.getcwd)

    def _run(self, file_path: str = "", content: str = "LAST_STDOUT", **kwargs) -> str:
        global last_terminal_output
        actual_path = file_path or kwargs.get("path") or kwargs.get("filepath")
        if not actual_path:
            return "Error: You must specify file_path."
        file_path = actual_path

        if content in ["LAST_STDOUT", "STDOUT", "<STDOUT from Step 1>", "<STDOUT>", "<STDOUT text>", ""] or (last_terminal_output and content.strip() in last_terminal_output):
            if last_terminal_output:
                content = last_terminal_output
            elif content in ["LAST_STDOUT", "STDOUT", "<STDOUT from Step 1>", "<STDOUT>", "<STDOUT text>", ""]:
                content = "No previous command output recorded."

        if self.task_id:
            needs_approval = not self.auto_approve
            if needs_approval:
                event = threading.Event()
                mock_command = f"Write {len(content)} characters to '{file_path}'"
                pending_approvals[self.task_id] = {
                    "command": mock_command,
                    "dir": self.cwd,
                    "event": event,
                    "approved": False,
                    "notified": False
                }
                event.wait()
                
                approved = pending_approvals[self.task_id]["approved"]
                del pending_approvals[self.task_id]
                
                if not approved:
                    return "SYSTEM ERROR: The user DENIED permission to write to this file. You must think of an alternative or explain why you cannot proceed."

        try:
            if "%USERPROFILE%" in file_path:
                file_path = file_path.replace("%USERPROFILE%", os.environ.get("USERPROFILE", ""))
            
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote to {file_path}"
        except Exception as e:
            return f"Failed to write file: {str(e)}"

class FinalAnswerInput(BaseModel):
    answer: str = Field(description="The final answer or explanation to present to the user after completing all tasks.")

class FinalAnswerTool(BaseTool):
    name: str = "final_answer"
    description: str = "Provides the final answer and summary to the user. Always call this tool when your task is complete."
    args_schema: Type[BaseModel] = FinalAnswerInput

    def _run(self, answer: str) -> str:
        return answer
