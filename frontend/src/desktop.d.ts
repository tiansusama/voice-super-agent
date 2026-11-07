interface DesktopSnapshot {
  sequence: number;
  state: {
    status: 'ready';
    main_window: 'visible' | 'hidden' | 'minimized';
    floating_visible: boolean;
    preview_expanded: boolean;
    desktop_check: boolean;
    activity_running: boolean;
    activity_tick: number;
  };
}

interface DesktopConnection extends DesktopSnapshot {
  view: 'main' | 'floating';
}

interface Window {
  pywebview?: {
    api: {
      connect(): Promise<DesktopConnection>;
      command(name: string): Promise<void>;
    };
  };
}
