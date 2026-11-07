import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  const [snapshot, setSnapshot] = useState<DesktopSnapshot>();
  const [view, setView] = useState<DesktopConnection['view']>();
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);

  useEffect(() => {
    let active = true;
    let connected = false;
    let sequence = -1;
    const buffered: DesktopSnapshot[] = [];
    function apply(next: DesktopSnapshot) {
      if (active && next.sequence > sequence) {
        sequence = next.sequence;
        setSnapshot(next);
      }
    }
    function onState(event: Event) {
      const next = (event as CustomEvent<DesktopSnapshot>).detail;
      if (connected) apply(next);
      else buffered.push(next);
    }
    async function connect() {
      try {
        const initial = await window.pywebview!.api.connect();
        if (!active) return;
        setView(initial.view);
        document.body.className = initial.view;
        // 先订阅事件再取快照，桥接往返期间的推送按序号衔接。
        apply(initial);
        connected = true;
        buffered.forEach(apply);
        buffered.length = 0;
      } catch (reason) {
        if (active) setError(`桌面连接失败：${String(reason)}`);
      }
    }
    window.addEventListener('agent-state', onState);
    window.addEventListener('pywebviewready', connect);
    if (window.pywebview) void connect();
    return () => {
      active = false;
      window.removeEventListener('agent-state', onState);
      window.removeEventListener('pywebviewready', connect);
    };
  }, []);

  useEffect(() => {
    if (followLatest.current && messagesRef.current) {
      messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
    }
  }, [snapshot]);

  async function sendText() {
    const text = draft.trim();
    if (!text || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    try {
      await window.pywebview!.api.send_text(text);
      setDraft(current => current === draft ? '' : current);
      followLatest.current = true;
      setError('');
    } catch (reason) {
      setError(`发送失败：${String(reason)}`);
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  }

  async function command(name: string) {
    try {
      await window.pywebview!.api.command(name);
      setError('');
    } catch (reason) {
      setError(`操作失败：${String(reason)}`);
    }
  }

  if (!snapshot || !view) return <main><p role="status">{error || '正在连接桌面…'}</p></main>;
  const state = snapshot.state;
  const symbol = <span className="wave-symbol" aria-hidden="true"><i /><i /><i /><i /><i /></span>;

  if (view === 'floating') {
    return (
      <div className="floating-host">
        {state.preview_expanded && <section className="floating-card">
          <header>
            <span>悬浮宿主联调</span>
            <button onClick={() => void command('hide_floating')} aria-label="隐藏收音窗">×</button>
          </header>
          <div className="floating-scroll" tabIndex={0}>
            <p className="preview-question">验证圆角卡片、圆球与中间的穿透区域。</p>
            <div className="preview-answer">
              <p>这里显示窗口联调说明。真实语音和问答内容在后续阶段接入。</p>
              <p>后台计数：<output>{state.activity_tick}</output></p>
              <p>此区域可以滚动，按钮可以点击。打开主窗口后，悬浮组件收起。</p>
              <p>关闭只隐藏悬浮组件，后台继续运行。</p>
            </div>
          </div>
          <button className="open-main" onClick={() => void command('show_main')}>打开主窗口</button>
        </section>}
        <button className="floating-orb" onClick={() => void command('toggle_preview_card')}
          aria-label={state.preview_expanded ? '收起联调卡片' : '展开联调卡片'}>{symbol}</button>
        {error && <div role="alert" className="floating-error">{error}</div>}
      </div>
    );
  }

  return (
    <main className="chat-page">
      <header className="chat-header">
        <div><span className="eyebrow">VOICE SUPER AGENT</span><h1>小博</h1></div>
        <div className="actions">
          <button onClick={() => void command('hide_main')}>隐藏到托盘</button>
          <button onClick={() => void command('quit')}>退出应用</button>
        </div>
      </header>
      <section className="chat-panel">
        <div className="chat-status" role="status" aria-live="polite">
          <span className={`status-dot ${state.model_available ? 'ready' : ''}`} />
          {state.model_available ? (state.active_reply_id ? '正在回复 · 可发送新消息接管' : '文字对话已就绪') : '文字对话暂不可用'}
        </div>
        {state.model_error && <p role="alert" className="model-error">{state.model_error}</p>}
        <div className="messages" ref={messagesRef} aria-label="对话记录" tabIndex={0}
          onScroll={event => {
            const element = event.currentTarget;
            followLatest.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
          }}>
          {snapshot.messages.length === 0 && <div className="chat-empty">
            <div className="voice-symbol">{symbol}</div>
            <h2>你好，我是小博</h2><p>有什么想聊的？</p>
          </div>}
          {snapshot.messages.map(message => <article className={`message ${message.role}`} key={message.id}
            data-message-id={message.id} data-status={message.status}>
            <div className="message-author">{message.role === 'user' ? '你' : '小博'}</div>
            {message.text && <div className="message-text">{message.text}</div>}
            {message.status === 'queued' && <span className="message-progress">等待处理…</span>}
            {message.status === 'thinking' && <span className="message-progress">正在思考…</span>}
            {message.status === 'streaming' && <span className="message-progress">正在回复…</span>}
            {message.error && <p className="message-error" role="alert">{message.error}</p>}
          </article>)}
        </div>
        <form className="composer" onSubmit={event => { event.preventDefault(); void sendText(); }}>
          <label className="sr-only" htmlFor="message-input">输入消息</label>
          <textarea id="message-input" placeholder="输入消息，Enter 发送，Shift + Enter 换行"
            value={draft} disabled={!state.model_available} rows={2}
            onChange={event => setDraft(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                event.preventDefault();
                if (state.model_available) void sendText();
              }
            }} />
          <button type="submit" disabled={!state.model_available || !draft.trim() || sending}>
            {sending ? '发送中…' : '发送'}
          </button>
        </form>
        {error && <p role="alert" className="error">{error}</p>}
      </section>
      {state.desktop_check && <section className="desktop-check">
        <h2>桌面联调</h2>
        <p>真实后台任务每秒推送 20 次状态，可在窗口隐藏或重载后继续查看。</p>
        <div className="check-status">后台计数 <output>{state.activity_tick}</output> · 状态序号 {snapshot.sequence}</div>
        <div className="actions">
          <button onClick={() => void command(state.activity_running ? 'stop_activity' : 'start_activity')}>
            {state.activity_running ? '停止后台活动' : '开始后台活动'}
          </button>
          <button onClick={() => void command('preview_floating')}>预览悬浮宿主</button>
        </div>
      </section>}
      <footer>关闭窗口后继续在托盘中运行 · 当前对话在本次运行期间保留</footer>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
