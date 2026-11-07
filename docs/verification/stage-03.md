# 阶段 03 验证记录

日期：2026-11-07。结果：阶段 03 完成。实现、自动化、本地流式服务桌面联调及真实 DeepSeek 验收均已通过。首次联调出现的官方接口 HTTP 500 已解除。

对应 [真实文字对话与状态事件计划](../plan/V1.0.0/03-text-conversation.md)。

## 实现与接入位置

| 位置 | 职责 |
| --- | --- |
| `src/voice_super_agent/conversation.py` | 原生上下文、文字聚合器与 DeepSeek 装配；接管、片段、结束、错误和资源关闭 |
| `src/voice_super_agent/runtime.py` | 快速接收、串行输入队列、消息 ID 与回复关联、能力状态和完整快照 |
| `src/voice_super_agent/desktop/host.py` | `DesktopApi.send_text` 桥接；沿用已有状态发送线程 |
| `frontend/src/main.tsx`、`desktop.d.ts` | 聊天记录、输入、流式状态、错误和快照恢复 |
| `tests/test_conversation.py` | 本地 SSE 服务驱动真实 DeepSeek 适配器与原生聚合器的核心测试 |

文字流水线为用户聚合器 → DeepSeek → 助手聚合器，共享一个 `LLMContext`。使用 `ExternalUserTurnStrategies`，不装配麦克风、ASR、System One、TTS 或本地语音轮次模型。

接管步骤为 `broadcast_interruption()`、`flush_pipeline()`、追加 `LLMMessagesAppendFrame(run_llm=True)`。本条文字处理完原生助手开始帧后，输入队列继续处理下一条；模型生成仍在流水线中进行。这样连续输入可及时接管，并确保每条用户文字已进入上下文。

展示片段在助手聚合器同步 `on_before_process_frame` 读取，结束状态在同步 `on_after_process_frame` 发布。原生助手聚合器在结束或打断帧处理中完成上下文提交。UI 展示文本与原生模型消息各自保留所属职责，后续持久化应分别接入。

完整快照复制消息字典，避免已排入桌面发送队列的快照被后续片段修改。窗口隐藏或页面重载不销毁后台对话。

## 自动验证

```powershell
npm --prefix frontend run build
uv run --locked python -m unittest discover -s tests -v
git diff --check
```

前端 TypeScript 检查和构建通过；13 项测试通过，其中新增 7 项覆盖：

- 片段流式传递；第二轮真实 HTTP 请求包含前一轮用户和助手的原生上下文。
- 部分旧回复先提交，再追加修正与连续补充；旧流取消，迟到内容不进入新回复。
- HTTP 503 只请求一次，错误绑定对应回复，后续输入仍可完成。
- 后台确认连续输入、隐藏期间继续处理、重新订阅恢复消息；旧快照保持原内容。
- HTTP 401 后保留桌面命令和错误记录，后续输入明确反馈能力不可用。
- 缺少模型 Key 时仍可获取快照、操作窗口，发送说明缺失配置。
- 地址端口配置错误时显示原因、结束等待状态，并可正常退出。

测试使用本地 HTTP SSE 服务，不调用付费模型；实际执行的是本项目锁定版本的 DeepSeek 适配器、worker 和原生聚合器。

规范与需求两路复核已完成。需求复核发现地址配置错误会使回复停留在等待状态并影响退出，现已捕获具体 `InvalidURL` 并反馈配置错误，新增测试通过。复核未保留其他确定问题。

## Windows 桌面联调

沿用本项目 uv 环境、pywebview 6.2.1 和 EdgeChromium。Playwright 经临时本机调试端口连接实际桌面 WebView；测试进程通过环境变量使用本地 SSE 服务和测试 Key，项目 `.env` 保留用户配置。

已观察到首个文字片段及后续更新；首轮和连续追问均完整结束。生成中发送新文字能够接管，旧回复保留普通文字。窗口隐藏期间一条回复继续生成至 240 字；重新显示并重载页面后，消息 ID、内容和状态恢复一致。连续修正与补充分别追加用户消息，最后回复关联到最后一条输入。Shift + Enter 保留换行。

另以空 Key 环境启动实际桌面，页面正常显示，明确提示缺少 `DEEPSEEK_API_KEY`，输入框与发送按钮禁用，窗口操作及退出正常。截图位于 `.local/verification/stage-03-missing-key.png`。

截图位于本工作副本 `.local/verification/stage-03-desktop-chat.png`；本地请求内容和诊断脚本同样保存在忽略目录。此处结果用于验证桌面、桥接和原生流水线，真实模型结果见下节。

## 真实 DeepSeek 验收

用户确认服务恢复后，使用当前 `.env`、`deepseek-flash` 和 `https://api.deepseek.com/v1` 复验成功。验证直接运行当前 `Runtime`、`TextConversation` 与原生 Pipecat 流水线，并操作已有 Windows 桌面完成真实交流。

后台连续输入五条消息，结果如下：

| 场景 | 实际结果 |
| --- | --- |
| 首轮输入 | 要求记住“海棠73、杭州”，回复“已记住”，产生两次文本更新 |
| 连续追问 | 正确回答“代号是海棠73，地点是杭州。”，产生九次文本更新 |
| 生成中接管 | 长文生成到 30 字时发送修正，原生最终提交 31 字，状态为 `interrupted` |
| 连续修正 | 先将地点改为苏州，再将代号改为竹影26，最终回答“代号：竹影26；地点：苏州。” |
| 原生上下文 | 五条用户输入均存在，旧助手片段位于修正输入之前；打断后的旧消息文字保持一致 |
| 快照恢复 | 断开订阅期间继续生成，重新订阅恢复全部十条展示消息 |

本次后台观测到的首字耗时分别为：首轮 0.875 秒、追问 0.344 秒、长文 1.031 秒、连续修正后的最终回复 0.531 秒。桥接同一运行时的接收调用耗时为 0～47 毫秒，较短值受本机时钟分辨率影响；这些值为单次本机观测，不作为性能承诺。

随后通过 Windows UI Automation 操作已有桌面的输入框和发送按钮，使用真实模型再次验证：

- 首轮确认“云杉58、南京”，连续追问正确列出这两项。
- 游记生成时观察到多个可见文字片段，随后连续发送“地点改为无锡”和“代号改为晨露81”。最终界面显示“代号：晨露81；地点：无锡”。
- 最后一次输入后隐藏主窗口，后台继续处理；再次唤出原窗口后，完整回复和先前对话均可查看。

实际桌面截图为 `.local/verification/stage-03-live-desktop.png`，页面文字记录为 `stage-03-live-desktop.json`；后台原生上下文、消息关联与耗时记录为 `stage-03-live-success.json`，均位于本工作副本 `.local/verification/`。此前通过的 13 项自动测试与前端构建结果继续有效，本轮补充真实模型和桌面验收。

## 首次联调的 HTTP 500 记录（已解除）

首次联调使用用户已填写的本项目 `.env`，默认模型为 `deepseek-flash`，默认地址为 `https://api.deepseek.com/v1`。当时 `GET /models` 返回 `deepseek-flash` 和 `deepseek-v4-pro`，生成接口多次返回 `HTTP 500 / internal_error`，页面显示对应请求失败原因。

已对照 [官方首次调用示例](https://api-docs.deepseek.com/) 和 [Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)：官方模型名为 `deepseek-flash`，根地址为 `https://api.deepseek.com`，流式使用 `stream: true`，`thinking.type` 支持 `enabled` 与 `disabled`。本地记录的流水线请求体使用这些字段。

为区分应用接入与外部调用问题，进行了以下诊断：

| 调用方式 | 结果 |
| --- | --- |
| 应用经 Pipecat 的流式请求 | HTTP 500 |
| 独立 OpenAI SDK 流式请求，关闭思考 | HTTP 500 |
| 直接 HTTP 请求根地址，Flash 与 Pro 模型 | 两者均 HTTP 500 |
| 官方首页 Python 示例，根地址、非流式、开启思考、high | 约 15.44 秒返回 HTTP 500 |
| 同一官方示例，客户端 `trust_env=False`，避开进程代理 | 约 15.28 秒返回 HTTP 500 |

官方示例请求标识分别为 `56eef325-4e04-4543-9950-289b3bd23f6f` 与 `392738df-8d59-49e8-b7b9-5b382556caa6`。调用使用当前 Key；输出和记录未包含凭据。

这些结果表明当时错误可在独立官方示例中复现。用户随后反馈 DeepSeek 出现过服务故障；复验中同一配置已成功完成真实交流，当前验收阻塞已解除。

## 后续阶段

- 进程内消息已可恢复到页面；数据库保存及退出重启恢复属于阶段 04。
- 未完成语音的文字接管、语音状态保持属于阶段 08；完整能力恢复与失败消息重新发送属于阶段 09。
