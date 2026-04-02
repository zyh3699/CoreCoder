# NanoCoder

[![PyPI](https://img.shields.io/pypi/v/nanocoder)](https://pypi.org/project/nanocoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/NanoCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/NanoCoder/actions)

**51万行 TypeScript → 1300 行 Python。**

我花了一个周末通读 Claude Code 泄露的全部源码。凌晨三点，盯着 `StreamingToolExecutor.ts` 里 530 行的并行工具编排系统，我想：这些设计确实精妙，但不应该需要逆向一个闭源代码库才能理解它们。

于是我从零重写了核心架构。**NanoCoder 就是把所有不承重的部分拆掉之后剩下的东西。** 每个文件一屏看完，每个设计决策都来自生产级系统的实战验证。

[English](README.md) | [中文](README_CN.md) | [Claude Code 源码导读（7篇系列）](article/)

## 它能干什么

```
You > 读一下 main.py，修掉那个拼错的 import

> read_file(file_path='main.py')
> edit_file(file_path='main.py', old_string='from utils import halper', new_string='from utils import helper')
--- a/main.py
+++ b/main.py
@@ -1,4 +1,4 @@
-from utils import halper
+from utils import helper

修好了：`halper` → `helper`。
```

它会读你的代码、做精准编辑（每次改动都给你看 diff）、跑命令、搜索代码库。跟 Claude Code 一样的工作流，但**用什么模型你说了算**。

## 为什么要做这个

Claude Code 好用，但有三个问题：

1. **只能用 Anthropic 的 API。** 你要是用 DeepSeek、Qwen、Kimi 或者本地模型，那就没戏了。
2. **源码有 51 万行 TypeScript。** 就算泄露了，真正看懂也得考古好几天。
3. **没法魔改。** 想加个自定义工具？改改 Agent 循环？对着一个你本来不该有的闭源代码库折腾，不现实。

NanoCoder 解决这三个问题：**1300 行**，一个下午读完；**任意 OpenAI 兼容 API** 都能用；MIT 协议，fork 了想怎么改怎么改。

## 快速开始

```bash
pip install nanocoder
```

选你的模型：

```bash
# OpenAI
export OPENAI_API_KEY=sk-...
nanocoder

# DeepSeek（国内首选）
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com
nanocoder -m deepseek-chat

# 通义千问
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
nanocoder -m qwen-plus

# Kimi（月之暗面）
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.moonshot.cn/v1
nanocoder -m moonshot-v1-128k

# 本地模型（Ollama）
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
nanocoder -m qwen2.5-coder

# 单次模式（不进 REPL）
nanocoder -p "给 parse_config() 加上错误处理"
```

只要是 OpenAI 兼容的 API 就行：**OpenAI、DeepSeek、Qwen、Kimi、智谱 GLM、Ollama、vLLM、OpenRouter、Together AI**。

## 里面有什么

整个项目一目了然：

```
nanocoder/
├── cli.py          REPL + 命令行参数
├── agent.py        Agent 循环（+ 并行工具执行）
├── llm.py          流式 OpenAI 兼容客户端
├── context.py      三层上下文压缩
├── session.py      会话保存/恢复
├── prompt.py       系统提示词
├── config.py       环境变量配置
└── tools/
    ├── bash.py     Shell 执行 + 危险命令拦截
    ├── read.py     带行号的文件读取
    ├── write.py    文件创建
    ├── edit.py     搜索替换 + unified diff
    ├── glob_tool.py  文件模式匹配
    ├── grep.py     正则内容搜索
    └── agent.py    子代理生成
```

### 核心设计（从 Claude Code 里提炼的）

通读源码之后，我认为最重要的几个模式。NanoCoder 全部实现了：

**搜索替换式编辑。** Claude Code 不用行号补丁，也不整文件重写。它让 LLM 指定一段精确的文本来查找和替换，而且这段文本必须在文件里唯一出现。就这一个约束，干掉了一整类编辑 bug：不会改错位置，不会行号错位。NanoCoder 的实现在每次编辑后还会输出 unified diff，让你看清每一处改动。

**Agent 工具循环。** 用户说话 → LLM 返回工具调用 → 执行工具 → 结果喂回 LLM → 重复，直到 LLM 用文本回复。说起来简单，细节里全是坑：一次来 8 个工具调用怎么办？（线程池并行执行。）上下文快满了怎么办？（三层压缩。）任务太复杂一个上下文装不下呢？（派生子代理。）

**三层上下文压缩。** Claude Code 用四层策略（HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE → Autocompact）。NanoCoder 实现了其中三层：先把冗长的工具输出裁剪到头尾，再用 LLM 摘要旧对话，最后硬压缩兜底。这意味着你可以一直干下去，不用担心上下文爆掉。

**子代理委托。** Claude Code 的 AgentTool（1397 行）为复杂子任务派生独立 Agent，各有自己的上下文窗口。NanoCoder 用 50 行做了一样的事。

**危险命令拦截。** `rm -rf /`、fork bomb、`curl | bash` 这些在执行前就会被拦下来。Claude Code 的 BashTool 有 1143 行的安全检查，NanoCoder 实现了最核心的那些。

## 怎么扩展

加个工具大概 20 行：

```python
from nanocoder.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "发起 HTTP 请求。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "请求地址"},
            "method": {"type": "string", "description": "HTTP 方法", "default": "GET"},
        },
        "required": ["url"],
    }

    def execute(self, url: str, method: str = "GET") -> str:
        import urllib.request
        resp = urllib.request.urlopen(url)
        return resp.read().decode()[:5000]
```

在 `tools/__init__.py` 里注册一下就行了。

也可以当库用：

```python
from nanocoder.agent import Agent
from nanocoder.llm import LLM

llm = LLM(model="deepseek-chat", api_key="sk-...", base_url="https://api.deepseek.com")
agent = Agent(llm=llm)
response = agent.chat("找出项目里所有的 TODO 注释并列出来")
```

## REPL 命令

| 命令 | 干嘛的 |
|---|---|
| `/model <name>` | 切换模型 |
| `/tokens` | 看 token 用量 |
| `/save` | 保存会话到磁盘 |
| `/sessions` | 列出已保存的会话 |
| `/reset` | 清空对话历史 |
| `quit` | 退出 |

恢复会话：`nanocoder -r <session_id>`

## 对比

|  | Claude Code | Claw-Code | NanoCoder |
|---|---|---|---|
| 代码量 | 51万行 TS（闭源） | 10万+行 Python/Rust | **1300 行 Python** |
| 模型 | 仅 Anthropic | 多模型 | **任意 OpenAI 兼容** |
| 能通读吗？ | 不能 | 很难 | **一个下午** |
| 适合 | 直接用 | 直接用 | **先看懂，再造自己的** |

## 源码导读

我还写了一套 [16 篇的 Claude Code 架构深度解析](article/)，从 Agent 循环到权限系统，到隐藏在 feature flag 后面的未发布功能。如果你想知道 NanoCoder 为什么这样设计，从那里开始。

## License

MIT。Fork 它，学它，拿去造更好的东西。

---

作者 [何宇峰](https://github.com/he-yufeng) · Agentic AI Researcher @ Moonshot AI (Kimi) · [Claude Code 源码分析（知乎 17 万阅读）](https://zhuanlan.zhihu.com/p/1898797658343862272)
