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
import sys
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
    return f"  {DIM}[{block.name}] {str(detail)[:120]}{RESET}"


AUTO_APPROVE = False  # set by --yes
ALWAYS_ALLOWED: set = set()  # tools you answered "a" for, this run only


async def ask_permission(tool_name, tool_input, context):
    # File edits inside the folder are auto-approved (acceptEdits). Everything else asks you first,
    # unless you started with --yes or answered "a" for this tool earlier in the run.
    if AUTO_APPROVE or tool_name in ALWAYS_ALLOWED:
        return PermissionResultAllow(behavior="allow")
    shown = tool_input.get("command") or json.dumps(tool_input)[:300]
    prompt = f"\n  {YELLOW}Allow {tool_name}:{RESET} {shown}\n  {YELLOW}[y = yes, a = always allow {tool_name} this run, N = no] >{RESET} "
    answer = (await anyio.to_thread.run_sync(input, prompt)).strip().lower()
    if answer in ("a", "always"):
        ALWAYS_ALLOWED.add(tool_name)
        return PermissionResultAllow(behavior="allow")
    if answer in ("y", "yes"):
        return PermissionResultAllow(behavior="allow")
    return PermissionResultDeny(behavior="deny", message="The user declined this action.")


PROG = os.path.basename(sys.argv[0]) or "agent.py"
CHAT_HELP = """Inside the chat:
  /model claude-opus-5-5   switch model
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

{CHAT_HELP}"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.exit(f"{message}\n\n{examples()}")


async def main() -> None:
    p = Parser(
        description="Claude agent chat on API credits",
        usage=f"python {PROG} --model ID --budget USD --new --yes --dir FOLDER   (every option is optional)",
        epilog=examples(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", default=os.getcwd(), help="folder the agent works in (default: current folder)")
    p.add_argument("--new", action="store_true", help="start a fresh chat instead of continuing")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--budget", type=float, default=2.0, help="max US$ per message (default 2.00)")
    p.add_argument("--yes", action="store_true", help="approve every action without asking (use with care)")
    args = p.parse_args()

    global AUTO_APPROVE
    AUTO_APPROVE = args.yes

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

    print(f"{DIM}Folder: {folder}{RESET}")
    print(f"{DIM}Model: {args.model} | {'continuing previous chat' if resume else 'new chat'} | /help for commands, 'exit' to quit{RESET}")
    if AUTO_APPROVE:
        print(f"{RED}--yes is on: commands run without asking.{RESET}")
    print()

    run_total = 0.0
    while True:  # each pass is one connection; /budget reconnects to the same chat with the new cap
        reconnect = False
        client_total = 0.0  # the SDK's running cost restarts with each connection
        async with ClaudeSDKClient(options=options) as client:
            while True:
                try:
                    prompt = await anyio.to_thread.run_sync(input, f"{GREEN}you >{RESET} ")
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
                        print(f"{DIM}Model is now {parts[1]}.{RESET}\n")
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
                if cmd == "/yes":
                    AUTO_APPROVE = not AUTO_APPROVE
                    print(f"{RED if AUTO_APPROVE else DIM}Auto-approve is {'on: commands run without asking' if AUTO_APPROVE else 'off: commands ask first'}.{RESET}\n")
                    continue

                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                print(f"\n{ORANGE}claude >{RESET} {style(block.text)}")
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
                            cost = f"this message ${this_msg:.4f} | total this run ${run_total:.4f}"
                        status = f"{RED}error: {msg.subtype}{RESET}{DIM}" if msg.is_error else "done"
                        print(f"\n{DIM}[{status} | {cost}]{RESET}\n")
        if not reconnect:
            return


if __name__ == "__main__":
    anyio.run(main)
