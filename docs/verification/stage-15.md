# 完整流程验收与性能测量

日期：2026-11-16。分支：`feature/integration-validation`，起点：`main` 的 `e5421fa`。对应[实施计划](../plan/V1.0.0/15-integration-validation.md)和 [AC-01～AC-39 验收矩阵](stage-15-acceptance.md)。

本轮按用户确认的范围完成自动化、真实云服务接口和性能测量。耳机、扬声器、真实发言及设备插拔稍后安排。所有测试数据库和 WebView 配置位于独立临时目录，实际开发对话库和 `.env` 未被改写。

## 环境与测量边界

| 项目 | 本轮条件 |
| --- | --- |
| 系统与硬件 | Windows 11 x64，10.0.26200；机械革命翼龙 15 Pro；AMD Ryzen 7 8845H，8 核 16 线程，物理内存总量 23.29 GiB |
| 桌面 | pywebview 6.2.1、EdgeChromium / WebView2；192 DPI、200% 缩放；主窗口内容区 947 × 645 CSS px |
| Python / Pipecat | Python 3.12.13；独立 uv 环境；Pipecat `de8998789d5690073ddd1dafcec7268839af8153`，SDK 自报 1.14.1.dev387 |
| 真实云端 | 本地 `.env` 中的 DeepSeek `deepseek-flash`、火山 ASR/TTS、System One；使用本机当时的现有网络，未控制公网抖动 |
| 默认设备枚举 | 麦克风阵列（2- Senary Audio）；播放端点 QSN-CBB（NVIDIA High Definition Audio），属于显示器音频端点 |
| 音频使用 | 真实服务探测中，TTS 音频仅保存在内存，重采样后提交给 ASR；本轮没有打开麦克风或播放流 |
| 界面与资源 | 本地 SSE 模型服务、真实 Python 宿主和生产前端；语音消息、状态及音量由验收脚本注入 |

## 自动化与桌面结果

| 检查 | 结果 | 覆盖与限制 |
| --- | --- | --- |
| Python 核心测试 | **186 passed**，49.65 秒 | 保存/恢复、轮次、打断、迟到结果、历史工具取消、窗口预算、设置和设备生命周期；音频与云端使用测试替身 |
| 新增连续集成场景 | 包含在上述 186 项中 | 同一 Runtime 连续执行唤醒、语音、附和、接管、文字切入、结束、重新开始、历史读取、语音失败、文字继续、恢复与重启 |
| 前端类型检查与生产构建 | 通过 | `npm --prefix frontend run build` 包含 TypeScript 检查 |
| 现有浏览器回归 | **2 passed** | 主聊天区重新开始成功/失败后的滚动位置与跟随 |
| 设置与历史页面 CLI 验收 | **22 项通过** | 设置有效值/待应用值、保存失败、20 条分页、Unicode 原文续读、迟到读取隔离、保留草稿、能力缺失、627 × 445 窄视口及失败刷新；桥接使用内存样例 |
| Windows 原生验收 | **21 项通过** | 真实桥接、流式文字接管、主窗口关闭/最小化恢复、悬浮形状与悬停、隐藏找回、原生窗口实际可见性、错误恢复入口、长回复滚动及焦点；结束按钮验证命令发送，实际音频停止由音频测试及后续实机验收覆盖 |

Python 输出含两类现有依赖弃用提示，以及 `record_property` 与 JUnit xunit2 的兼容提示，共 4 条 warning。全部测试无失败。测试夹具用较短的超时策略驱动用户轮次，真实 Smart Turn 的声学断句仍待实机验证。

原生验收使用普通 WinForms 文本窗口承接焦点。首次显示悬浮窗、持续音量更新、隐藏后找回三处均确认前台仍为该窗口；其他进程应用中的实际键盘输入列入人工复核。

## 实测发现与修复

**D-15-01：悬浮窗自动出现时抢焦点，已修复。** 在普通文本窗口已获前台的条件下，出现悬浮问答后，前台句柄变成悬浮窗句柄；此时 `WS_EX_NOACTIVATE` 样式仍在。根因是显示路径调用 WinForms `Form.Show()`，仍会主动选择窗口。

经用户许可，`desktop/host.py` 改为 `ShowWindow(hwnd, SW_SHOWNOACTIVATE)`，原生句柄参数使用 Windows 类型声明。该调用明确要求显示时保留原活动窗口，见 [Microsoft ShowWindow 文档](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow)。`scripts/validate_desktop.py` 保留上述三处原生焦点回归，修复后全部通过。焦点接收窗口未能获得前台、或被外部应用切走时，脚本记录 `inconclusive`，该结果需要复核。

## 真实服务与性能

数字和检查结果保存于[测量结果 JSON](stage-15-results.json)。下表单位为毫秒，服务探测仅为小样本，文字为不同场景，未据此计算服务 P95。

| 场景 | 起止点 | 实测 | 用户确认的开发参考目标 |
| --- | --- | --- | --- |
| 首次文字 | `send_text` 至首个文字事件 / 回复完成 | 943.8 / 1122.6 | 普通文字首段 ≤2000 |
| 共享上下文追问 | 同上 | 706.1 / 868.0，正确返回项目代号 | 普通文字首段 ≤2000 |
| 重新开始后回忆 | 发问至首段文字 / 最终回答完成 | 637.4 / 2737.1；实际依次调用 `history_search`、`history_read` | 最终历史回答暂未设数值目标 |
| ASR 连接就绪 | 调用 `connect()` 至成功返回，3 次 | 297.4～363.3，中位 310.8 | ≤1000 |
| TTS 首音频块 | 请求短句至首个 PCM 块，3 次 | 484.8～588.0，中位 509.0 | ≤1000 |
| System One | 输入已有转写与上下文至判意结果，三个不同表达 | 附和 299.6；两次接管 235.4、259.2；3/3 符合预期 | ≤500 |
| 2 万条历史检索 | 单个中文关键词调用至查询返回，30 次 | 中位 28.44，P95 29.84，最大 30.48 | ≤100 |
| 本地文字渲染 | 发送操作开始至 WebView DOM 出现首段内容 | 118.8，本地 SSE 服务 | 暂未设数值目标 |

这些参考目标由用户在本轮确认，适用于当前开发机和服务条件。它们用于后续比较与定位，当前数据不足以形成稳定的公网服务分位数承诺。

历史回忆的首段是“我先检索一下……”之类的工具前说明，最终答案在约 2.74 秒完成；637.4 毫秒不能解释为完整回忆答案的延迟。新 Runtime 重启后的真实模型追问也正确返回原代号。

ASR 输入为约 3.99 秒的真实 TTS 合成音频，以 16 kHz、200 毫秒分块接近实时供帧，并追加 1 秒静音。三次从连接就绪至首个定稿分别约 **4.63、6.35、6.22 秒**，识别均包含“语音接口验收”和“琥珀松鼠”。该时间包含整段语音输入，不能作为用户说完后的识别延迟。TTS 首 PCM 块也尚未经过设备缓冲，不能作为首个可听声音的延迟。

### 历史与本地唤醒

使用独立临时 SQLite 库，2 千/2 万条合成展示及原生消息，序列化原文约 1.52/15.20 MB；同一打开连接各测 30 次，涵盖首次及缓存后的调用。下表为中位数 / P95，单位毫秒。

| 数据规模 | 历史列表 20 条 | 关键词检索 | 单条原文读取 | 工作窗口投影 |
| --- | --- | --- | --- | --- |
| 2000 条 | 0.304 / 0.450 | 1.955 / 2.471 | 0.283 / 0.416 | 0.538 / 0.835 |
| 20000 条 | 0.469 / 0.590 | 28.438 / 29.842 | 0.715 / 1.124 | 2.147 / 2.502 |

投影后输入约 30.9 KB，保持在既定预算内。2000 条投影有一次 69.48 毫秒的最大值，保留在 JSON 中；本地查询测量与真实服务探测并行执行，含初始化和调度影响，不能只凭中位数推断所有调用耗时。

真实中文 KWS 模型初始化 3 次为 1045.6～1230.5 毫秒，中位 1053.4 毫秒。离线批量处理 10 秒静音耗时 217.0 毫秒、CPU 时间 203.1 毫秒。这仅验证模型装载与静音处理成本，尚未验证真实“你好小博”的命中、误唤醒或持续采集占用。

### 桌面资源

每种场景采样 10 秒、间隔 500 毫秒，共 20 个区间，统计 Python 宿主及其 WebView2 子进程。CPU 由进程 user+system 时间增量计算；整机占比除以 16 个逻辑核。表中内存为各进程 private bytes 合计的平均值，RSS 为区间最大合计值；RSS 包含共享页面重复计数，二者不能直接相减。采样期间进程集合未变化。

| 场景 | 单逻辑核 CPU % | 整机 CPU % | 平均私有内存 MiB | 最大 RSS 合计 MiB |
| --- | --- | --- | --- | --- |
| 主窗口显示、语音关闭 | 4.54 | 0.28 | 879.99 | 610.84 |
| 主窗口显示、本地模型流式生成 | 26.59 | 1.66 | 941.33 | 676.81 |
| 主窗口隐藏、语音关闭 | 5.13 | 0.32 | 970.42 | 686.83 |
| 悬浮窗、每秒 20 次受控音量更新 | 57.51 | 3.59 | 976.81 | 721.20 |

各场景按上述顺序运行，后续内存包含前面加载、生成和渲染形成的缓存。短时数据无法判断长期增长。语音关闭时已加载 Runtime 的相关依赖，私有内存接近 0.9 GiB，后续资源优化应以这些现状为依据。悬浮场景没有 AEC、音频设备或云端语音流，完整待唤醒/交互的资源尚待测量。

## 剩余实测

| 编号 | 待执行内容 | 关联验收 |
| --- | --- | --- |
| M-15-01 | 耳机下实际说唤醒词、按住组合键、就绪叮声、Smart Turn 分句、连续追问、附和和接管 | AC-02～AC-12、AC-28、AC-33、AC-35 |
| M-15-02 | 耳机及扬声器分别静默播放、附和和真实发言接管；记录距离、音量和回声情况 | AC-19，以及 AC-06～AC-08、AC-32 |
| M-15-03 | 实际改默认设备、插拔及失效恢复；覆盖本地 KWS 和活跃对话 | AC-36、AC-38 |
| M-15-04 | 用户轮次结束至首段可听回复、接管至实际停播、完整语音待唤醒/交互资源与长时常驻 | T-12；声学性能目标待实测后确认 |
| M-15-05 | 在其他实际应用中保留键盘输入焦点，覆盖唤醒、窗口自动显示及音量持续更新 | AC-03、AC-21 |
| D-16-01 | 新工作副本独立准备资源和凭据，脱离 STS 目录完成真实语音流程 | AC-26，由阶段 16 完成 |

本轮发现的焦点缺陷已修复；上述尚未执行项均保留待验收状态，完整声学流程的结论随后补充。

## 复现

在本项目根目录运行，Python 统一使用 uv。测量结果默认写入 `.local/verification/stage-15/`。三个 Python 测量脚本使用现有 Windows 依赖中的 psutil，未新增数据库迁移。

```powershell
uv run --locked pytest -q --junitxml=.local/verification/stage-15/pytest.xml
npm --prefix frontend run build
npm --prefix frontend test
uv run --locked python scripts/measure_local.py
uv run --locked python scripts/measure_services.py --live-services
uv run --locked python scripts/validate_desktop.py --seconds 10
```

`measure_local.py` 需要先按资源说明准备本地 KWS 模型。`measure_services.py --live-services` 读取 `.env` 并产生真实服务调用；它使用临时库和内存音频。`validate_desktop.py` 会显示原生窗口、移动指针验证悬停并关闭测试窗口；运行时应留出桌面，避免人工切走焦点。脚本的 `focus.status` 需要为 `passed` 才能确认焦点结果。

页面验收先在单独终端启动 `npm --prefix frontend run dev -- --host 127.0.0.1 --port 5179`，再在项目根目录执行：

```powershell
New-Item -ItemType Directory -Force .local/verification/stage-15/output/playwright | Out-Null
Push-Location .local/verification/stage-15
npx --yes --package @playwright/cli playwright-cli -s=stage15 open http://127.0.0.1:5179
npx --yes --package @playwright/cli playwright-cli -s=stage15 snapshot
npx --yes --package @playwright/cli playwright-cli -s=stage15 run-code --filename=../../../scripts/validate_pages.js --raw
npx --yes --package @playwright/cli playwright-cli -s=stage15 close
Pop-Location
```

页面输出应包含 `passed: 22`，截图写入 `output/playwright/`。CLI 输出包含错误时应按错误处理，即使外层进程退出码为 0。
