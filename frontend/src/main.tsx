import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { MainWindow } from './MainWindow';

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

  if (!snapshot || !view) return <main className="connection-page"><p role="status">{error || '正在连接桌面…'}</p></main>;
  const state = snapshot.state;
  const historyLabel = {idle: '', reading: '正在读取历史…', found: '已读取历史', not_found: '未找到相关历史',
    needs_clarification: '需要更多历史线索', invalid_arguments: '历史查询条件无效', failed: '历史读取失败', cancelled: '历史读取已取消'}[state.history_status];
  const voiceLabel = {off: '语音未启动', standby: '待唤醒', starting: '正在准备', active: '语音交互中', stopping: '正在结束', error: '语音不可用'}[state.voice_status];
  const latestVoiceQuestion = snapshot.messages.filter(message => message.role === 'user' && message.source === 'voice'
    && message.status !== 'backchannel').at(-1);
  const latestVoiceReply = snapshot.messages.find(message => message.id === latestVoiceQuestion?.reply_id);
  const capabilityError = state.voice_error || state.model_error;
  const symbol = <span className="wave-symbol" aria-hidden="true"><i /><i /><i /><i /><i /></span>;

  if (view === 'floating') {
    return (
      <div className="floating-host">
        {state.preview_expanded && <section className="floating-card">
          <header>
            <span>{voiceLabel}</span>
            <button onClick={() => void command('hide_floating')} aria-label="隐藏收音窗">×</button>
          </header>
          <div className="floating-scroll" tabIndex={0}>
            <p className="preview-question">{latestVoiceQuestion?.text || '等待你开始交流'}</p>
            <div className="preview-answer">
              {latestVoiceReply?.text && <p>{latestVoiceReply.text}</p>}
              {state.history_reply_id === latestVoiceReply?.id && historyLabel && <p role="status">{state.history_error || historyLabel}</p>}
              {capabilityError && <p className="message-error" role="alert">{capabilityError}</p>}
              {!capabilityError && latestVoiceReply?.error && <p className="message-error" role="alert">{latestVoiceReply.error}</p>}
              {state.voice_recovery === 'running' && <p role="status">正在恢复语音…</p>}
              {state.voice_recovery === 'succeeded' && state.voice_status === 'standby' &&
                <p role="status">语音已恢复，等待再次唤醒。</p>}
            </div>
          </div>
          <div className="floating-actions">
            {state.voice_status === 'error' && <button className="open-main" onClick={() => void command('recover_voice')}
              disabled={!state.model_available || !!state.storage_error || state.exiting}>恢复语音</button>}
            <button className="open-main" onClick={() => void command('show_main')}>打开主窗口</button>
          </div>
        </section>}
        <button className={`floating-orb ${capabilityError ? 'has-error' : ''}`} onClick={() => void command('toggle_preview_card')}
          title={capabilityError || voiceLabel}
          aria-label={`${voiceLabel}，${state.preview_expanded ? '收起卡片' : '展开卡片'}`}>{symbol}</button>
        {error && <div role="alert" className="floating-error">{error}</div>}
      </div>
    );
  }

  return <MainWindow snapshot={snapshot} connectionError={error} />;

}

createRoot(document.getElementById('root')!).render(<App />);
