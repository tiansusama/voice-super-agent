# 阶段 02 验证记录

日期：2026-11-07。结果：阶段 02 完成。

对应 [桌面常驻、窗口与桥接计划](../plan/V1.0.0/02-desktop-host.md)，启动方式见 [开发运行](../development.md)。

## 实际环境与实现

沿用阶段 01 的本项目 uv 环境、CPython 3.12.13、pywebview 6.2.1 与 EdgeChromium 渲染器。实测屏幕为 3840 × 2160，Windows 缩放 200%；`GetDpiForWindow` 返回 192，WebView 的 `devicePixelRatio` 为 2。

一个 Python 应用实例中，入口调用 pywebview，Windows 渲染器管理 WinForms GUI 线程；`agent-runtime` 线程运行 asyncio；`desktop-events` 线程发送页面状态；`instance-notify` 线程等待重复启动通知。WebView2 浏览器子进程由渲染器管理。

| 位置 | 本阶段职责 |
| --- | --- |
| `desktop/instance.py` | 命名 Mutex 与激活 Event、通知监听及句柄释放 |
| `desktop/host.py` | 窗口、托盘、原生 GUI 调度、悬浮区域与状态发送 |
| `runtime.py` | 后台命令、窗口状态、序号快照、订阅及可取消联调活动 |
| `frontend/src/main.tsx` | 页面订阅、快照衔接、窗口操作及悬浮宿主预览 |

## 实机结果

构建模式使用 `uv run --locked voice-super-agent --desktop-check`；另行启动 Vite 后验证 `--dev --desktop-check`。Playwright 通过仅在验收进程设置的 WebView2 调试端口连接实际页面，Windows API 与 UI Automation 用于操作原生窗口和托盘。

| 场景 | 实际结果 |
| --- | --- |
| 重复启动 | 第二个进程通知后退出；原主窗口恢复，Python 宿主进程和窗口句柄保持一致 |
| 关闭主窗口 | 发送系统关闭操作后窗口隐藏，后台计数继续增长，托盘保留 |
| 最小化与恢复 | 原生状态变为最小化；再次启动后恢复同一主窗口 |
| 托盘双击 | 主窗口隐藏后，实际双击托盘图标能够显示主窗口 |
| 托盘菜单 | “显示主窗口”能够找回窗口；通过菜单“退出应用”结束应用 |
| 明确退出 | 持续推送期间退出，活动取消，两个窗口关闭，托盘释放；应用进程结束，日志记录后台循环停止 |
| 页面重载 | 后台活动保持运行；重载后重新订阅，DOM 中计数继续增长，未从零开始 |
| 隐藏与显示 | 连续三次隐藏、显示期间后台活动和状态推送持续运行 |
| 开发服务模式 | 主窗口和悬浮页面均完成订阅；重载后恢复当前计数；明确退出成功 |
| 悬浮区域 | 卡片与圆球参与命中；中间间隙、圆角外部和圆球外部命中后方窗口 |
| 卡片与按钮 | “打开主窗口”及隐藏操作可用；圆球切换卡片展开、收起；收起后原卡片区域可穿透 |
| 滚动区域 | 通过真实滚轮操作，内部 `scrollTop` 从 0 变化为 93，窗口区域保持一致 |
| 悬浮关闭 | 系统关闭操作只隐藏悬浮宿主，后台活动保持运行；托盘保留找回入口 |
| 不抢焦点 | 激活独立测试窗口后由后台显示悬浮宿主，显示前后前台 HWND 均为该测试窗口 |
| 置顶与任务栏 | 原生窗口保有置顶及不激活样式，工具窗口样式移出任务栏；位置按主窗口所在屏幕工作区计算 |

一次持续推送验证中，第二次启动到原窗口可见约 0.37 秒。此值为本机观测结果。联调任务按 50 毫秒间隔产生完整状态，用于验证命令和推送同时工作。

悬浮宿主逻辑尺寸为 320 × 312；卡片高 224，圆球直径 64。在本机缩放下原生尺寸为 640 × 624，底边距工作区边缘 40 个物理像素。截图保存在本工作副本忽略目录 `.local/verification/stage-02-floating.png`。

## 实测发现与落实

1. 在 pywebview 的 `before_show` 中修改 WinForms `ShowInTaskbar`，会重建正在用于 WebView2 初始化的窗口句柄，本机出现 `E_ABORT`。改为在已有 HWND 上设置 `WS_EX_TOOLWINDOW` 并移除 `WS_EX_APPWINDOW`；两个页面均完成初始化。
2. 本机 WinForms `DeviceDpi` 返回 96，而窗口 DPI 为 192。首次预览因此发生裁剪错位，用户截图也呈现了该问题。区域缩放已统一采用 `GetDpiForWindow`；修正后的截图与命中测试通过。
3. `evaluate_js` 等待脚本执行结果，因此由独立发送线程处理；后台只向队列提交状态。连续完整状态可合并，窗口命令仍按原顺序处理。

实现依据包括 [pywebview 6.2.1 WinForms 源码](https://github.com/r0x0r/pywebview/blob/6.2.1/webview/platforms/winforms.py)、[EdgeChromium 源码](https://github.com/r0x0r/pywebview/blob/6.2.1/webview/platforms/edgechromium.py)、[Windows 任务栏窗口样式说明](https://learn.microsoft.com/en-us/windows/win32/shell/taskbar)及 [SetWindowLongW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowlongw)。本机行为同时对照已安装版本源码和实际窗口验证。

## 自动验证

```powershell
npm --prefix frontend run build
uv run --locked python -m unittest discover -s tests -v
uv run --locked voice-super-agent --help
```

前端构建和 TypeScript 检查通过；6 项核心测试通过，覆盖跨进程通知、监听开始前通知保留、进程异常结束后实例释放、快照与后续状态顺序、重新订阅、后台活动期间接收命令、未知命令报错，以及运行中任务取消和线程结束。

## 后续复核范围

当前悬浮卡片展示联调说明与真实后台计数。语音状态驱动的显示与手动隐藏周期、音量环、音波动画、单轮问答替换等，在阶段 06、13 结合真实链路接入与验收。

本次实机验证覆盖当前屏幕与 200% 缩放；不同缩放的多显示器迁移、真实语音推送频率，以及最终布局与拖动在后续整合时继续复核。阶段 02 的窗口与焦点结论用于后续实现，完整 AC-21、AC-22、AC-31、AC-35 仍按计划在阶段 13 复核。
