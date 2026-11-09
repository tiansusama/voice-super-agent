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
    voice_status: 'off' | 'standby' | 'starting' | 'active' | 'stopping' | 'error';
    voice_activity: 'listening' | 'recognizing' | 'classifying' | 'thinking' | 'speaking';
    voice_error: string;
    wake_word: string;
    wake_hotkey: string;
    voice_idle_seconds: number;
    microphone_level: number;
    playback_level: number;
    microphone_device: string;
    playback_device: string;
    audio_device_pending: boolean;
    playback_stop_ms: number;
  };
}

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  source: 'text' | 'voice';
  reply_id: string;
  text: string;
  status: 'queued' | 'thinking' | 'streaming' | 'completed' | 'interrupted' | 'failed' | 'unfinished' | 'backchannel';
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
