interface DesktopSnapshot {
  sequence: number;
  messages: ChatMessage[];
  state: {
    status: 'ready';
    main_window: 'visible' | 'hidden' | 'minimized';
    floating_visible: boolean;
    preview_expanded: boolean;
    floating_hidden: boolean;
    floating_available: boolean;
    floating_reply_id: string | null;
    floating_card_height: number;
    floating_controls_visible: boolean;
    floating_hint_visible: boolean;
    voice_playing: boolean;
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
    pending_wake_word: string | null;
    settings_error: string;
    settings_saving: boolean;
    microphone_level: number;
    playback_level: number;
    microphone_device: string;
    playback_device: string;
    audio_device_pending: boolean;
    playback_stop_ms: number;
    last_error: {
      service: string;
      category: string;
      permanent: boolean;
      message: string;
      reply_id: string | null;
      source: 'text' | 'voice' | null;
    } | null;
    voice_recovery: 'idle' | 'running' | 'succeeded' | 'failed';
    history_status: 'idle' | 'reading' | 'found' | 'not_found' | 'needs_clarification' | 'invalid_arguments' | 'failed' | 'cancelled';
    history_reply_id: string | null;
    history_error: string;
    restarting: boolean;
    exit_error: string;
    task_revision?: number;
    task_concurrency?: number | null;
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

type SettingName = 'word' | 'hotkey' | 'idle_seconds';

interface HistoryEntry extends Omit<ChatMessage, 'text'> {
  position: number;
  preview: string;
  total_characters: number;
}

interface HistoryPage {
  messages: HistoryEntry[];
  next_before_position: number | null;
}

interface HistoryMessage extends ChatMessage {
  position: number;
  total_characters: number;
}

type HistoryOriginal = { status: 'found'; message: HistoryMessage; next_offset: number | null }
  | { status: 'not_found'; message_id: string };

interface Window {
  pywebview?: {
    api: {
      connect(): Promise<DesktopConnection>;
      command(name: string): Promise<void>;
      send_text(text: string): Promise<{ user_message_id: string; reply_id: string }>;
      resend_text(user_message_id: string): Promise<{ user_message_id: string; reply_id: string }>;
      save_setting(name: SettingName, value: string | number): Promise<{ pending: boolean }>;
      list_history(before_position?: number): Promise<HistoryPage>;
      read_history(message_id: string, offset?: number): Promise<HistoryOriginal>;
      floating_layout(card_height: number, controls_visible: boolean, hint_visible: boolean): Promise<void>;
      list_tasks?(cursor?: number): Promise<{ tasks: import('./TaskPanel').TaskSummary[]; next_cursor: number | null }>;
      get_task?(task_id: string): Promise<import('./TaskPanel').TaskDetail>;
      task_command?: import('./TaskPanel').RunTaskAction;
      save_task_concurrency?(limit: number): Promise<number>;
    };
  };
}
