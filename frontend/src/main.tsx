import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  const [snapshot, setSnapshot] = useState<DesktopSnapshot>();
  const [view, setView] = useState<DesktopConnection['view']>();
  const [error, setError] = useState('');

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
    <main>
      <span className="eyebrow">VOICE SUPER AGENT</span>
      <section className="startup-card">
        <div className="voice-symbol">{symbol}</div>
        <h1>你好，我是小博</h1>
        <p>很高兴与你见面。</p>
        <div className="startup-status" role="status" aria-live="polite">
          <span className="status-dot ready" />桌面与后台已就绪
        </div>
        <div className="actions">
          <button onClick={() => void command('hide_main')}>隐藏到托盘</button>
          <button onClick={() => void command('quit')}>退出应用</button>
        </div>
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
      <footer>关闭窗口后，小博会继续在系统托盘中运行</footer>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
