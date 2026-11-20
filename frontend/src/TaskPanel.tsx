import { useEffect, useId, useState } from 'react';
import { ArrowDown, ArrowLeft } from 'lucide-react';
import './tasks.css';

const taskStatus = {
  queued: '等待执行', running: '执行中', waiting_user: '等待回答', pausing: '正在暂停',
  paused: '已暂停', cancelling: '正在取消', cancelled: '已取消', recovering: '恢复核对中',
  storage_error: '保存失败', failed: '执行失败', completed: '已完成',
};

const taskActions = {
  pause: '暂停', resume: '继续', cancel: '取消', retry: '重试',
};

const stepResolutionLabels = {
  succeeded: '已完成', not_executed: '未执行', cancelled: '已停止',
};
const stepStatus = {
  ...stepResolutionLabels, started: '执行中', uncertain: '等待核对', failed: '失败',
};

type TaskControl = keyof typeof taskActions;
type StepResolution = keyof typeof stepResolutionLabels;

export type TaskSummary = {
  id: string;
  title: string;
  goal: string;
  state: keyof typeof taskStatus;
  progress: string;
  result: string;
  error: string;
  unread: boolean;
  actions: (TaskControl | 'supplement' | 'answer')[];
  source: { message_id: string; reply_id: string; source: 'text' | 'voice' };
};

type TaskQuestion = {
  id: string;
  task_id: string;
  text: string;
  status: 'open' | 'answered' | 'closed';
  answer: string | null;
  step_id?: string | null;
  requested_operation?: 'file.create';
};

export type TaskNotice = {
  id: string;
  task_id: string;
  read: boolean;
  question: TaskQuestion | null;
};

export type TaskDetail = {
  task: TaskSummary;
  questions: TaskQuestion[];
  artifacts: { kind: string; path: string }[];
  steps?: {
    id: string;
    status: keyof typeof stepStatus;
    request: { tool: string; target: string; arguments: Record<string, unknown> };
    outcome: { result: Record<string, unknown>; error: string } | null;
  }[];
};

export type RunTaskAction = (
  taskId: string,
  action: TaskControl | 'answer',
  payload?: { question_id: string; text: string; resolution?: StepResolution },
) => Promise<unknown>;

export function TaskCard({ task, open, command }: {
  task: TaskSummary;
  open: (id: string) => void;
  command: RunTaskAction;
}) {
  const [pending, setPending] = useState<TaskControl | null>(null);
  const [error, setError] = useState('');
  const actions = task.actions.filter((action): action is TaskControl => action in taskActions);

  async function run(action: TaskControl) {
    setPending(action);
    setError('');
    try {
      await command(task.id, action);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setPending(null);
    }
  }

  return <article className="task-card" aria-label={task.title}>
    <header>
      <button className="task-title" onClick={() => open(task.id)}>{task.title}</button>
      <span className="task-state">{taskStatus[task.state]}</span>
      {task.unread && <span className="task-unread">未读</span>}
    </header>
    {(task.result || task.progress) && <p>{task.result || task.progress}</p>}
    {task.error && <p className="message-error">{task.error}</p>}
    {actions.length > 0 && <div className="task-actions">
      {actions.map(action => <button key={action} className="quiet-button" disabled={pending !== null}
        onClick={() => void run(action)}>{pending === action ? `${taskActions[action]}中…` : taskActions[action]}</button>)}
    </div>}
    {error && <p className="message-error" role="alert">{error}</p>}
  </article>;
}

export function QuestionReply({ question, command }: { question: TaskQuestion; command: RunTaskAction }) {
  const fieldId = useId();
  const [text, setText] = useState('');
  const [resolution, setResolution] = useState<StepResolution | ''>('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  async function answer() {
    setPending(true);
    setError('');
    try {
      await command(question.task_id, 'answer', resolution
        ? { question_id: question.id, text: stepResolutionLabels[resolution], resolution }
        : { question_id: question.id, text: text.trim() });
      setText('');
      setResolution('');
    } catch (reason) {
      setError(String(reason));
    } finally {
      setPending(false);
    }
  }

  return <form className="task-answer" onSubmit={event => { event.preventDefault(); void answer(); }}>
    <label htmlFor={fieldId}>{question.text}</label>
    {question.step_id ? <select id={fieldId} value={resolution}
      onChange={event => setResolution(event.target.value as StepResolution | '')} disabled={pending} required>
      <option value="">选择实际结果</option>
      {Object.entries(stepResolutionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select> : question.requested_operation === 'file.create' ?
      <input id={fieldId} type="text" placeholder="新的绝对文件路径" value={text}
        onChange={event => setText(event.target.value)} disabled={pending} required />
      : <textarea id={fieldId} value={text} onChange={event => setText(event.target.value)}
      disabled={pending} rows={2} required />}
    <button type="submit" className="quiet-button" disabled={pending || !(question.step_id ? resolution : text.trim())}>
      {pending ? '正在提交…' : '提交答复'}
    </button>
    {error && <p className="message-error" role="alert">{error}</p>}
  </form>;
}

export function TaskPanel({ selected, revision, open, close, command }: {
  selected: string | null;
  revision: number;
  open: (id: string) => void;
  close: () => void;
  command: RunTaskAction;
}) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [pages, setPages] = useState(1);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const currentDetail = detail?.task.id === selected ? detail : null;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    async function read() {
      const api = window.pywebview!.api;
      if (selected) {
        const result = await api.get_task(selected);
        if (active) setDetail(result);
        return;
      }
      let cursor: number | null = null;
      const rows: TaskSummary[] = [];
      for (let index = 0; index < pages; index++) {
        const result = await api.list_tasks(cursor ?? undefined);
        rows.push(...result.tasks);
        cursor = result.next_cursor;
        if (cursor === null || !active) break;
      }
      if (active) {
        setTasks(rows);
        setNext(cursor);
      }
    }
    void read().catch(reason => { if (active) setError(String(reason)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [selected, revision, pages]);

  return <section className="task-panel" aria-label="任务">
    {selected ? <>
      <button className="quiet-button" onClick={close}><ArrowLeft size={16} />返回任务列表</button>
      {currentDetail && <div className="task-detail">
        <TaskCard key={currentDetail.task.id} task={currentDetail.task} open={open} command={command} />
        <p>{currentDetail.task.goal}</p>
        <small>来自{currentDetail.task.source.source === 'voice' ? '语音' : '文字'}对话</small>
        {currentDetail.questions.map(question => question.status === 'open' && currentDetail.task.actions.includes('answer')
          ? <QuestionReply key={question.id} question={question} command={command} />
          : <div key={question.id} className="task-question">
            <p>{question.text}</p>
            {question.answer && <p>{question.answer}</p>}
          </div>)}
        {currentDetail.artifacts.length > 0 && <section aria-label="产物">
          <h3>产物</h3>
          <ul>{currentDetail.artifacts.map(artifact => <li key={artifact.path}>{artifact.path}</li>)}</ul>
        </section>}
        {currentDetail.steps && currentDetail.steps.length > 0 &&
          <details className="task-steps"><summary>执行记录（{currentDetail.steps.length}）</summary>
            {currentDetail.steps.map(step => <details key={step.id}>
              <summary>{step.request.tool} · {stepStatus[step.status]}</summary>
              <p>{step.request.target}</p>
              <pre>{JSON.stringify(step.request.arguments, null, 2)}</pre>
              {step.outcome?.error && <p className="message-error">{step.outcome.error}</p>}
              {step.outcome && <pre>{JSON.stringify(step.outcome.result, null, 2)}</pre>}
            </details>)}
          </details>}
      </div>}
    </> : <>
      {tasks.map(task => <TaskCard key={task.id} task={task} open={open} command={command} />)}
      {!loading && !error && tasks.length === 0 && <p>还没有任务。</p>}
      {next !== null && <button className="quiet-button" disabled={loading} onClick={() => setPages(count => count + 1)}>
        <ArrowDown size={14} />加载更多任务
      </button>}
    </>}
    {loading && <p role="status">正在加载任务…</p>}
    {error && <p className="message-error" role="alert">{error}</p>}
  </section>;
}

export function TaskConcurrencySetting({ limit, save, disabled }: {
  limit: number;
  save: (limit: number) => Promise<number>;
  disabled: boolean;
}) {
  const [value, setValue] = useState(String(limit));
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; failed: boolean } | null>(null);
  useEffect(() => setValue(String(limit)), [limit]);

  async function submit() {
    const selected = Number(value);
    if (!Number.isSafeInteger(selected) || selected < 1) {
      setFeedback({ text: '请输入大于 0 的整数。', failed: true });
      return;
    }
    setSaving(true);
    setFeedback(null);
    try {
      const saved = await save(selected);
      setValue(String(saved));
      setFeedback({ text: '任务并发上限已保存。', failed: false });
    } catch (reason) {
      setFeedback({ text: String(reason), failed: true });
    } finally {
      setSaving(false);
    }
  }

  return <div className="setting-card">
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <label htmlFor="task-concurrency">任务并发上限</label>
      <p>调低上限后，在执行的任务继续运行。有空闲名额时再执行排队任务。</p>
      <div className="setting-input">
        <input id="task-concurrency" type="number" min="1" step="1" value={value} required disabled={disabled || saving}
          onChange={event => setValue(event.target.value)} />
        <span>项</span>
        <button type="submit" disabled={disabled || saving}>{saving ? '正在保存…' : '保存并发上限'}</button>
      </div>
      <small>当前上限为 {limit} 项。</small>
      {feedback && <p className={`setting-feedback${feedback.failed ? ' failed' : ''}`} role={feedback.failed ? 'alert' : 'status'}>
        {feedback.text}
      </p>}
    </form>
  </div>;
}
