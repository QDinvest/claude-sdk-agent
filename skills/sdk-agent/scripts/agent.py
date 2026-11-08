"""sdk-agent: a terminal coding agent built on the Claude Agent SDK.

Works in any folder or repo: it reads, edits and creates files there, and asks before
running commands. It bills to the API key in ANTHROPIC_API_KEY. The chat for each folder
is remembered, so running it again in the same folder continues where you left off.

    python agent.py                          # work in the current folder, continue its chat
    python agent.py --dir path/to/repo       # work in another folder
    python agent.py --new                    # start a fresh chat in this folder
    python agent.py --model claude-opus-5-5 --budget 3
"""
import argparse
import json
import os
import re
import shutil
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    get_session_messages,
    list_sessions,
)

SESSIONS_FILE = Path.home() / ".claude-agent-sessions.json"

# Colors. Set NO_COLOR=1 to turn them off.
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
if USE_COLOR and os.name == "nt":
    os.system("")  # switches on color support in the Windows console


def c(code: str) -> str:
    return f"\033[{code}m" if USE_COLOR else ""


RESET, BOLD, DIM = c("0"), c("1"), c("2")
GREEN, CYAN, YELLOW, RED, ORANGE = c("32"), c("36"), c("33"), c("31"), c("38;5;209")


def style(text: str) -> str:
    """Show Claude's **bold** and `code` markdown in color."""
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: BOLD + m.group(1) + RESET, text)
    return re.sub(r"`([^`\n]+)`", lambda m: CYAN + m.group(1) + RESET, text)


try:
    sys.stdout.reconfigure(errors="replace")  # never crash on a character the console can't show
except AttributeError:
    pass
WIDTH = max(40, min(shutil.get_terminal_size((90, 20)).columns, 100))


def rule(label: str) -> str:
    """A dim line across the terminal with a label in it: ── label ─────"""
    visible = len(re.sub(r"\033\[[\d;]*m", "", label))
    return f"{DIM}── {label} {'─' * max(2, WIDTH - visible - 4)}{RESET}"


def indent(text: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def ago(ms: int) -> str:
    s = time.time() - ms / 1000
    for unit, n in (("day", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            v = int(s // n)
            return f"{v} {unit}{'s' if unit == 'day' and v > 1 else ''} ago"
    return "just now"


def load_history(session_id: str, folder: str):
    """Title, last-active time, your messages and Claude's replies from a saved chat. Read locally, costs nothing."""
    try:
        msgs = get_session_messages(session_id, directory=folder)
        info = next((s for s in list_sessions(directory=folder) if s.session_id == session_id), None)
    except Exception:
        return None, [], []

    def text_of(m) -> str:
        content = m.message.get("content") if isinstance(m.message, dict) else None
        if isinstance(content, str):
            return content.strip()
        return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text").strip()

    asks = [t for m in msgs if m.type == "user" and (t := text_of(m)) and not t.startswith(("<", "[Request interrupted"))]
    replies = [t for m in msgs if m.type == "assistant" and (t := text_of(m))]
    return info, asks, replies


def show_recap(asks: list, replies: list) -> None:
    """Print where the chat left off: your last message and the start of Claude's last reply."""
    print(f"{BOLD}Where you left off{RESET} {DIM}· {len(asks)} messages so far{RESET}")
    label, items = recent_tasks(asks, replies)
    print(f"  {DIM}{label}{RESET}")
    for item in items:
        print(f"  {GREEN}{'✓' if label.startswith('Recently') else '·'}{RESET} {style(item[:WIDTH - 6])}")
    print(f"  {DIM}Last exchange:{RESET}")
    for who, color, text, max_lines in (("you", GREEN, asks[-1], 2), ("claude", ORANGE, replies[-1] if replies else "", 3)):
        lines = textwrap.wrap(" ".join(text.replace("#", "").split()), WIDTH - 12)
        if not lines:
            continue
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] += " …"
        for i, line in enumerate(lines):
            print(f"  {color}{who if i == 0 else '':<7}{RESET} {style(line)}")
    print()


def load_sessions() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_session(folder: str, session_id: str) -> None:
    sessions = load_sessions()
    sessions[folder] = session_id
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


def describe_tool(block: ToolUseBlock) -> str:
    i = block.input or {}
    detail = i.get("file_path") or i.get("command") or i.get("pattern") or i.get("path") or ""
    return f"  {DIM}· {block.name:<6} {str(detail)[:WIDTH - 12]}{RESET}"


AUTO_MODEL = False  # set by --auto or /auto
AUTO_MODELS = {"easy": "claude-haiku-5-5", "medium": "claude-sonnet-5-5", "hard": "claude-opus-5-5"}
ROUTER_PROMPT = """Rate how hard this request to a coding agent is. Reply with one word: easy, medium or hard.
easy: questions, explanations, small lookups, one-line edits, running a single command.
medium: normal feature work, a bug fix in one area, writing tests, edits across a few files.
hard: architecture or design, multi-file refactors, tricky debugging, performance or security work, planning large work.
A short follow-up such as "yes", "do it" or "continue" has the same level as the previous request."""


def haiku(system: str, text: str, max_tokens: int = 200) -> str:
    """One quick Haiku call, returning the reply text. Raises if the call fails."""
    body = json.dumps({"model": AUTO_MODELS["easy"], "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": text}]}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        reply = json.loads(r.read().decode("utf-8"))
    return " ".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")


def rate_difficulty(prompt: str, previous: str) -> str:
    """Ask Haiku how hard the request is. Falls back to medium if the call fails."""
    text = f"Previous request: {previous[:1500] or '(none)'}\n\nThis request: {prompt[:3000]}"
    try:
        word = haiku(ROUTER_PROMPT, text).lower()
    except Exception:
        return "medium"
    return next((level for level in ("hard", "medium", "easy") if level in word), "medium")


RECAP_PROMPT = """You get the end of a chat between a user and a coding agent.
List the 3 most recent tasks the agent completed, newest first. One line each, at most 12 words,
plain past tense, each line starting with "- ". Only list work that was actually done."""


def recent_tasks(asks: list, replies: list) -> list:
    """Three short bullets of recently finished work, written by Haiku (under a cent)."""
    text = ("User requests, oldest first:\n" + "\n".join(f"- {a[:300]}" for a in asks[-6:])
            + "\n\nAgent's latest replies, oldest first:\n" + "\n\n".join(r[:800] for r in replies[-4:]))
    try:
        lines = haiku(RECAP_PROMPT, text, 300).splitlines()
        items = [l.strip().lstrip("-•* ").strip() for l in lines if l.strip().startswith(("-", "•", "*"))]
    except Exception:
        items = []
    if items:
        return "Recently done:", items[:3]
    return "Your last requests:", [" ".join(a.split())[:90] for a in asks[-3:][::-1]]  # when Haiku can't be reached


AUTO_APPROVE = False  # set by --yes
ALWAYS_ALLOWED: set = set()  # tools you answered "a" for, this run only


async def ask_permission(tool_name, tool_input, context):
    # File edits inside the folder are auto-approved (acceptEdits). Everything else asks you first,
    # unless you started with --yes or answered "a" for this tool earlier in the run.
    if AUTO_APPROVE or tool_name in ALWAYS_ALLOWED:
        return PermissionResultAllow(behavior="allow")
    shown = tool_input.get("command") or json.dumps(tool_input)[:300]
    prompt = f"\n  {YELLOW}{BOLD}? Allow {tool_name}{RESET} {shown}\n  {YELLOW}  y = yes · a = always allow {tool_name} this run · Enter = no ›{RESET} "
    answer = (await anyio.to_thread.run_sync(input, prompt)).strip().lower()
    if answer in ("a", "always"):
        ALWAYS_ALLOWED.add(tool_name)
        return PermissionResultAllow(behavior="allow")
    if answer in ("y", "yes"):
        return PermissionResultAllow(behavior="allow")
    return PermissionResultDeny(behavior="deny", message="The user declined this action.")


PROG = os.path.basename(sys.argv[0]) or "agent.py"
CHAT_HELP = """Inside the chat:
  /model claude-opus-5-5   switch model (turns auto-switch off)
  /auto                    pick the model by task difficulty, on or off
  /budget 5                change the max US$ per message
  /new                     start a fresh chat in this folder
  /yes                     turn auto-approve on or off
  /help                    show this list
  exit                     quit (the chat is saved)"""


def examples() -> str:
    return f"""Examples (type options exactly as shown: no brackets, no space after --):
  python {PROG} --budget 50
  python {PROG} --model claude-opus-5-5 --budget 10
  python {PROG} --new        start a fresh chat in this folder
  python {PROG} --auto       pick Haiku, Sonnet or Opus per message by difficulty

{CHAT_HELP}"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.exit(f"{message}\n\n{examples()}")


async def main() -> None:
    p = Parser(
        description="Claude agent chat on API credits",
        usage=f"python {PROG} --model ID --budget USD --new --yes --auto --dir FOLDER   (every option is optional)",
        epilog=examples(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", default=os.getcwd(), help="folder the agent works in (default: current folder)")
    p.add_argument("--new", action="store_true", help="start a fresh chat instead of continuing")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--budget", type=float, default=2.0, help="max US$ per message (default 2.00)")
    p.add_argument("--yes", action="store_true", help="approve every action without asking (use with care)")
    p.add_argument("--auto", action="store_true", help="pick Haiku, Sonnet or Opus for each message by difficulty")
    args = p.parse_args()

    global AUTO_APPROVE, AUTO_MODEL
    AUTO_APPROVE = args.yes
    AUTO_MODEL = args.auto

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set in this window. Set it first.")

    folder = str(Path(args.dir).resolve())
    resume = None if args.new else load_sessions().get(folder)

    options = ClaudeAgentOptions(
        cwd=folder,
        model=args.model,
        resume=resume,
        permission_mode="acceptEdits",
        can_use_tool=ask_permission,
        max_budget_usd=args.budget,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    info, asks, replies = load_history(resume, folder) if resume else (None, [], [])
    if resume:
        chat = "continuing"
        if info:
            chat += f' "{info.summary[:60]}" · last active {ago(info.last_modified)}'
    else:
        chat = "new chat"
    bar = f"{ORANGE}│{RESET}"
    print(f"\n{ORANGE}┌{RESET} {BOLD}claude-sdk-agent{RESET} {DIM}· runs on your API credits{RESET}")
    for key, value in (("folder", folder),
                       ("model", "auto (Haiku, Sonnet or Opus by difficulty)" if AUTO_MODEL else args.model),
                       ("budget", f"${args.budget:.2f} per message"),
                       ("chat", chat)):
        print(f"{bar} {DIM}{key:<7}{RESET} {value}")
    if AUTO_APPROVE:
        print(f"{bar} {RED}{'approve':<7} --yes is on: commands run without asking{RESET}")
    print(f"{ORANGE}└{RESET} {DIM}/help for commands · exit to quit{RESET}\n")
    if asks:
        show_recap(asks, replies)

    run_total = 0.0
    last_prompt = ""
    while True:  # each pass is one connection; /budget reconnects to the same chat with the new cap
        reconnect = False
        client_total = 0.0  # the SDK's running cost restarts with each connection
        async with ClaudeSDKClient(options=options) as client:
            while True:
                try:
                    prompt = await anyio.to_thread.run_sync(input, f"{GREEN}{BOLD}you ›{RESET} ")
                except EOFError:
                    return
                cmd = prompt.strip()
                if cmd.lower() in ("exit", "quit"):
                    return
                if not cmd:
                    continue
                if cmd.startswith("/model"):
                    parts = cmd.split()
                    if len(parts) == 2:
                        await client.set_model(parts[1])
                        options.model = parts[1]
                        AUTO_MODEL = False
                        print(f"{DIM}Model is now {parts[1]} (auto-switch off).{RESET}\n")
                    else:
                        print(f"{DIM}Usage: /model claude-opus-5-5{RESET}\n")
                    continue
                if cmd.startswith("/budget"):
                    parts = cmd.split()
                    try:
                        value = float(parts[1].lstrip("$"))
                        if value <= 0:
                            raise ValueError
                    except (IndexError, ValueError):
                        print(f"{DIM}Usage: /budget 5   (max US$ per message){RESET}\n")
                        continue
                    options.max_budget_usd = value
                    reconnect = True
                    print(f"{DIM}Budget is now ${value:.2f} per message. Reconnecting to the same chat...{RESET}\n")
                    break
                if cmd == "/new":
                    options.resume = None
                    reconnect = True
                    print(f"{DIM}Starting a new chat in this folder...{RESET}\n")
                    break
                if cmd == "/help":
                    print(f"{DIM}{CHAT_HELP}{RESET}\n")
                    continue
                if cmd == "/auto":
                    AUTO_MODEL = not AUTO_MODEL
                    print(f"{DIM}Auto-switch is {'on: each message picks Haiku, Sonnet or Opus by difficulty' if AUTO_MODEL else 'off: staying on ' + str(options.model)}.{RESET}\n")
                    continue
                if cmd == "/yes":
                    AUTO_APPROVE = not AUTO_APPROVE
                    print(f"{RED if AUTO_APPROVE else DIM}Auto-approve is {'on: commands run without asking' if AUTO_APPROVE else 'off: commands ask first'}.{RESET}\n")
                    continue

                if AUTO_MODEL:
                    level = await anyio.to_thread.run_sync(rate_difficulty, prompt, last_prompt)
                    if AUTO_MODELS[level] != options.model:
                        await client.set_model(AUTO_MODELS[level])
                        options.model = AUTO_MODELS[level]
                    print(f"{DIM}auto: {level} -> {options.model}{RESET}")
                last_prompt = prompt
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                print(f"\n{ORANGE}{BOLD}● claude{RESET}\n{indent(style(block.text.strip()))}")
                            elif isinstance(block, ToolUseBlock):
                                print(describe_tool(block))
                    elif isinstance(msg, ResultMessage):
                        if msg.session_id:
                            save_session(folder, msg.session_id)
                            options.resume = msg.session_id  # so a reconnect continues this chat
                        total = msg.total_cost_usd
                        if total is None:
                            cost = "cost n/a"
                        else:
                            # total_cost_usd is the running total for this connection; the difference is this message.
                            this_msg = total - client_total if total >= client_total else total
                            client_total = total
                            run_total += this_msg
                            cost = f"this message ${this_msg:.4f} · total this run ${run_total:.4f}"
                        status = f"{RED}error: {msg.subtype}{RESET}{DIM}" if msg.is_error else "done"
                        print(f"\n{rule(f'{status} · {cost} · {options.model}')}\n")
        if not reconnect:
            return


if __name__ == "__main__":
    anyio.run(main)
