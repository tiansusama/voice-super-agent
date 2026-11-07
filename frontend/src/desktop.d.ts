interface Window {
  pywebview?: {
    api: {
      get_startup_status(): Promise<{ status: 'ready' }>;
    };
  };
}
