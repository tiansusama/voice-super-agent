import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:5180', viewport: { width: 960, height: 680 } },
  webServer: { command: 'npm run dev -- --port 5180', url: 'http://127.0.0.1:5180' },
});
