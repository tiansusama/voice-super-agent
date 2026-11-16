// Playwright CLI 页面验收：桥接使用内存样例，覆盖设置、分页、迟到原文和错误恢复。
async (page) => {
  await page.reload();
  await page.evaluate(() => {
    const snapshot = {sequence: 1, messages: [{id:'live-user', reply_id:'live-reply', role:'user', source:'text', status:'completed',
      created_at:'2026-11-16T01:00:00Z', text:'继续当前的交流', error:''}, {id:'live-reply', reply_id:'live-reply', role:'assistant', source:'text', status:'completed',
      created_at:'2026-11-16T01:00:00Z', text:'当前对话的内容会继续保留。', error:''}], state: {
      status:'ready', main_window:'visible', floating_visible:false, preview_expanded:true, floating_hidden:false,
      floating_available:false, floating_reply_id:null, floating_card_height:0, floating_controls_visible:false,
      floating_hint_visible:false, voice_playing:false, desktop_check:false, activity_running:false, activity_tick:0,
      model_available:true, model_error:'', model:'deepseek-flash', active_reply_id:null, storage_status:'saved',
      storage_error:'', exiting:false, voice_status:'standby', voice_activity:'listening', voice_error:'', wake_word:'你好小博',
      wake_hotkey:'Ctrl+\\', voice_idle_seconds:60, pending_wake_word:null, settings_error:'', settings_saving:false,
      microphone_level:0, playback_level:0, microphone_device:'', playback_device:'', audio_device_pending:false,
      playback_stop_ms:0, last_error:null, voice_recovery:'idle', history_status:'idle', history_reply_id:null,
      history_error:'', restarting:false, exit_error:''
    }};
    const calls = [];
    const rows = Array.from({length:45}, (_, i) => ({position:i, id:'h'+i, reply_id:'r'+Math.floor(i/2),
      role:i % 2 ? 'assistant' : 'user', source:i % 3 ? 'text' : 'voice', status:i===40?'failed':i===42?'backchannel':'completed',
      created_at:i<20?'2026-11-15T01:00:00Z':'2026-11-16T02:00:00Z', error:i===40?'测试服务失败':'',
      text:i===44?'旧日讨论的长原文🙂。\n'.repeat(1500):i===43?'慢请求的旧原文':i===42?'嗯嗯，你继续':`历史消息 ${i}：我们讨论的那个方案。`}));
    function update() { snapshot.sequence++; window.dispatchEvent(new CustomEvent('agent-state',{detail:structuredClone(snapshot)})); }
    const test = window.__settingsTest = {snapshot, rows, calls, update, failSave:false, failList:false, slowRead:false, release:null};
    window.pywebview = {api:{
      connect:async()=>({view:'main',...structuredClone(snapshot)}),
      command:async name=>{ calls.push({name}); },
      send_text:async text=>{calls.push({name:'send_text',text}); return {user_message_id:'new',reply_id:'new-reply'};},
      resend_text:async()=>{throw new Error('本检查不重发');}, floating_layout:async()=>{},
      save_setting:async (name,value)=>{
        calls.push({name:'save_setting',field:name,value});
        if(test.failSave || (name==='word' && /[a-z]/i.test(value))) {
          snapshot.state.settings_error='设置未生效：'+(test.failSave?'测试数据库写入失败':'唤醒词需要是一个中文短语'); update();
          throw new Error(snapshot.state.settings_error);
        }
        snapshot.state.settings_error='';
        if(name==='word') {
          if(snapshot.state.voice_status==='active') snapshot.state.pending_wake_word=value;
          else {snapshot.state.wake_word=value; snapshot.state.pending_wake_word=null;}
        }
        if(name==='hotkey') snapshot.state.wake_hotkey=value;
        if(name==='idle_seconds') snapshot.state.voice_idle_seconds=value;
        update(); return {pending:!!snapshot.state.pending_wake_word && name==='word'};
      },
      list_history:async before=>{
        calls.push({name:'list_history',before});
        if(test.failList) throw new Error('测试历史读取失败');
        const matches=rows.filter(row=>before===undefined || row.position<before).reverse();
        return {messages:matches.slice(0,20).map(({text,...row})=>({...row,preview:Array.from(text).slice(0,160).join(''),total_characters:Array.from(text).length})),
          next_before_position:matches.length>20?matches[19].position:null};
      },
      read_history:async (id,offset=0)=>{
        calls.push({name:'read_history',id,offset});
        if(id==='h43' && test.slowRead) await new Promise(resolve=>test.release=resolve);
        const row=rows.find(row=>row.id===id);
        const text=Array.from(row.text); const end=Math.min(offset+8000,text.length);
        return {status:'found',message:{...row,text:text.slice(offset,end).join(''),total_characters:text.length},next_offset:end<text.length?end:null};
      }
    }};
    window.dispatchEvent(new Event('pywebviewready'));
  });
  const checks=[];
  function check(condition,label) {if(!condition) throw new Error(label);checks.push(label);}
  await page.setViewportSize({width:960,height:680});
  await page.getByRole('textbox',{name:'输入消息'}).fill('历史浏览期间保留草稿');
  await page.getByRole('button',{name:'设置',exact:true}).click();
  await page.getByLabel('中文唤醒词',{exact:true}).fill('你好朋友');
  await page.getByRole('button',{name:'保存唤醒词',exact:true}).click();
  await page.getByText('当前生效：你好朋友。听到“叮”声后，再说指令。',{exact:true}).waitFor();
  check(true,'待唤醒时保存新词立即反馈生效');
  await page.evaluate(()=>{const t=window.__settingsTest;t.snapshot.state.voice_status='active';t.update();});
  await page.getByLabel('中文唤醒词',{exact:true}).fill('你好小友');
  await page.getByRole('button',{name:'保存唤醒词',exact:true}).click();
  await page.getByText('已保存：你好小友 · 等待进入待唤醒',{exact:true}).waitFor();
  check(await page.getByText('当前生效：你好朋友。听到“叮”声后，再说指令。',{exact:true}).isVisible(),'活动语音分别显示有效词和待应用词');
  await page.getByRole('button',{name:'录入',exact:true}).click();
  await page.getByLabel('唤醒组合键',{exact:true}).press('Control+Shift+K');
  check(await page.getByLabel('唤醒组合键',{exact:true}).inputValue()==='Ctrl+Shift+K','录入组合键使用物理键位及修饰键');
  await page.getByRole('button',{name:'保存组合键',exact:true}).click();
  await page.getByText(/当前生效：Ctrl\+Shift\+K/).waitFor();
  check(true,'组合键保存后更新有效值');
  await page.getByLabel('空闲超时',{exact:true}).fill('75');
  await page.getByRole('button',{name:'保存超时',exact:true}).click();
  await page.getByText(/当前生效：75 秒/).waitFor();
  check(true,'正整数超时保存后更新有效值');
  await page.screenshot({path:'output/playwright/settings.png'});
  const count=await page.evaluate(()=>window.__settingsTest.calls.filter(c=>c.name==='save_setting').length);
  await page.getByLabel('空闲超时',{exact:true}).fill('1.5');
  await page.getByRole('button',{name:'保存超时',exact:true}).click();
  check(await page.evaluate(()=>window.__settingsTest.calls.filter(c=>c.name==='save_setting').length)===count,'小数秒由输入校验阻止提交');
  await page.evaluate(()=>window.__settingsTest.failSave=true);
  await page.getByLabel('空闲超时',{exact:true}).fill('90');
  await page.getByRole('button',{name:'保存超时',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'测试数据库写入失败'}).waitFor();
  check(await page.getByText(/当前生效：75 秒/).isVisible(),'保存失败保留原有效配置并展示原因');
  await page.screenshot({path:'output/playwright/settings-error.png'});
  await page.evaluate(()=>window.__settingsTest.failSave=false);
  await page.getByLabel('空闲超时',{exact:true}).fill('45');
  await page.getByRole('button',{name:'保存超时',exact:true}).click();
  await page.getByText(/当前生效：45 秒/).waitFor();
  await page.getByRole('button',{name:'历史记录',exact:true}).click();
  await page.locator('.history-entry').first().waitFor();
  check(await page.locator('.history-entry').count()===20,'首次按需加载20条历史');
  await page.screenshot({path:'output/playwright/history.png'});
  await page.locator('.history-entry').first().click();
  await page.getByRole('button',{name:'继续加载原文',exact:true}).waitFor();
  check((await page.locator('.history-original').innerText()).includes('旧日讨论的长原文'),'点击历史展开原文');
  while(await page.getByRole('button',{name:'继续加载原文',exact:true}).count()) {
    await page.getByRole('button',{name:'继续加载原文',exact:true}).click();
  }
  check(await page.evaluate(()=>document.querySelector('.history-original').textContent===window.__settingsTest.rows[44].text),'分段加载可取得完整Unicode长原文');
  await page.screenshot({path:'output/playwright/original.png'});
  await page.getByRole('button',{name:'返回列表',exact:true}).click();
  await page.getByRole('button',{name:'加载更早记录',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.history-entry').length===40);
  await page.getByRole('button',{name:'加载更早记录',exact:true}).click();
  await page.getByText('已显示全部记录',{exact:true}).waitFor();
  check(await page.locator('.history-entry').count()===45,'游标分页追加历史并显示加载完成');
  await page.evaluate(()=>window.__settingsTest.slowRead=true);
  await page.getByRole('button').filter({hasText:'慢请求的旧原文'}).click();
  await page.waitForFunction(()=>window.__settingsTest.release!==null);
  await page.getByRole('button').filter({hasText:'嗯嗯，你继续'}).click();
  await page.locator('.history-original').filter({hasText:'嗯嗯，你继续'}).waitFor();
  await page.evaluate(()=>window.__settingsTest.release());
  check(await page.locator('.history-original').innerText()==='嗯嗯，你继续','切换记录后迟到读取结果不会覆盖新原文');
  await page.getByRole('button',{name:'对话',exact:true}).click();
  check(await page.getByRole('textbox',{name:'输入消息'}).inputValue()==='历史浏览期间保留草稿','返回对话保留未发送草稿');
  check(await page.locator('.message').count()===2,'浏览历史保留当前聊天消息');
  check(await page.evaluate(()=>window.__settingsTest.calls.every(c=>c.name!=='restart_conversation'&&c.name!=='send_text')),'浏览历史不触发重开或模型输入');
  await page.evaluate(()=>{const t=window.__settingsTest;t.snapshot.state.model_available=false;t.snapshot.state.model_error='缺少模型配置';t.snapshot.state.voice_status='error';t.snapshot.state.voice_error='缺少语音配置';t.update();});
  await page.getByRole('button',{name:'设置',exact:true}).click();
  check(await page.getByRole('button',{name:'保存超时',exact:true}).isEnabled(),'语音和模型不可用时仍可编辑设置');
  await page.getByRole('button',{name:'历史记录',exact:true}).click();
  await page.locator('.history-entry').first().waitFor();
  check(await page.locator('.history-entry').count()===20,'能力不可用时仍可读取历史');
  await page.setViewportSize({width:627,height:445});
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'窄窗口历史页无横向溢出');
  await page.locator('.history-entry').first().click();
  await page.locator('.history-original').waitFor();
  check(!await page.locator('.history-list').isVisible(),'窄窗口切换至原文并保留返回入口');
  await page.screenshot({path:'output/playwright/history-compact.png'});
  await page.getByRole('button',{name:'设置',exact:true}).click();
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'窄窗口设置页无横向溢出');
  await page.screenshot({path:'output/playwright/settings-compact.png'});
  await page.evaluate(()=>window.__settingsTest.failList=true);
  await page.getByRole('button',{name:'历史记录',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'测试历史读取失败'}).waitFor();
  check(await page.getByRole('button',{name:'刷新',exact:true}).isEnabled(),'历史读取失败有原因和刷新入口');
  await page.evaluate(()=>window.__settingsTest.failList=false);
  await page.getByRole('button',{name:'刷新',exact:true}).click();
  await page.locator('.history-entry').first().waitFor();
  check(true,'刷新后恢复历史浏览');
  return {passed:checks.length,checks};
}
