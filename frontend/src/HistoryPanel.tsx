import { useEffect, useState } from 'react';
import { ArrowLeft, ArrowDown, Clock3, RotateCcw } from 'lucide-react';

const statusLabel: Record<ChatMessage['status'], string> = {
  queued: '等待处理', thinking: '思考中', streaming: '生成中', completed: '已完成',
  interrupted: '已结束', failed: '失败', unfinished: '未完成', backchannel: '附和',
};

function HistoryDetail({ id, close }: { id: string; close: () => void }) {
  const [message, setMessage] = useState<HistoryMessage>();
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    window.pywebview!.api.read_history(id).then(result => {
      if (!active) return;
      if (result.status === 'not_found') { setError('这条历史记录未找到。'); return; }
      setMessage(result.message); setNext(result.next_offset);
    }).catch(reason => { if (active) setError(`读取原文失败：${String(reason)}`); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [id]);
  async function readMore() {
    setLoading(true); setError('');
    try {
      const result = await window.pywebview!.api.read_history(id, next!);
      if (result.status === 'not_found') throw new Error('这条历史记录未找到。');
      setMessage(current => ({ ...result.message, text: current!.text + result.message.text }));
      setNext(result.next_offset);
    } catch (reason) { setError(`读取原文失败：${String(reason)}`); }
    finally { setLoading(false); }
  }
  return <section className="history-detail" aria-label="历史原文">
    <button className="quiet-button" onClick={close}><ArrowLeft size={16} />返回列表</button>
    {message && <><div className="history-detail-heading"><h2>{message.role === 'user' ? '我' : '小博'}</h2>
      <span>{new Date(message.created_at).toLocaleString('zh-CN')} · {message.source === 'voice' ? '语音' : '文字'} · {statusLabel[message.status]}</span></div>
      <div className="history-original">{message.text || '（未产生正文）'}</div>
      {message.error && <p className="message-error" role="alert">{message.error}</p>}
      <p className="history-character-count">已显示 {Array.from(message.text).length} / {message.total_characters} 字符</p></>}
    {loading && <p role="status">正在读取原文…</p>}
    {error && <p className="message-error" role="alert">{error}</p>}
    {next !== null && <button className="history-more" disabled={loading} onClick={() => void readMore()}><ArrowDown size={14} />继续加载原文</button>}
  </section>;
}

export function HistoryPanel() {
  const [rows, setRows] = useState<HistoryEntry[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    window.pywebview!.api.list_history().then(result => {
      if (active) { setRows(result.messages); setNext(result.next_before_position); }
    }).catch(reason => { if (active) setError(`加载历史失败：${String(reason)}`); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  async function load(older: boolean) {
    setLoading(true); setError('');
    try {
      const result = await window.pywebview!.api.list_history(older ? next! : undefined);
      setRows(current => older ? [...current, ...result.messages] : result.messages);
      setNext(result.next_before_position);
    } catch (reason) { setError(`加载历史失败：${String(reason)}`); }
    finally { setLoading(false); }
  }
  return <section className="history-view" aria-label="历史记录">
    <div className="history-toolbar"><p>按时间查看原文，浏览记录时可以继续当前交流。</p>
      <button className="quiet-button" disabled={loading} onClick={() => void load(false)}><RotateCcw size={15} />刷新</button></div>
    {error && <p className="message-error" role="alert">{error}</p>}
    <div className={`history-grid ${selected ? 'has-detail' : ''}`}>
      <div className="history-list" aria-label="历史消息列表">
        {rows.map((row, index) => {
          const day = new Date(row.created_at).toLocaleDateString('zh-CN');
          return <div key={row.id}>
            {(index === 0 || new Date(rows[index - 1].created_at).toLocaleDateString('zh-CN') !== day) && <h3 className="history-day">{day}</h3>}
            <button className={`history-entry ${selected === row.id ? 'selected' : ''}`} aria-pressed={selected === row.id}
              onClick={() => setSelected(row.id)}><span className="history-entry-meta"><strong>{row.role === 'user' ? '我' : '小博'}</strong>
                <time>{new Date(row.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                <span>{row.source === 'voice' ? '语音' : '文字'}</span></span>
              <span className="history-preview">{row.preview || '（未产生正文）'}</span>
              <span className={`history-entry-status ${row.status === 'failed' ? 'failed' : ''}`}>{statusLabel[row.status]} · {row.total_characters} 字符 · 查看原文</span>
            </button></div>;
        })}
        {!loading && !error && rows.length === 0 && <div className="history-empty"><Clock3 size={28} /><h2>还没有历史记录</h2><p>开始交流后，内容会保存在这里。</p></div>}
        {loading && <p role="status">正在加载历史…</p>}
        {next !== null && <button className="history-more" disabled={loading} onClick={() => void load(true)}><ArrowDown size={14} />加载更早记录</button>}
        {!loading && rows.length > 0 && next === null && <p className="history-end">已显示全部记录</p>}
      </div>
      {selected ? <HistoryDetail key={selected} id={selected} close={() => setSelected(null)} /> :
        <div className="history-detail history-placeholder"><Clock3 size={28} /><h2>选一条记录，接着回想</h2><p>查看原文会保留当前对话和未发送草稿。</p></div>}
    </div>
  </section>;
}
