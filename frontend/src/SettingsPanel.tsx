import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Check, Keyboard } from 'lucide-react';

export function SettingsPanel({ state, command, children }: {
  state: DesktopSnapshot['state']; command: (name: string) => Promise<void>;
  children?: ReactNode;
}) {
  const savedWord = state.pending_wake_word ?? state.wake_word;
  const [word, setWord] = useState(savedWord);
  const [hotkey, setHotkey] = useState(state.wake_hotkey);
  const [seconds, setSeconds] = useState(String(state.voice_idle_seconds));
  const [recording, setRecording] = useState(false);
  const [saving, setSaving] = useState<SettingName | null>(null);
  const [feedback, setFeedback] = useState<{ name: SettingName; text: string; error: boolean } | null>(null);
  const keyInput = useRef<HTMLInputElement>(null);
  useEffect(() => setWord(savedWord), [savedWord]);
  useEffect(() => setHotkey(state.wake_hotkey), [state.wake_hotkey]);
  useEffect(() => setSeconds(String(state.voice_idle_seconds)), [state.voice_idle_seconds]);

  async function save(name: SettingName, value: string | number) {
    setSaving(name);
    setFeedback(null);
    try {
      const result = await window.pywebview!.api.save_setting(name, value);
      setFeedback({ name, text: result.pending ? '已保存，回到待唤醒时应用。' : '已保存并生效。', error: false });
    } catch (reason) {
      setFeedback({ name, text: String(reason), error: true });
    } finally {
      setSaving(null);
    }
  }
  const disabled = state.exiting || state.restarting || state.settings_saving || saving !== null;
  const status = (name: SettingName) => feedback?.name === name &&
    <p className={`setting-feedback ${feedback.error ? 'failed' : ''}`} role={feedback.error ? 'alert' : 'status'}>
      {!feedback.error && <Check size={14} />}{feedback.text}</p>;

  return <section className="info-page settings-page" aria-label="设置">
    <div className="page-intro"><h2>让交流更顺手</h2><p>按自己的习惯唤醒小博，继续每一次交流。</p></div>
    {state.settings_error && !feedback?.error && <p className="setting-feedback failed" role="alert">{state.settings_error}</p>}
    <div className="setting-card">
      <form onSubmit={event => { event.preventDefault(); void save('word', word); }}>
        <label htmlFor="wake-word">中文唤醒词</label><p>待唤醒时保存直接应用；语音交互中保存，在回到待唤醒时应用。</p>
        <div className="setting-input"><input id="wake-word" value={word} required onChange={event => setWord(event.target.value)} disabled={disabled} />
          <button type="submit" disabled={disabled}>{saving === 'word' ? '正在保存…' : '保存唤醒词'}</button></div>
        <small>当前生效：{state.wake_word}。听到“叮”声后，再说指令。</small>
        {state.pending_wake_word && <p className="pending-setting" role="status">已保存：{state.pending_wake_word} · 等待进入待唤醒</p>}
        {status('word')}
      </form>
      <form onSubmit={event => { event.preventDefault(); void save('hotkey', hotkey); }}>
        <label htmlFor="wake-hotkey">唤醒组合键</label><p>按住 1 秒后唤醒，松开后继续交流。保存后生效。</p>
        <div className="setting-input"><input ref={keyInput} id="wake-hotkey" value={hotkey} required disabled={disabled}
          placeholder={recording ? '请按下组合键，Esc 取消' : '例如 Ctrl+\\ 或 Ctrl+Shift+K'}
          onChange={event => setHotkey(event.target.value)} onBlur={() => setRecording(false)}
          onKeyDown={event => {
            if (!recording) return;
            event.preventDefault();
            if (event.key === 'Escape') { setRecording(false); return; }
            const key = /^Key[A-Z]$/.test(event.code) ? event.code.slice(3) : /^Digit\d$/.test(event.code) ? event.code.slice(5) :
              /^F([1-9]|1\d|2[0-4])$/.test(event.code) ? event.code :
              ({ Backslash: '\\', Space: 'Space', Enter: 'Enter', Tab: 'Tab' } as Record<string, string>)[event.code];
            if (!key) return;
            setHotkey([event.ctrlKey && 'Ctrl', event.altKey && 'Alt', event.shiftKey && 'Shift', event.metaKey && 'Win', key].filter(Boolean).join('+'));
            setRecording(false);
          }} />
          <button type="button" className="record-hotkey" disabled={disabled} aria-pressed={recording}
            onClick={() => { setRecording(!recording); keyInput.current!.focus(); }}><Keyboard size={16} />{recording ? '录入中' : '录入'}</button>
          <button type="submit" disabled={disabled}>{saving === 'hotkey' ? '正在保存…' : '保存组合键'}</button></div>
        <small>当前生效：{state.wake_hotkey}。全局按键检测与其他应用共享键位，无法识别全部快捷键占用。</small>
        {status('hotkey')}
      </form>
      <form onSubmit={event => { event.preventDefault(); void save('idle_seconds', Number(seconds)); }}>
        <label htmlFor="idle-seconds">空闲超时</label><p>只在等待你继续说话时计时，识别、思考、读取历史和播报期间暂停。</p>
        <div className="setting-input"><input id="idle-seconds" type="number" min="1" step="1" value={seconds} required disabled={disabled}
          onChange={event => setSeconds(event.target.value)} /><span>秒</span>
          <button type="submit" disabled={disabled}>{saving === 'idle_seconds' ? '正在保存…' : '保存超时'}</button></div>
        <small>当前生效：{state.voice_idle_seconds} 秒。保存后重新开始当前等待计时；处理中则在结束后使用新时长。</small>
        {status('idle_seconds')}
      </form>
    </div>
    {children}
    <div className="info-card service-settings"><h3>模型与语音服务</h3><p>模型凭据与服务参数通过本地 .env 配置，修改后重启生效。已保存的交互设置优先于 .env 中的对应项。</p>
      <dl><div><dt>对话模型</dt><dd>{state.model}</dd></div><div><dt>录音设备</dt><dd>{state.microphone_device || 'Windows 默认录音设备'}</dd></div>
        <div><dt>播放设备</dt><dd>{state.playback_device || 'Windows 默认播放设备'}</dd></div></dl>
      {state.audio_device_pending && <p role="status">默认设备已变化，下次唤醒时切换。</p>}</div>
    <div className="window-actions"><button className="quiet-button" onClick={() => void command('hide_main')}>隐藏到托盘</button>
      <button className="quiet-button" onClick={() => void command('quit')} disabled={state.exiting}>退出应用</button></div>
  </section>;
}
