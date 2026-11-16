import { useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, CircleStop, RotateCcw, X } from 'lucide-react';
import './floating.css';

export function FloatingWindow({ snapshot, connectionError }: { snapshot: DesktopSnapshot; connectionError: string }) {
  const [error, setError] = useState('');
  const [hovered, setHovered] = useState(false);
  const [following, setFollowing] = useState(true);
  const cardRef = useRef<HTMLElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const previousReply = useRef<string | null>(null);
  const layoutQueue = useRef(Promise.resolve());
  const state = snapshot.state;
  const reply = snapshot.messages.find(message => message.id === state.floating_reply_id);
  const question = snapshot.messages.find(message => message.role === 'user' && message.source === 'voice'
    && message.status !== 'backchannel' && message.reply_id === state.floating_reply_id);
  const failure = state.voice_error || state.model_error || state.storage_error || state.exit_error || connectionError || error;
  const showCard = state.preview_expanded && Boolean(question || failure);
  const hint = state.voice_status === 'starting' ? '正在准备' : state.voice_activity === 'recognizing' ? '正在识别' : '';
  const busy = state.voice_status === 'starting' || state.history_status === 'reading' || Boolean(state.active_reply_id)
    || ['recognizing', 'classifying'].includes(state.voice_activity);
  const mode = failure ? 'error' : state.voice_playing ? 'speaking' : busy ? 'busy' : 'listening';
  const microphone = Math.sqrt(state.microphone_level);
  const amplitude = mode === 'speaking' ? Math.sqrt(state.playback_level) : mode === 'listening' ? microphone : 0;
  const historyLabel = state.history_reply_id === reply?.id ? {
    idle: '', reading: '正在读取历史…', found: '', not_found: '未找到相关历史', needs_clarification: '需要更多历史线索',
    invalid_arguments: '历史查询条件无效', failed: state.history_error || '历史读取失败', cancelled: '历史读取已取消',
  }[state.history_status] : '';
  const canOperate = !state.exiting && !state.restarting && !state.storage_error && !state.exit_error;
  const canEnd = ['starting', 'active'].includes(state.voice_status) && canOperate;
  const voiceLabel = { off: '语音未启动', standby: '待唤醒', starting: '正在准备', active: '语音交互中',
    stopping: '正在结束', error: '语音不可用' }[state.voice_status];

  useLayoutEffect(() => {
    if (!state.floating_visible) setHovered(false);
  }, [state.floating_visible]);

  useLayoutEffect(() => {
    let active = true;
    let previousLayout = '';
    function report() {
      const height = showCard ? Math.ceil(cardRef.current!.getBoundingClientRect().height) : 0;
      const layout = `${height}:${hovered}:${Boolean(hint)}`;
      if (layout === previousLayout) return;
      previousLayout = layout;
      // 原生裁剪区域与实际卡片、悬停按钮和临时提示同步，保留桌面空隙。
      layoutQueue.current = layoutQueue.current.then(() => window.pywebview!.api.floating_layout(height, hovered, Boolean(hint))).catch(reason => {
        if (active) setError(`收音窗布局失败：${String(reason)}`);
      });
    }
    report();
    const observer = new ResizeObserver(report);
    if (showCard) observer.observe(cardRef.current!);
    return () => { active = false; observer.disconnect(); };
  }, [showCard, hovered, hint]);

  useLayoutEffect(() => {
    if (previousReply.current !== state.floating_reply_id) {
      previousReply.current = state.floating_reply_id;
      followLatest.current = true;
      setFollowing(true);
    }
    if (showCard && followLatest.current) scrollRef.current!.scrollTop = scrollRef.current!.scrollHeight;
  }, [state.floating_reply_id, question?.text, reply?.text, reply?.status, failure, historyLabel, showCard]);

  async function command(name: string) {
    try {
      await window.pywebview!.api.command(name);
      setError('');
    } catch (reason) {
      setError(`操作失败：${String(reason)}`);
    }
  }

  return <div className="floating-widget" data-mode={mode}>
    {showCard && <section className="floating-conversation" ref={cardRef} aria-label="本轮语音问答">
      <div className="floating-transcript" ref={scrollRef} tabIndex={0} aria-label="本轮问答内容"
        onScroll={event => {
          const element = event.currentTarget;
          followLatest.current = element.scrollHeight - element.scrollTop - element.clientHeight < 20;
          setFollowing(followLatest.current);
        }}>
        {question?.text && <p className="floating-question">{question.text}</p>}
        {reply?.text && <p className="floating-answer">{reply.text}</p>}
        {!reply?.text && reply?.status === 'thinking' && <p className="floating-progress">正在思考…</p>}
        {historyLabel && <p className="floating-progress" role="status">{historyLabel}</p>}
        {failure && <p className="floating-failure" role="alert">{failure}</p>}
        {reply?.error && reply.error !== failure && <p className="floating-failure" role="alert">{reply.error}</p>}
        {state.voice_recovery === 'running' && <p className="floating-progress" role="status">正在恢复语音…</p>}
      </div>
      {(!following || failure) && <div className="floating-card-actions">
        {!following && <button onClick={() => {
          followLatest.current = true;
          setFollowing(true);
          scrollRef.current!.scrollTop = scrollRef.current!.scrollHeight;
        }}><ArrowDown size={12} />回到最新</button>}
        {state.voice_status === 'error' && <button onClick={() => void command('recover_voice')}
          disabled={!state.model_available || !canOperate}><RotateCcw size={12} />恢复语音</button>}
        {failure && <button onClick={() => void command('show_main')}>打开主窗口</button>}
      </div>}
    </section>}
    <div className="orb-interaction" onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}
      onFocusCapture={() => setHovered(true)} onBlurCapture={event => {
        if (!event.currentTarget.contains(event.relatedTarget)) setHovered(false);
      }}>
      <button className="orb-control orb-end" data-visible={state.floating_controls_visible} disabled={!canEnd}
        onClick={() => void command('end_voice')} title="结束语音" aria-label="结束语音"><CircleStop size={14} /></button>
      <button className="orb-control orb-hide" data-visible={state.floating_controls_visible}
        onClick={() => void command('hide_floating')} title="隐藏收音窗" aria-label="隐藏收音窗"><X size={14} /></button>
      <button className="voice-orb" onClick={() => { if (!failure && !reply?.error) void command('toggle_preview_card'); }}
        aria-disabled={Boolean(failure || reply?.error)} title={failure || voiceLabel}
        aria-label={`${voiceLabel}，${failure || reply?.error ? '错误详情已展开' : state.preview_expanded ? '收起问答' : '展开问答'}`}>
        <svg className="microphone-ring" viewBox="0 0 64 64" aria-hidden="true">
          <circle className="ring-track" cx="32" cy="32" r="30" />
          <circle className="ring-level" cx="32" cy="32" r="30" pathLength="1" strokeDasharray={`${microphone} 1`} />
        </svg>
        <span className="orb-wave" aria-hidden="true">{[0.45, 0.75, 1, 0.75, 0.45].map((factor, index) =>
          <i key={index} style={{ height: mode === 'busy' ? 6 + 10 * factor : 3 + 23 * amplitude * factor,
            animationDelay: `${index * -0.22}s` }} />)}</span>
      </button>
    </div>
    {hint && <span className="orb-hint" role="status">{hint}</span>}
  </div>;
}
