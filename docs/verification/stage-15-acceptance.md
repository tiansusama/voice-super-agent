# V1.0.0 集成验收矩阵

日期：2026-11-16。需求来自 [PRD 验收场景](../prd/V1.0.0.md#11-验收场景)，环境、测试命令、性能和问题编号见[本轮记录](stage-15.md)。

“自动化通过”表示真实 SDK / Pipecat 与本地服务、音频替身完成验证；“原生通过”表示本机 Windows / WebView2 检查；“真实接口通过”表示实际云服务调用。这些结果分别记录，语音类场景中的真实发言、听感、设备操作继续按 M-15 清单验收。

| 编号 | 条件与操作 | 预期 | 实际结果与状态 | 证据 / 待测 |
| --- | --- | --- | --- | --- |
| AC-01 | 启动、重复实例通知，关闭主窗口再显示 | 单实例、关闭常驻、恢复同一内容 | 进程间通知与实例释放自动化通过；原生关闭后台存活、最小化恢复通过 | `test_instance.py`、`validate_desktop.py` |
| AC-02 | 待唤醒接收关键词事件，等待 ASR 与就绪提示 | 准备成功后发叮声再放行输入 | 自动化验证顺序通过；真实 ASR 连接通过；实际人声唤醒和叮声待验收 | `test_wake_lifecycle.py`、`test_voice.py`；M-15-01 |
| AC-03 | 默认/自定义组合键保持 1 秒、释放和重复触发 | 单次长按一次、松开后保持交互 | 按键状态机与设置自动化通过；其他应用下真实全局按键待验收 | `test_wake_lifecycle.py`、`test_settings_history.py`；M-15-01/05 |
| AC-04 | 同一表达送入多个 ASR 定稿分句 | 一次用户轮次包含完整分句 | 转写分句、聚合和上下文自动化通过；真实 Smart Turn 声学断句待验收 | `test_voice.py::test_multiple_final_asr_segments_form_one_native_user_turn`；M-15-01 |
| AC-05 | 在同一运行时连续补充并追问项目代号 | 保留之前内容并衔接回答 | 自动化通过；真实模型文字追问正确；真实连续发言待验收 | `test_acceptance_flow.py`、`measure_services.py`；M-15-01 |
| AC-06 | 模拟播报中输入“嗯嗯，你继续” | 继续播放，附和展示保存并从模型上下文过滤 | 展示/模型分离自动化通过；真实 System One 正确判为附和；实际回声条件待验收 | `test_interruption.py`、`test_voice.py`；M-15-02 |
| AC-07 | 播报时表达“等一下，我改一下” | 语义接管，保留已有片段并响应新输入 | 原生打断与保存自动化通过；真实判意正确；实际停播待测 | `test_acceptance_flow.py`、`test_interruption.py`；M-15-02/04 |
| AC-08 | 修订重叠转写，让旧判意结果迟到 | 只有当前转写对应的结果生效 | 自动化通过；真实重叠发言仍需声学复核 | `test_interruption.py::test_revised_text_supersedes_a_late_interrupt_decision`；M-15-02 |
| AC-09 | 语音播报中发送 UI 文字，本地流式文字生成中再发送文字 | 打断、提交片段、新文字按顺序执行 | 音频替身自动化及真实桌面文字接管通过 | `test_voice.py`、`validate_desktop.py`；实际播报随 M-15-01 |
| AC-10 | 语音与文字轮流追问同一主题 | 共享上下文，回复形式跟随输入 | 自动化通过；真实文字上下文追问通过；人声交替待验收 | `test_acceptance_flow.py`、`test_voice.py`；M-15-01 |
| AC-11 | Agent 结束动作、收尾，再唤醒继续 | 回待唤醒，释放 ASR，保留上下文 | 结束工具、等待收尾、再次唤醒自动化通过；真实结束意图及收尾听感待验收 | `test_voice.py` 的 agent_end / stopping / rewake 场景；M-15-01 |
| AC-12 | 默认及修改超时，覆盖等待、生成、工具、播放 | 只计算等待用户的时间 | 状态计时、重设及暂停自动化通过；设置页面显示通过 | `test_wake_lifecycle.py`、`test_settings_history.py` |
| AC-13 | 正常退出，分别保存完成和未完成回复后重启 | 恢复内容、状态和工作范围，不自动续写 | SQLite 与 Runtime 自动化通过；真实模型重启后追问正确 | `test_conversation.py`、`test_acceptance_flow.py`、`measure_services.py` |
| AC-14 | 以关键词、主题、时间定位窗口外旧记录 | 查询后按需读取原文，线索不足时追问 | 数据查询、相对时间和工具结果自动化通过；真实模型检索并读取项目代号通过 | `test_history.py`、真实 `history_search` / `history_read` 调用记录 |
| AC-15 | 读取包含中文和 emoji 的长历史 | 原文分段续读，内容连续完整 | 存储自动化及浏览器 Unicode 原文续读通过 | `test_history.py`、`validate_pages.js` |
| AC-16 | 不存在的历史、参数错误和读取失败 | 返回真实状态，允许继续输入 | 自动化通过；页面失败原因与刷新恢复通过 | `test_history.py`、`validate_pages.js` |
| AC-17 | 注入设备、ASR、System One、LLM、TTS 错误 | 保留内容，不重复整轮请求，可恢复或重发 | 自动化通过；原生悬浮错误与恢复入口通过；真实设备故障待测 | `test_recovery.py`、`test_audio.py`、`validate_desktop.py`；M-15-03 |
| AC-18 | 保存有效、无效和应用失败设置，重启读取已存值 | 各项按约定时机生效，失败保留原值 | 自动化及 22 项页面检查中的设置路径通过；本轮未编辑真实 `.env` | `test_settings_history.py`、`validate_pages.js` |
| AC-19 | 耳机及扬声器外放，分别静默、附和和接管 | 回声得到处理，真实用户能接管 | **真实声学未执行**；离线合成回声 AEC 测试通过 | `test_audio.py`；M-15-02/04 |
| AC-20 | 待唤醒和激活中各发送普通文字 | 文字回复保持语音激活状态 | 自动化通过；连续集成场景验证文字回复期间仍 active | `test_voice.py`、`test_acceptance_flow.py` |
| AC-21 | 主窗口隐藏/最小化后进入语音状态，再恢复 | 显示悬浮组件，恢复主窗后切换；自动出现保持焦点 | 可见性自动化及原生检查通过；D-15-01 已修复并复测；实际其他应用输入待复核 | `test_floating.py`、`validate_desktop.py`；M-15-05 |
| AC-22 | 悬停后隐藏，在音量更新中保持隐藏，再找回 | 按钮可达，本轮保持隐藏，托盘入口可恢复 | 自动化及原生悬停、隐藏、找回通过；原生检查调用托盘对应命令 | `test_floating.py`、`validate_desktop.py` |
| AC-23 | 结束语音到待唤醒，再次激活 | 收起后按规则重新显示 | 自动化通过；原生结束命令收起通过 | `test_floating.py`、`validate_desktop.py` |
| AC-24 | 重新开始、浏览/回忆旧记录，再退出重启 | 新工作窗口，旧历史保留，重启恢复新范围 | 自动化、两项滚动回归及真实模型历史回读通过 | `test_context_window.py`、`test_acceptance_flow.py`、`measure_services.py` |
| AC-25 | 工具读取中接管，包含并行调用完成/未完成混合情况 | 已完成结果保留，未完成取消，迟到结果无效 | Pipecat 原生工具取消自动化通过，覆盖文字/语音来源 | `test_history.py` 的 parallel_calls / native_cancel 场景 |
| AC-26 | 新副本准备资源、依赖和凭据，脱离 STS 启动 | 完整项目独立运行语音/文字 | 当前副本使用独立 uv 与锁定依赖；**最终新副本验收留阶段 16** | D-16-01 |
| AC-27 | 语音、附和、文字、回复形成历史，再重开 | 展示/模型状态保存，旧引用可读，音频默认不落盘 | 自动化通过；真实探测音频仅在内存中处理 | `test_store.py`、`test_voice.py`、`test_acceptance_flow.py` |
| AC-28 | 生成或历史回读中尚未播报时插入新语音 | 沿原生用户轮次通路，跳过重叠判意 | 自动化通过；真实发言时序待验收 | `test_voice.py::test_speech_during_model_generation_bypasses_overlap_classifier`、`test_history.py`；M-15-01 |
| AC-29 | 一轮问答完成，再进入下一轮 | 悬浮只显示新一轮，历史仍保留旧轮 | 后台 reply_id 切换和主记录保留自动化通过；原生单轮呈现通过 | `test_floating.py`、`test_acceptance_flow.py`、`validate_desktop.py` |
| AC-30 | 判意期间、附和结果及真正接管依次到达 | 判意/附和保留旧问答，接管后切换 | 自动化通过；真实 System One 三个样例正确 | `test_floating.py::test_candidate_backchannel_and_confirmed_takeover_keep_correct_floating_round`；M-15-02 |
| AC-31 | 悬浮长回复滚动，上滚后更新，再回最新 | 高度受限，保留阅读位置，恢复跟随 | **真实 WebView2 原生检查通过** | `validate_desktop.py` |
| AC-32 | 注入准备/识别/播报与独立输入、播放音量 | 提示正确，外环与音柱各用对应音量 | 音频量计算自动化通过；原生提示、音柱与外环呈现通过；真实声学映射待验收 | `test_audio.py`、`validate_desktop.py`；M-15-02 |
| AC-33 | 未完成语音时发文字，注入旧连接/旧聚合器迟到结果，再发新语音 | 旧语音被替代，迟到结果无效，新语音正常 | 自动化通过，覆盖 ASR 重连缓冲与旧 epoch；真人交错待验收 | `test_voice.py`、`test_volcengine.py`；M-15-01 |
| AC-34 | 语音/文字回答失败，再问或点击原文字重发 | 重发追加新用户消息，保留原片段，按文字接管 | 自动化通过；页面入口沿用已有主界面记录 | `test_recovery.py::test_resend_appends_original_text_and_preserves_failed_partial_reply`、[主界面记录](stage-12.md) |
| AC-35 | 准备/播报时从主窗及悬浮球结束 | 停止当前工作回待唤醒，隐藏按钮只隐藏 | 自动化覆盖准备/生成/播报取消；原生悬浮结束与隐藏命令分别通过；实际停播待测 | `test_voice.py`、`validate_desktop.py`；M-15-01/04 |
| AC-36 | 改默认设备、延后采用、设备失效与恢复 | 按阶段切换，失效明确提示，恢复后待唤醒 | 设备生命周期自动化通过；**实际插拔/切换未执行** | `test_voice.py`、`test_audio.py`；M-15-03 |
| AC-37 | 语音缺失、模型可用，再使模型也缺失 | 文字按能力可用，历史和设置可访问 | 自动化及页面检查通过，覆盖配置修复；原生资源测量使用语音关闭的文字模式 | `test_recovery.py`、`test_settings_history.py`、`validate_pages.js` |
| AC-38 | 可见/隐藏时语音失败，再主动恢复 | 在对应入口反馈，已隐藏保持隐藏，成功回待唤醒 | 自动化通过；原生可见错误入口通过；连续场景验证隐藏时失败仍隐藏及恢复 | `test_floating.py`、`test_acceptance_flow.py`、`validate_desktop.py`；设备实测 M-15-03 |
| AC-39 | 历史分页、查看原文、切回当前交流 | 浏览不修改模型窗口，草稿与当前记录保留 | Runtime 自动化及 22 项浏览器检查通过 | `test_settings_history.py::test_history_browse_and_full_text_do_not_change_working_context`、`validate_pages.js` |

测试文件均位于仓库 `tests/`，验收脚本位于 `scripts/`。本轮自动化无失败；真实语音相关行按所列 M-15 编号继续验收，AC-26 进入阶段 16。
