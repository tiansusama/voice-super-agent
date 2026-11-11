import { useEffect, useRef, useState } from 'react';
import { ArrowDown, AudioLines, Check, CircleStop, Clock3, CornerDownLeft, History,
  MessageCircle, RotateCcw, SendHorizontal, Settings2, UserRound } from 'lucide-react';

export function MainWindow({ snapshot, connectionError }: { snapshot: DesktopSnapshot; connectionError: string }) {
  const [page, setPage] = useState<'chat' | 'history' | 'settings'>('chat');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);
  const [following, setFollowing] = useState(true);
  const sendingRef = useRef(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const followLatest = useRef(true);
  const state = snapshot.state;
  const voiceBusy = ['starting', 'active', 'stopping'].includes(state.voice_status);
  const voiceSwitching = ['starting', 'stopping'].includes(state.voice_status);
  const canOperate = !state.exiting && !state.restarting && !state.storage_error && !state.exit_error;
  const canSend = state.model_available && canOperate && !voiceSwitching;
  const failedReplies = new Set(snapshot.messages.filter(message => message.role === 'assistant' && message.status === 'failed')
    .map(message => message.id));
  const voiceLabel = { off: '语音未启动', standby: '语音待唤醒', starting: '正在准备语音', active: '语音交互中',
    stopping: '正在结束语音', error: '语音不可用' }[state.voice_status];
  const activityLabel = { listening: '正在聆听', recognizing: '正在识别', classifying: '正在判断接管',
    thinking: '正在思考', speaking: '正在播报' }[state.voice_activity];
  const historyLabel = { idle: '', reading: '正在读取历史…', found: '已读取历史', not_found: '未找到相关历史',
    needs_clarification: '需要更多历史线索', invalid_arguments: '历史查询条件无效', failed: '历史读取失败',
    cancelled: '历史读取已取消' }[state.history_status];
  // 文字回复与收音状态独立，生成中的文字回复优先于持续聆听的提示。
  const processingLabel = state.storage_error ? '保存异常 · 已暂停新消息' : state.exit_error ? '退出未完成' :
    state.exiting ? '正在保存并退出…' : state.restarting ? '正在重新开始…' :
    state.history_status === 'reading' ? '正在读取历史' :
    state.voice_status === 'active' && state.voice_activity !== 'listening' ? activityLabel :
    state.active_reply_id ? '正在回复' : state.voice_status === 'active' ? activityLabel :
    !state.model_available ? '模型未就绪' : '';

  useEffect(() => {
    // 跟随最新内容时推进滚动；用户向上阅读后保留当前位置。
    if (page === 'chat' && followLatest.current) {
      const messages = messagesRef.current!;
      messages.scrollTop = messages.scrollHeight;
    }
  }, [page, snapshot]);

  useEffect(() => {
    const input = inputRef.current!;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
  }, [draft, page]);

  async function command(name: string) {
    try {
      await window.pywebview!.api.command(name);
      setError('');
    } catch (reason) {
      setError(`操作失败：${String(reason)}`);
    }
  }

  async function sendText(originalMessageId?: string) {
    const text = draft.trim();
    if (!canSend || (!originalMessageId && !text) || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    try {
      if (originalMessageId) await window.pywebview!.api.resend_text(originalMessageId);
      else {
        await window.pywebview!.api.send_text(text);
        setDraft(current => current === draft ? '' : current);
      }
      followLatest.current = true;
      setFollowing(true);
      setError('');
      inputRef.current!.focus();
    } catch (reason) {
      setError(`发送失败：${String(reason)}`);
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  }

  return (
    <div className="desktop-shell">
      <aside className="sidebar" aria-label="主导航">
        <nav>
          <button className={`nav-item ${page === 'chat' ? 'selected' : ''}`} aria-current={page === 'chat' ? 'page' : undefined}
            onClick={() => setPage('chat')}><MessageCircle size={25} strokeWidth={1.6} /><span>对话</span></button>
          <button className={`nav-item ${page === 'history' ? 'selected' : ''}`} aria-current={page === 'history' ? 'page' : undefined}
            onClick={() => setPage('history')}><Clock3 size={25} strokeWidth={1.6} /><span>历史记录</span></button>
        </nav>
        <button className={`nav-item settings-nav ${page === 'settings' ? 'selected' : ''}`} aria-current={page === 'settings' ? 'page' : undefined}
          onClick={() => setPage('settings')}><Settings2 size={25} strokeWidth={1.6} /><span>设置</span></button>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <div className="agent-heading">
            <h1>{page === 'chat' ? '小博' : page === 'history' ? '历史记录' : '设置'}</h1>
            <div className="agent-status" role="status" aria-live="polite">
              <span className={`voice-status ${state.voice_status}`}><span className="status-dot" />{voiceLabel}</span>
              {processingLabel && <span className="processing-status">{processingLabel}</span>}
            </div>
          </div>
          <div className="header-actions">
            {['starting', 'active'].includes(state.voice_status) &&
              <button className="quiet-button end-voice" aria-label="结束语音" onClick={() => void command('end_voice')} disabled={state.exiting}>
                <CircleStop size={17} /><span>结束语音</span></button>}
            {!voiceBusy && state.voice_status !== 'error' &&
              <button className="quiet-button wake-voice" aria-label="唤醒语音" onClick={() => void command('wake_voice')}
                disabled={!canOperate || !state.model_available}><AudioLines size={18} /><span>唤醒语音</span></button>}
            <button className="quiet-button restart-button" aria-label="重新开始" onClick={() => void command('restart_conversation')}
              disabled={!canOperate}><RotateCcw size={18} /><span>重新开始</span></button>
          </div>
        </header>

        <div className="notices" aria-label="应用提示">
          {state.voice_error && <div className="notice warning" role="alert"><p>{state.voice_error}</p>
            {state.voice_status === 'error' && <button className="notice-action" onClick={() => void command('recover_voice')}
              disabled={!canOperate || !state.model_available || state.voice_recovery === 'running'}>
              <RotateCcw size={15} />恢复语音</button>}</div>}
          {state.model_error && <div className="notice warning" role="alert"><p>{state.model_error}</p></div>}
          {state.storage_error && <div className="notice warning" role="alert"><p>{state.storage_error}</p></div>}
          {state.exit_error && <div className="notice warning" role="alert"><p>{state.exit_error}</p>
            <button className="notice-action" onClick={() => void command('quit')} disabled={state.exiting}>再次退出</button></div>}
          {state.voice_recovery === 'running' && <div className="notice" role="status">正在恢复语音…</div>}
          {state.voice_recovery === 'succeeded' && state.voice_status === 'standby' &&
            <div className="notice" role="status"><Check size={16} />语音已恢复，等待再次唤醒。</div>}
          {(error || connectionError) && <div className="notice warning" role="alert"><p>{error || connectionError}</p></div>}
        </div>

        {/* 页面切换保留聊天节点，草稿与阅读位置随主窗口持续存在。 */}
        <div className="conversation-view" hidden={page !== 'chat'}>
          <div className="messages" ref={messagesRef} aria-label="对话记录" tabIndex={0}
            onScroll={event => {
              const element = event.currentTarget;
              followLatest.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
              setFollowing(followLatest.current);
            }}>
            {snapshot.messages.length === 0 && <div className="chat-empty">
              <div className="welcome-symbol"><AudioLines size={30} strokeWidth={1.8} /></div>
              <h2>你好，我是小博</h2><p>有什么想聊的？我们接着说。</p>
              <span>说“{state.wake_word}”，听到叮声后开始交流</span>
            </div>}
            {snapshot.messages.filter(message => !(message.role === 'assistant' && message.status === 'unfinished' && !message.text))
              .map(message => <article className={`message ${message.role}`} key={message.id}
                data-message-id={message.id} data-status={message.status}>
                <div className="message-author">
                  <span className="avatar">{message.role === 'user' ? <UserRound size={20} strokeWidth={1.6} /> : <AudioLines size={21} strokeWidth={1.9} />}</span>
                  <span>{message.role === 'user' ? '我' : '小博'}</span>
                </div>
                <div className="message-body">
                  {message.text && <div className="message-text">{message.text}</div>}
                  {message.status === 'queued' && <span className="message-progress">等待处理…</span>}
                  {state.history_reply_id === message.id && historyLabel &&
                    <span className="message-progress" role="status">{state.history_error || historyLabel}</span>}
                  {message.status === 'thinking' && !(state.history_reply_id === message.id && state.history_status === 'reading') &&
                    <span className="message-progress">正在思考…</span>}
                  {message.status === 'streaming' && <span className="message-progress">
                    {message.role === 'user' ? '正在识别…' : '正在回复…'}</span>}
                  {message.error && <p className="message-error" role="alert">{message.error}</p>}
                </div>
                {message.role === 'user' && message.source === 'text' && failedReplies.has(message.reply_id) &&
                  <button className="resend-message" disabled={!canSend || sending} onClick={() => void sendText(message.id)}>
                    <RotateCcw size={13} />重新发送</button>}
              </article>)}
          </div>
          {!following && <button className="latest-button" onClick={() => {
            followLatest.current = true;
            setFollowing(true);
            messagesRef.current!.scrollTop = messagesRef.current!.scrollHeight;
          }}><ArrowDown size={14} />回到最新</button>}
          <div className="composer-area">
            <form className="composer" onSubmit={event => { event.preventDefault(); void sendText(); }}>
              <label className="sr-only" htmlFor="message-input">输入消息</label>
              <textarea ref={inputRef} id="message-input" placeholder={canSend ? '输入消息，接着聊…' : '文字对话暂不可用'}
                value={draft} disabled={!canSend} rows={1} onChange={event => setDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                    event.preventDefault();
                    void sendText();
                  }
                }} />
              <button className="send-button" type="submit" disabled={!canSend || !draft.trim() || sending}
                aria-label={sending ? '发送中' : '发送消息'} title="发送消息"><SendHorizontal size={22} strokeWidth={1.8} /></button>
            </form>
            <div className="composer-meta">
              <span className="input-hint"><span><CornerDownLeft size={12} />Enter 发送</span><span>Shift + Enter 换行</span>
                <span className="hotkey-hint">{state.wake_hotkey} · 按住 1 秒唤醒</span></span>
              <span className="save-status" role="status">{state.storage_status === 'saved' ? '已保存在本机' :
                state.storage_status === 'saving' ? '正在保存…' : '保存异常'}</span>
            </div>
          </div>
        </div>

        {page === 'history' && <section className="info-page" aria-label="历史记录">
          <div className="page-intro"><span className="page-symbol"><History size={25} strokeWidth={1.5} /></span>
            <h2>聊过的事，都可以再问问</h2><p>对话原文保存在本机。告诉小博具体的主题、关键词或大致时间，它会按需查找并回忆。</p></div>
          <div className="info-card"><h3>接着之前的话题</h3><p>试着问：“我们之前讨论的唤醒方式是什么？”</p>
            <button className="quiet-button" onClick={() => setPage('chat')}><MessageCircle size={17} />回到对话</button></div>
          <p className="page-note">“重新开始”会腾空当前对话窗口，过去的内容仍可按需查询。</p>
        </section>}

        {page === 'settings' && <section className="info-page" aria-label="当前设置">
          <div className="page-intro"><h2>让交流更顺手</h2><p>当前生效的语音设置</p></div>
          <dl className="settings-list">
            <div><dt>唤醒词<small>听到“叮”声后再说指令</small></dt><dd>{state.wake_word}</dd></div>
            <div><dt>唤醒组合键<small>按住 1 秒，松开后继续交流</small></dt><dd><kbd>{state.wake_hotkey}</kbd></dd></div>
            <div><dt>空闲超时<small>等待用户输入时计时，处理中暂停</small></dt><dd>{state.voice_idle_seconds} 秒</dd></div>
          </dl>
          <div className="info-card service-settings"><h3>模型与语音服务</h3><p>服务参数在项目的本地 .env 中配置，修改后重启应用生效。</p>
            <dl><div><dt>对话模型</dt><dd>{state.model}</dd></div><div><dt>录音设备</dt><dd>{state.microphone_device || 'Windows 默认录音设备'}</dd></div>
              <div><dt>播放设备</dt><dd>{state.playback_device || 'Windows 默认播放设备'}</dd></div></dl>
            {state.audio_device_pending && <p role="status">默认设备已变化，下次唤醒时切换。</p>}</div>
          <div className="window-actions"><button className="quiet-button" onClick={() => void command('hide_main')}>隐藏到托盘</button>
            <button className="quiet-button" onClick={() => void command('quit')} disabled={state.exiting}>退出应用</button></div>
        </section>}

        {state.desktop_check && <section className="desktop-check"><details><summary>桌面联调 · 后台计数 {state.activity_tick} · 状态序号 {snapshot.sequence}</summary>
          <div className="actions"><button onClick={() => void command(state.activity_running ? 'stop_activity' : 'start_activity')}>
            {state.activity_running ? '停止后台活动' : '开始后台活动'}</button>
            <button onClick={() => void command('preview_floating')}>预览悬浮宿主</button></div>
        </details></section>}
      </main>
    </div>
  );
}
