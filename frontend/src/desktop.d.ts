interface DesktopSnapshot {
  sequence: number;
  messages: ChatMessage[];
  state: {
    status: 'ready';
    main_window: 'visible' | 'hidden' | 'minimized';
    floating_visible: boolean;
    preview_expanded: boolean;
    desktop_check: boolean;
    activity_running: boolean;
    activity_tick: number;
    model_available: boolean;
    model_error: string;
    model: string;
    active_reply_id: string | null;
    storage_status: 'saving' | 'saved' | 'error';
    storage_error: string;
    exiting: boolean;
  };
}

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  source: 'text';
  reply_id: string;
  text: string;
  status: 'queued' | 'thinking' | 'streaming' | 'completed' | 'interrupted' | 'failed' | 'unfinished';
  created_at: string;
  error: string;
}

interface DesktopConnection extends DesktopSnapshot {
  view: 'main' | 'floating';
}

interface Window {
  pywebview?: {
    api: {
      connect(): Promise<DesktopConnection>;
      command(name: string): Promise<void>;
      send_text(text: string): Promise<{ user_message_id: string; reply_id: string }>;
    };
  };
}
