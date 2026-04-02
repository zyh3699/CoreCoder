# 知乎文章草稿 2：导读系列引流

**标题方案**：
- A: Claude Code 源码里最值得学的 7 个设计模式
- B: 读完 Claude Code 51 万行源码，我写了 7 篇技术导读
- C: AI 编程 Agent 是怎么工作的？Claude Code 源码里的答案

---

上个月 Claude Code 源码泄露之后，我写了一篇分析文章，反响挺大（17万阅读）。很多朋友的反馈是：分析文看了，但 51 万行源码自己根本看不动，能不能出个导读？

于是我花了两周写了一套导读。不是把每个文件都过一遍那种——那个版本也有，16 篇 16 万字，放在 GitHub 上了——这套导读只挑了我认为最值得开发者了解的 7 个设计模式，每篇围绕一个核心问题展开。

如果你在做 AI Agent 相关的工作，或者单纯对"一个 51 万行的生产级 AI 产品内部长什么样"好奇，这个系列应该能给你一些启发。

全部文章在 GitHub 上：https://github.com/he-yufeng/NanoCoder/tree/main/article

下面是每篇的内容概要和核心发现。

---

## 第一篇：从 51 万行说起

为什么一个 CLI 工具需要 51 万行？

答案是它不只是一个 CLI 工具。它同时是：一个 LLM 交互引擎、一个终端 UI 应用（用 React 渲染）、一个文件系统操作器、一个代码搜索引擎、一个多 Agent 协调器、一个插件系统、一个权限管理系统、一个 MCP 协议客户端。51 万行不是膨胀，是真的有那么多事要做。

技术栈选型里最意外的是 **Bun + React**。Bun 的选择比较好理解（启动速度是 Node.js 的四分之一），React 写终端就让人意外了。但看完代码之后理解了——多 Agent 并行输出、实时进度条、权限弹窗、代码 diff 高亮，这些 UI 状态管理用 React 的声明式模型处理比手搓 ANSI 转义码合理得多。

这篇还总结了读完全部代码后的十大设计哲学。其中我觉得最值得借鉴的是"上下文经济学"——token 就是钱，每个工具的输出都有截断策略，上下文有四层压缩，工具定义里有专门的 `getToolUseSummary()` 方法定义压缩时的摘要策略。

---

## 第二篇：1729 行的 while(true)

`src/query.ts`，整个 Claude Code 最核心的文件。一个 `while(true)` 循环驱动所有行为。

这篇深入了循环的五个关键细节：

**系统提示词是动态拼的。** 不是一个写死的字符串——`prompts.ts` 有 914 行，根据工作目录、Git 状态、可用工具、甚至具体模型版本动态组装。里面还有一堆 `@[MODEL LAUNCH]` 注释，是给未发布的 Capybara 模型准备的行为修正。

**StreamingToolExecutor。** 模型还在流式输出的时候，前面的工具已经在跑了。530 行的事件驱动状态机，监听 SSE 流事件，每个 tool_use 块的参数收集完毕就立即提交执行。只读工具并行，写工具独占。

**错误恢复。** 429 限流指数退避重试、400/413 上下文过长自动压缩重试、529 服务过载切 fallback 模型、输出触顶悄悄重试最多 3 次（上面还有段中世纪巫师口吻的注释，维护者显然被这段代码坑过很多次）。

**工具异常喂回 LLM。** 大多数 Agent 框架遇到工具异常就报错给用户。Claude Code 把异常包装成 tool_result 喂回 LLM，让 LLM 自己决定怎么处理。这才是"agentic"的核心含义——Agent 遇到问题自己想办法。

---

## 第三篇：让 AI 安全地改你的代码

Claude Code 不用行号补丁，不整文件重写，不生成 diff 格式。它用**搜索替换**。

LLM 指定一段精确的文本和替换内容，文本必须在文件中唯一出现。0 次 = 记错了文件内容，2+ 次 = 上下文不够无法定位。只有 1 次才执行。这把"编辑文件"从一个模糊操作变成了确定性操作。

这篇还分析了工具系统的整体设计——40 多个工具零继承，全是 `buildTool()` 工厂函数生成的纯对象。两阶段门控（先检查输入合法性，再检查权限）减少了不必要的弹窗。BashTool 1143 行的安全堡垒——命令分类器、沙箱（macOS sandbox-exec / Linux seccomp）、交互式命令拦截、大输出管理、sed 检测。

---

## 第四篇：有限窗口，无限任务

128K token 听起来很多，十几轮工具调用就快满了。Claude Code 用四层策略分级处理：

1. **HISTORY_SNIP**：删纯噪声（grep 返回 500 行，模型只用了 3 行，剩下 497 行直接删）
2. **CACHED_MICROCOMPACT**：调一次 LLM 做摘要，结果缓存复用
3. **CONTEXT_COLLAPSE**：结构化归档，保留每轮的决策要点
4. **Autocompact**：后台自动触发，用户无感知

按顺序执行，前面能搞定就不动后面。核心洞察：不同信息的"保质期"不一样——工具中间输出几轮后就没用了，但用户的需求描述整个会话都要保留。按保质期分级处理，比一刀切好得多。

---

## 第五篇：边想边做

`StreamingToolExecutor` 的深度分析。530 行，事件驱动状态机。

核心价值：把工具执行时间从用户可感知的延迟中移除。LLM 生成 2 秒的回复，其中包含三个 read_file 调用——如果串行等生成完再执行，用户等 2.45 秒。如果流式解析并行执行，用户等 2 秒（工具执行被完全吸收在 LLM 生成时间里）。

这篇分析了并发安全调度逻辑（`isConcurrencySafe` 标记）、结果排序保证（按接收顺序缓冲）、以及为什么完整实现需要 530 行——部分 JSON 拼接、错误传播、UI 更新竞争、AbortController 取消，这些 edge case 组合起来产生了大量状态管理代码。

---

## 第六篇：当一个 Claude 不够用

AgentTool 6700 行代码解析。三种执行模式：普通模式（共享文件系统）、Worktree 模式（Git 隔离）、后台模式（异步执行）。

子 Agent 不能再创建子 Agent——不是做不到，是工程上不值得。递归 Agent 的费用爆炸风险和调试难度远超两层的收益。

六种内置 Agent 类型：通用、规划（只做计划不执行）、探索（只读，没有写工具）、验证（跑测试）、帮助、配置。`exploreAgent` 的设计特别好——只给 read/grep/glob，保证它不会误改代码。

团队系统（`TeamCreateTool`）已经在源码里实现了，但还在 Feature Flag 后面。支持角色分配、Agent 间消息通信、tmux pane / in-process / remote 三种后端。

---

## 第七篇：Feature Flag 背后的秘密

44 个 Feature Flag。两层系统：编译时 DCE（Dead Code Elimination，代码物理消失）和运行时 GrowthBook A/B 测试。

最震撼的发现：

**KAIROS 永驻模式。** Claude Code 变成守护进程，定期醒来检查是否有事要做，自主决定是否行动。包含 autoDream（后台记忆整理）和 PROACTIVE（主动执行模式）。从工具变成同事。

**Buddy 宠物系统。** 18 个物种，5 级稀有度，1% 传说概率，帽子系统，闪光变体。物种名全部 hex 编码——因为其中一个（capybara）是未发布模型 Claude Mythos 的内部代号，构建系统会 grep 黑名单字符串。

**Undercover Mode。** Anthropic 员工给外部开源项目提代码时自动去除 AI 归因。说明他们内部已经大规模使用 Claude Code 做日常开发了。

**消融实验。** `ABLATION_BASELINE` 一键关闭 thinking/compact/memory/background，跑对照实验量化每个功能的价值。在工业代码里用 ML 研究的方法论做产品，第一次见。

---

## 怎么获取

全部 7 篇导读在 GitHub 上，免费开放：

👉 https://github.com/he-yufeng/NanoCoder/tree/main/article

配套的 Python 参考实现（1300 行，上面讨论的所有设计模式都有可运行的代码）：

👉 https://github.com/he-yufeng/NanoCoder

觉得有用给个 Star。有技术问题可以在评论区讨论或到 GitHub 开 issue。

---

*作者：何宇峰 | Agentic AI Researcher @ Moonshot AI (Kimi) | 港大 CS 硕士*
