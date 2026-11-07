---
name: sdk-agent
description: Set up and run sdk-agent, a terminal coding agent built on the Claude Agent SDK that works in any repo and bills to an Anthropic API key. Use when the user wants a Claude coding session that runs on their API key or API credits (for example credits whose grant covers the Agent SDK), or asks to install, start, continue or troubleshoot sdk-agent.
---

# sdk-agent

`scripts/agent.py` is a terminal chat with a Claude agent. It works inside one folder, can read, edit and create files there, and asks the user `y/N` before running commands or any other tool. It bills to whatever `ANTHROPIC_API_KEY` is set to. Each folder keeps its own chat, so running it again in the same folder continues that chat.

## Setup

1. Check Python is 3.10 or newer: `python --version`. If it is missing or older, tell the user to install it from python.org and stop.
2. Install the SDK: `python -m pip install claude-agent-sdk`. It bundles the agent runtime, so nothing else is needed.
3. The user sets their own key. Never ask for the key in chat, print it, or write it to a file. Give them the command for their shell:
   - PowerShell: `$env:ANTHROPIC_API_KEY = "their-key"`
   - macOS/Linux: `export ANTHROPIC_API_KEY="their-key"`
   Keys are created in the Claude Console under API Keys.

## Run

From the repo they want to work in:

```
python <path-to-this-skill>/scripts/agent.py
```

Options:
- `--dir <folder>`: work in another folder instead of the current one.
- `--new`: start a fresh chat instead of continuing.
- `--model <model-id>`: default `claude-sonnet-5-5`.
- `--budget <usd>`: spending cap per message, default 2.00.

Type `exit` to quit. The chat is saved.

## Billing check (do this before any long session)

Which credit balance pays for a run is decided by Anthropic's billing, not by this script. Promotional grants list the products they apply to on the Console's Billing page. Have the user:

1. Note their purchased and promotional balances on the Billing page.
2. Run the agent and send one short message, such as `say hi in one line`.
3. Refresh the Billing page and see which balance dropped.

Report what they find. Do not tell the user a particular credit grant will be used until this check confirms it.

## Safety notes to pass on

- File edits inside the working folder are applied without asking. Run it in a git repo, or commit first, so changes can be undone.
- Every other action (shell commands, web access) asks first. Answer `n` to anything unexpected.
- The per-message budget stops a runaway message, not a long session. Watch the cost line printed after each reply.
