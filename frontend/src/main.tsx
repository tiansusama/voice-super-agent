import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  const [status, setStatus] = useState('正在连接桌面…');
  const [ready, setReady] = useState(false);

  async function checkStartup() {
    setReady(false);
    setStatus('正在检查启动状态…');
    try {
      await window.pywebview!.api.get_startup_status();
      setReady(true);
      setStatus('桌面与后台已就绪');
    } catch (error) {
      setStatus(`启动检查失败：${String(error)}`);
    }
  }

  useEffect(() => {
    if (window.pywebview) {
      void checkStartup();
    } else {
      window.addEventListener('pywebviewready', checkStartup);
    }
    return () => window.removeEventListener('pywebviewready', checkStartup);
  }, []);

  return (
    <main>
      <span className="eyebrow">VOICE SUPER AGENT</span>
      <section className="startup-card">
        <div className="voice-symbol" aria-hidden="true">
          <i /><i /><i /><i /><i />
        </div>
        <h1>你好，我是小博</h1>
        <p>很高兴与你见面。</p>
        <div className="startup-status" role="status" aria-live="polite">
          <span className={ready ? 'status-dot ready' : 'status-dot'} />
          {status}
        </div>
        <button onClick={() => void checkStartup()} disabled={!window.pywebview}>
          检查启动状态
        </button>
      </section>
      <footer>小博 · 桌面启动</footer>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
