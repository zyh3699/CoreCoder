# NanoCoder

[![PyPI](https://img.shields.io/pypi/v/nanocoder)](https://pypi.org/project/nanocoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/NanoCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/NanoCoder/actions)

**512,000 lines of TypeScript → 1,300 lines of Python.**

I spent a weekend reading through the leaked Claude Code source — all half a million lines of it. Somewhere around 3 AM, staring at `StreamingToolExecutor.ts` and its 530-line parallel tool orchestration system, I thought: the core ideas here are brilliant, but you shouldn't need to reverse-engineer a proprietary codebase to understand them.

So I rebuilt the essential architecture from scratch. **NanoCoder is what's left when you strip away everything that isn't load-bearing.** Every file fits on one screen. Every design decision comes from a battle-tested production system.

[English](README.md) | [中文](README_CN.md) | [Claude Code Source Guide (7-part series)](article/)

## What Can It Actually Do?

```
You > read main.py and fix the broken import

> read_file(file_path='main.py')
> edit_file(file_path='main.py', old_string='from utils import halper', new_string='from utils import helper')
--- a/main.py
+++ b/main.py
@@ -1,4 +1,4 @@
-from utils import halper
+from utils import helper

Fixed the typo: `halper` → `helper`.
```

It reads your code, makes targeted edits (showing you exactly what changed), runs commands, searches your codebase — the same workflow as Claude Code, but with **any LLM you want**.

## Why This Exists

Claude Code is great, but:

1. **It only works with Anthropic's API.** If you're using DeepSeek, Qwen, Kimi, or a local model — you're out of luck.
2. **The source is 512,000 lines of TypeScript.** Even with the leak, understanding how it *actually works* requires serious archaeology.
3. **You can't hack on it.** Want to add a custom tool? Change the agent loop? Good luck modifying a proprietary codebase you're not supposed to have.

NanoCoder fixes all three: it's **1,300 lines** you can read in an afternoon, it works with **any OpenAI-compatible API**, and it's MIT-licensed — fork it, break it, ship something new.

## Quick Start

```bash
pip install nanocoder
```

Pick your model:

```bash
# OpenAI
export OPENAI_API_KEY=sk-...
nanocoder

# DeepSeek (recommended for Chinese developers)
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com
nanocoder -m deepseek-chat

# Local model via Ollama
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
nanocoder -m qwen2.5-coder

# One-shot mode (no REPL)
nanocoder -p "add error handling to parse_config()"
```

Works with any OpenAI-compatible provider: **OpenAI, DeepSeek, Qwen, Kimi, Zhipu GLM, Ollama, vLLM, OpenRouter, Together AI** — if it speaks the OpenAI chat API, it works.

## What's Inside

The whole thing fits in your head:

```
nanocoder/
├── cli.py          REPL + arg parsing
├── agent.py        The agent loop (+ parallel tool execution)
├── llm.py          Streaming OpenAI-compatible client
├── context.py      3-layer context compression
├── session.py      Save/resume conversations
├── prompt.py       System prompt
├── config.py       Env-based config
└── tools/
    ├── bash.py     Shell execution + dangerous command blocking
    ├── read.py     File reading with line numbers
    ├── write.py    File creation
    ├── edit.py     Search-and-replace + unified diff
    ├── glob_tool.py  File pattern matching
    ├── grep.py     Regex content search
    └── agent.py    Sub-agent spawning
```

### The Key Ideas (from Claude Code)

These are the patterns I consider most important after reading the full source. NanoCoder implements all of them:

**Search-and-replace editing.** Claude Code doesn't do line-number patches or whole-file rewrites. Instead, the LLM specifies an *exact substring* to find and its replacement. The substring must be unique in the file. This one constraint eliminates an entire class of editing bugs — no more "edited the wrong occurrence" or "line numbers shifted." NanoCoder's implementation shows a unified diff after every edit so you can see exactly what changed.

**The agentic tool loop.** User speaks → LLM responds with tool calls → tools execute → results go back to LLM → repeat until the LLM responds with text. Simple on paper, but the devil is in the details: what happens when there are 8 tool calls at once? (Parallel execution via ThreadPool.) What happens when context fills up? (3-layer compression.) What about a task too complex for one context window? (Sub-agent spawning.)

**3-layer context compression.** Claude Code uses a 4-tier system (HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE → Autocompact). NanoCoder implements 3 of those: first snip verbose tool outputs to head+tail, then LLM-summarize old conversation turns, finally hard-collapse as a last resort. This means you can work on long tasks without hitting context limits.

**Sub-agent delegation.** Claude Code's AgentTool (1,397 lines) spawns independent agents for complex sub-tasks, each with its own context window. NanoCoder does the same in 50 lines — `agent` tool creates a fresh Agent, runs the task, returns the summary.

**Dangerous command detection.** `rm -rf /`, fork bombs, `curl | bash` — blocked before they execute. Claude Code's BashTool is 1,143 lines of safety checks; NanoCoder implements the essential patterns.

## Extending It

Adding a tool is ~20 lines:

```python
from nanocoder.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "Make an HTTP request."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "method": {"type": "string", "description": "HTTP method", "default": "GET"},
        },
        "required": ["url"],
    }

    def execute(self, url: str, method: str = "GET") -> str:
        import urllib.request
        resp = urllib.request.urlopen(url)
        return resp.read().decode()[:5000]
```

Register in `tools/__init__.py`, done.

Or use it as a library:

```python
from nanocoder.agent import Agent
from nanocoder.llm import LLM

llm = LLM(model="deepseek-chat", api_key="sk-...", base_url="https://api.deepseek.com")
agent = Agent(llm=llm)
response = agent.chat("find all TODO comments in this project and list them")
```

## REPL Commands

| Command | What it does |
|---|---|
| `/model <name>` | Switch model mid-conversation |
| `/tokens` | Show token usage this session |
| `/save` | Save conversation to disk |
| `/sessions` | List saved sessions |
| `/reset` | Clear conversation history |
| `quit` | Exit |

Resume a session: `nanocoder -r <session_id>`

## How It Compares

|  | Claude Code | Claw-Code | NanoCoder |
|---|---|---|---|
| Code | 512K lines TS (proprietary) | 100K+ lines Python/Rust | **1,300 lines Python** |
| Models | Anthropic only | Multi-provider | **Any OpenAI-compatible** |
| Can you read all of it? | No | Not easily | **Yes, in an afternoon** |
| Purpose | Use it | Use it | **Understand it, then build yours** |

## The Source Guide

I also wrote a [7-part deep dive](article/) into Claude Code's architecture — covering everything from the agent loop to the permission system to the unreleased features hidden behind feature flags. If you want to understand *why* NanoCoder is built the way it is, start there.

## License

MIT. Fork it, learn from it, build something better.

---

Built by [Yufeng He](https://github.com/he-yufeng) · Agentic AI Researcher @ Moonshot AI (Kimi) · [Claude Code source analysis (170K+ reads on Zhihu)](https://zhuanlan.zhihu.com/p/1898797658343862272)
