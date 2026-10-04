import sys
import subprocess
import re
import platform
from mcp.server.fastmcp import FastMCP

# Create a FastMCP server instance
mcp = FastMCP("Windows-Terminal-Agent")

@mcp.tool()
def run_windows_command(command: str) -> str:
    """
    Executes a Windows command via CMD or PowerShell.
    Automatically handles smart conversion of Unix pipelines to PowerShell (like | grep, | head) 
    and applies strict safeguards against massive root directory scans.
    """
    try:
        # Replicate the core logic of our RunTerminalCommandTool
        exec_command = command
        use_shell = True
        
        if platform.system() == "Windows":
            # Translate common Unix pipelines to Windows PowerShell equivalents
            translated_cmd = re.sub(r'\|\s*head\s+(?:-n\s*)?(\d+)', r'| Select-Object -First \1', command, flags=re.IGNORECASE)
            translated_cmd = re.sub(r'\|\s*tail\s+(?:-n\s*)?(\d+)', r'| Select-Object -Last \1', translated_cmd, flags=re.IGNORECASE)
            translated_cmd = re.sub(r'\|\s*grep\s+(?:-i\s+)?["\']?([^"\'|\n]+)["\']?', r'| Select-String -Pattern "\1"', translated_cmd, flags=re.IGNORECASE)
            
            cmd_stripped = translated_cmd.strip()
            exec_command = translated_cmd
            
            if not cmd_stripped.lower().startswith(("powershell", "pwsh", "cmd ", "cmd.exe")):
                ps_indicators = [
                    "get-", "set-", "new-", "remove-", "start-", "stop-", "test-",
                    "select-object", "sort-object", "where-object", "measure-object",
                    "format-table", "format-list", "out-string", "-property",
                    "-descending", "invoke-", "$_.", "get-process", "get-service",
                    "head", "tail", "grep", "cat ", "ls ", "awk", "sed", "| head", "| tail", "| grep", "taskkill", "select-string", "get-wmiobject", "get-ciminstance"
                ]
                if any(indicator in cmd_stripped.lower() for indicator in ps_indicators) or "|" in cmd_stripped or ";" in cmd_stripped:
                    exec_command = ["powershell", "-NoProfile", "-Command", translated_cmd]
                    use_shell = False

        kwargs = {
            'shell': use_shell if isinstance(exec_command, str) else False,
            'capture_output': True,
            'text': True,
            'timeout': 90
        }
        
        # Guard against dumping the entire C: drive with no filter
        if isinstance(exec_command, str) and re.match(r'^(?:dir\s+/s\s+/b|dir\s+/b\s+/s)\s+(?:[c-z]:\\|\\|/\s*)$', exec_command.strip(), re.IGNORECASE):
            return "STDOUT:\nError: Recursive listing of entire root drive without a file/folder filter (`dir /s /b \\`) is blocked due to 100+ GB volume size. Please specify a filter pattern like `dir /s /b C:\\*foldername* 2>nul`."

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
                
        if len(output) > 3000:
            head = output[:1500]
            tail = output[-1500:]
            output = f"{head}\n\n...[OUTPUT TRUNCATED: {len(output) - 3000} characters hidden to prevent context overflow]...\n\n{tail}"
        
        return output
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 90 seconds. If this is a long running process, consider running it in the background."
    except Exception as e:
        return f"Error executing command: {str(e)}"

if __name__ == "__main__":
    # Start the MCP server using standard input/output streams
    mcp.run()
