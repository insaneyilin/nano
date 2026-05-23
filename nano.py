#!/usr/bin/env python3
import json, os, platform, subprocess, sys, threading, time
from urllib.request import Request, urlopen

try:
    import readline
except ImportError:
    pass
else:
    readline.parse_and_bind("\\C-l: clear-screen")

PROVIDER = os.getenv("NANO_PROVIDER", "openai").lower()
API = "https://api.openai.com/v1/responses"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5") if PROVIDER == "openai" else os.getenv("OLLAMA_MODEL", "qwen3.5:0.8b")
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "true").lower()
MAX_STEPS = int(os.getenv("NANO_MAX_STEPS", "200"))
APPROVE_ALL = os.getenv("NANO_APPROVE", "").lower() == "all"
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "venv"}
SESSIONS = os.path.expanduser("~/.nano_sessions.json")
CWD = os.getcwd()
_TTY = sys.stderr.isatty()

def _color(code, text): return f"\033[{code}m{text}\033[0m" if _TTY else text

def _spinner(done, frames="-\\|/"):
    index = 0
    while not done.wait(0.1):
        print(f"\r  {_color(90, frames[index % len(frames)] + ' thinking')}", end="", file=sys.stderr, flush=True)
        index += 1
    print("\r             \r", end="", file=sys.stderr, flush=True)

def find_files(roots, names, limit=40):
    home, found = os.path.expanduser("~"), []
    for root in map(os.path.expanduser, roots):
        if not os.path.isdir(root): continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in (f for f in files if f.lower() in names):
                path = os.path.abspath(os.path.join(base, name))
                found.append("~" + path[len(home):] if path.startswith(home + os.sep) else os.path.relpath(path))
                if len(found) >= limit:
                    return ", ".join(sorted(dict.fromkeys(found)))
    return ", ".join(sorted(dict.fromkeys(found))) or "none"

def require_api_key():
    if PROVIDER == "openai" and not os.getenv("OPENAI_API_KEY"): sys.exit("set OPENAI_API_KEY")

SYSTEM = f"""You are Nano, a general-purpose shell agent with one tool: execute_shell.
Use it to inspect, edit, install, test, search, automate, and answer.
Be concise, tenacious, and relentlessly useful. Keep taking shell steps until done or blocked.
Output short plain-text snippets optimized for terminal reading; no markdown rendering or syntax highlighting.
Never run destructive commands unless explicitly requested.
cwd: {CWD}
platform: {platform.platform()}
python: {sys.version.split()[0]}
shell: {os.getenv("SHELL", "")}
Important docs (read as needed): {find_files([CWD], {"claude.md", "agent.md", "agents.md", "readme.md"})}
Important skill files (read as needed): {find_files([".claude/skills", "~/.claude/skills", "~/.codex/skills", "~/.codex/plugins"], {"skill.md", "skills.md"})}
"""

TOOL_PARAMETERS = {"type": "object", "properties": {
        "command": {"type": "string"},
        "description": {"type": "string", "description": "Why this command is useful right now, in 5-10 words."},
        "cwd": {"type": ["string", "null"]},
        "timeout": {"type": "integer"},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
    }, "required": ["command", "description"], "additionalProperties": False}
TOOL_DESCRIPTION = "Run a shell command with inherited environment."
OPENAI_TOOL = {"type": "function", "name": "execute_shell", "description": TOOL_DESCRIPTION, "parameters": TOOL_PARAMETERS}
OLLAMA_TOOL = {"type": "function", "function": {"name": "execute_shell", "description": TOOL_DESCRIPTION, "parameters": TOOL_PARAMETERS}}

def approve(args):
    global APPROVE_ALL
    print(f"\n{_color(90, '# ' + args.get('description', 'No description'))}", file=sys.stderr)
    print(f"{_color(32, '$ ' + args.get('command', ''))}", file=sys.stderr)
    for key in ("cwd", "timeout", "env"):
        if args.get(key) not in (None, "", {}):
            print(f"{_color(90, f'{key}: {args[key]}')}", file=sys.stderr)
    if APPROVE_ALL: return True
    try:
        choice = input(f"Approve? {_color(32,'[y] Approve')}  {_color(33,'[a] Approve All')}  {_color(31,'[n] Deny')}: ").strip().lower()
    except EOFError: return False
    if choice in ("a", "all"):
        APPROVE_ALL = True; return True
    return choice in ("y", "yes")

def execute_shell(command, description=None, cwd=None, timeout=60, env=None):
    try:
        process = subprocess.run(command, shell=True, cwd=os.path.abspath(cwd or CWD),
                                 env={**os.environ, **(env or {})}, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=timeout)
        return f"$ {command}\nexit {process.returncode}\n{process.stdout}"[-12000:]
    except subprocess.TimeoutExpired as error:
        return f"$ {command}\ntimeout after {timeout}s\n{error.stdout or ''}"[-12000:]
    except Exception as error:
        return f"{type(error).__name__}: {error}"

def run_tool(name, arguments):
    if name != "execute_shell":
        return "unknown tool"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as error:
            return f"bad arguments: {error}"
    if not isinstance(arguments, dict):
        return "bad arguments: expected object"
    if not 5 <= len(arguments.get("description", "").split()) <= 10:
        return "bad arguments: description must be 5-10 words"
    return execute_shell(**arguments) if approve(arguments) else _color(31, "denied by user")

def post_json(url, body, headers=None):
    request = Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
    with urlopen(request) as api_response:
        return json.load(api_response)

def spin(call):
    done = threading.Event() if _TTY else None
    thread = threading.Thread(target=_spinner, args=(done,), daemon=True) if done else None
    if thread: thread.start()
    try: return call()
    finally:
        if thread:
            done.set(); thread.join()

def openai_request(payload, previous=None):
    body = {"model": MODEL, "instructions": SYSTEM, "tools": [OPENAI_TOOL], "input": payload}
    if previous: body["previous_response_id"] = previous
    headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"}
    return spin(lambda: post_json(API, body, headers))

def ollama_request(messages):
    think = OLLAMA_THINK if OLLAMA_THINK in ("high", "medium", "low") else OLLAMA_THINK not in ("0", "false", "no", "off")
    body = {"model": MODEL, "messages": messages, "tools": [OLLAMA_TOOL], "stream": False, "think": think}
    return spin(lambda: post_json(f"{OLLAMA_HOST}/api/chat", body))

def openai_text(response):
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )

def run_openai(prompt, previous=None):
    response = openai_request(prompt, previous)
    for _ in range(MAX_STEPS):
        calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
        if not calls:
            return openai_text(response), response["id"]
        outputs = [{"type": "function_call_output", "call_id": c["call_id"], "output": run_tool(c.get("name"), c.get("arguments") or "{}")} for c in calls]
        response = openai_request(outputs, response["id"])
    return "stopped: too many tool calls", response["id"]

def run_ollama(prompt, previous=None):
    messages = previous[:] if isinstance(previous, list) else [{"role": "system", "content": SYSTEM}]
    messages.append({"role": "user", "content": prompt})
    for _ in range(MAX_STEPS):
        message = ollama_request(messages).get("message", {})
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content", ""), messages
        for call in calls:
            function = call.get("function", {})
            messages.append({"role": "tool", "tool_name": function.get("name"), "content": run_tool(function.get("name"), function.get("arguments") or {})})
    return "stopped: too many tool calls", messages

def run(prompt, previous=None): return run_ollama(prompt, previous) if PROVIDER == "ollama" else run_openai(prompt, previous)

def load_sessions():
    try: return json.load(open(SESSIONS))
    except (FileNotFoundError, json.JSONDecodeError): return []

def matching_sessions(limit=None):
    sessions = [s for s in load_sessions() if s.get("cwd") == CWD and s.get("provider", "openai") == PROVIDER]
    return sessions[-limit:] if limit else sessions

def save_session(response_id, label):
    sessions = [s for s in load_sessions() if not (s.get("label") == label and s.get("cwd") == CWD and s.get("provider", "openai") == PROVIDER)]
    sessions.append({"id": response_id, "label": label[:80], "cwd": CWD, "provider": PROVIDER, "model": MODEL, "ts": int(time.time())})
    json.dump(sessions[-50:], open(SESSIONS, "w"))

def pick_session():
    sessions = matching_sessions(10)
    if not sessions: sys.exit("no sessions in this directory")
    for index, session in enumerate(reversed(sessions)):
        age = int(time.time()) - session.get("ts", 0)
        label = f"{age//60}m" if age < 3600 else f"{age//3600}h" if age < 86400 else f"{age//86400}d"
        print(f"  {_color(90, str(index))}  {session.get('label', '')}  {_color(90, label + ' ago')}")
    try:
        choice = input(f"{_color(1,'nano')}{_color(90,'#')} ").strip()
        return sessions[-(int(choice) + 1)]
    except (EOFError, KeyboardInterrupt):
        print(); sys.exit(0)
    except (ValueError, IndexError): sys.exit("invalid session")

def resume(flag):
    if flag == "-s": session = pick_session()
    elif flag == "-c":
        sessions = matching_sessions()
        if not sessions: sys.exit("no sessions in this directory")
        session = sessions[-1]
    else: return None, None
    action = "resuming" if flag == "-s" else "continuing"
    print(_color(90, f"{action}: {session['label']}"))
    return session["id"], session["label"]

def repl(previous=None, label=None):
    print(_color(1, "nano") + " repl " + _color(90, "(:q quit, :reset reset)"))
    while True:
        try:
            prompt = input(_color(36, "nano > ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not prompt: continue
        if prompt.lower() in (":q", "quit", "exit"): return
        if prompt.lower() in (":reset", "reset"):
            previous, label = None, None
            print(_color(90, "reset")); continue
        answer, previous = run(prompt, previous)
        label = label or prompt
        save_session(previous, label)
        print(answer)

def main():
    require_api_key()
    args = sys.argv[1:]
    flag = args.pop(0) if args and args[0] in ("-c", "-s") else None
    previous, label = resume(flag)
    prompt = " ".join(args)
    if not prompt:
        return repl(previous, label)
    answer, response_id = run(prompt, previous)
    save_session(response_id, label or prompt)
    print(answer)

if __name__ == "__main__":
    main()
