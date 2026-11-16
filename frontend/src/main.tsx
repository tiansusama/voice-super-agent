import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { MainWindow } from './MainWindow';
import { FloatingWindow } from './FloatingWindow';

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

  if (!snapshot || !view) return <main className="connection-page"><p role="status">{error || '正在连接桌面…'}</p></main>;
  if (view === 'floating') {
    return <FloatingWindow snapshot={snapshot} connectionError={error} />;
  }

  return <MainWindow snapshot={snapshot} connectionError={error} />;

}

createRoot(document.getElementById('root')!).render(<App />);
