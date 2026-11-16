import { expect, test } from '@playwright/test';

for (const restartFails of [false, true]) {
  test(restartFails ? '重新开始失败时保留原内容和阅读位置' : '重新开始后恢复新语音回复的滚动跟随', async ({ page }) => {
    await page.addInitScript(restartFails => {
      const connection: DesktopConnection = {
        view: 'main', sequence: 1,
        state: {
          status: 'ready', main_window: 'visible', floating_visible: false, preview_expanded: true,
          floating_hidden: false, floating_available: false, floating_reply_id: null, floating_card_height: 0,
          floating_controls_visible: false, floating_hint_visible: false, voice_playing: false,
          desktop_check: false, activity_running: false, activity_tick: 0,
          model_available: true, model_error: '', model: 'test-model', active_reply_id: null,
          storage_status: 'saved', storage_error: '', exiting: false, voice_status: 'standby',
          voice_activity: 'listening', voice_error: '', wake_word: '你好小博', wake_hotkey: 'Ctrl+\\',
          voice_idle_seconds: 60, microphone_level: 0, playback_level: 0, microphone_device: '',
          playback_device: '', audio_device_pending: false, playback_stop_ms: 0, last_error: null,
          voice_recovery: 'idle', history_status: 'idle', history_reply_id: null, history_error: '',
          restarting: false, exit_error: '',
        },
        messages: [{ id: 'old-reply', reply_id: 'old-reply', role: 'assistant', source: 'voice',
          text: '旧对话的长回复。\n'.repeat(180), status: 'completed', error: '', created_at: '2026-11-16T00:00:00Z' }],
      };
      window.pywebview = { api: {
        connect: async () => structuredClone(connection),
        command: async name => {
          if (name !== 'restart_conversation') throw new Error(`未预期的命令：${name}`);
          if (restartFails) throw new Error('测试重新开始失败');
          connection.messages = [];
          connection.sequence++;
          window.dispatchEvent(new CustomEvent('agent-state', { detail: structuredClone(connection) }));
        },
        send_text: async () => { throw new Error('本用例通过语音状态推送消息'); },
        resend_text: async () => { throw new Error('本用例不触发重发'); },
        floating_layout: async () => { throw new Error('主窗口不报告悬浮布局'); },
      } };
    }, restartFails);
    await page.goto('/');
    const messages = page.getByLabel('对话记录', { exact: true });
    const latest = page.getByRole('button', { name: '回到最新', exact: true });
    await expect.poll(() => messages.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(2);
    await messages.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')); });
    await expect(latest).toBeVisible();
    await page.getByRole('button', { name: '重新开始', exact: true }).click();
    if (restartFails) {
      await expect(page.getByText(/测试重新开始失败/)).toBeVisible();
      await expect(page.locator('[data-message-id="old-reply"]')).toBeVisible();
      await expect(latest).toBeVisible();
      await expect(messages).toHaveJSProperty('scrollTop', 0);
      return;
    }
    await expect(page.getByText('有什么想聊的？我们接着说。', { exact: true })).toBeVisible();
    await expect(latest).toBeHidden();
    // 后续回复来自后台语音事件，验证跟随恢复与文字发送入口独立。
    await page.evaluate(async () => {
      const snapshot = await window.pywebview!.api.connect();
      snapshot.sequence++;
      Object.assign(snapshot.state, { voice_status: 'active', voice_activity: 'thinking', active_reply_id: 'new-reply' });
      snapshot.messages = [
        { id: 'new-question', reply_id: 'new-reply', role: 'user', source: 'voice', text: '新一轮问题。',
          status: 'completed', error: '', created_at: '2026-11-16T00:00:00Z' },
        { id: 'new-reply', reply_id: 'new-reply', role: 'assistant', source: 'voice', text: '新语音回复。\n'.repeat(180),
          status: 'streaming', error: '', created_at: '2026-11-16T00:00:00Z' },
      ];
      window.dispatchEvent(new CustomEvent('agent-state', { detail: snapshot }));
    });
    await expect(page.locator('[data-message-id="new-reply"]')).toBeVisible();
    await expect.poll(() => messages.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(2);
    await expect(latest).toBeHidden();
  });
}
