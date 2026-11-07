# sdk-agent

A terminal coding agent built on the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/). Point it at any repo and chat with it: it reads, edits and creates files there, asks before running commands, and remembers the conversation per folder.

It bills to your Anthropic API key, so it runs on API credits. That includes promotional credits, if your grant lists the Agent SDK. Tested in November 2026: usage came out of a promotional grant whose "Applies to" list included the Agent SDK, while Claude Code itself could not use that grant. Grants differ, so check yours first (see [Billing](#billing)).

## Quick start

Needs Python 3.10+.

```bash
git clone https://github.com/QDinvest/claude-sdk-agent
python -m pip install -r claude-sdk-agent/requirements.txt
```

Set your API key (from the Claude Console, under API Keys):

```powershell
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "your-key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY="your-key"
```

Go to the repo you want to work on and start it:

```bash
cd path/to/your/repo
python path/to/claude-sdk-agent/skills/sdk-agent/scripts/agent.py
```

Then type what you want built at the `you >` prompt. Type `exit` to quit. Run the same command again in that folder to pick up where you left off.

## Options

| Flag | What it does | Default |
|---|---|---|
| `--dir <folder>` | Work in another folder | current folder |
| `--new` | Start a fresh chat | continue the last one |
| `--model <id>` | Pick the model | `claude-sonnet-5-5` |
| `--budget <usd>` | Spending cap per message | `2.00` |

## How it behaves

- **File edits** inside the working folder happen without asking. Use it in a git repo so you can review and undo.
- **Everything else** (shell commands, web access) asks `y/N` first.
- After each reply it prints what that message cost.
- Output is colored: your prompt, Claude's replies, tool steps and permission prompts each look different. Set `NO_COLOR=1` to turn color off.
- Flags go on the start command, not in the chat. Typing `--budget 10` into a message does nothing.
- Chats are stored per folder in `~/.claude-agent-sessions.json`.

## Billing

Which credit balance pays is decided by Anthropic, not by this tool. Promotional grants show the products they cover on the Console's Billing page. Before a long session:

1. Note your purchased and promotional balances.
2. Send one short message, like `say hi in one line`.
3. Refresh the Billing page and see which balance went down.

## Use it as a Claude skill

`skills/sdk-agent/` is a skill. Copy that folder to `~/.claude/skills/` and Claude can install, start and troubleshoot the agent for you when you ask.

## Not affiliated with Anthropic

This is a community tool that uses Anthropic's public Agent SDK.
