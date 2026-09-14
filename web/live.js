// A local PCM journal survives page interruption independently of the cloud session.
function openJournal() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('tingji-live-journal-v1', 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('sessions', { keyPath: 'id' });
      request.result.createObjectStore('parts', { keyPath: 'id' });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(Error('请关闭其他听记页面后，再保存本机录音。'));
  });
}
async function journal(store, action, value) {
  const db = await openJournal();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(store, action === 'getAll' ? 'readonly' : 'readwrite');
      const request = tx.objectStore(store)[action](value);
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || Error('本机录音缓存写入失败。'));
    });
  } finally { db.close(); }
}
async function finishJournal(session, parts) {
  const db = await openJournal();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(['sessions', 'parts'], 'readwrite');
      tx.objectStore('sessions').put({ ...session, saved: true });
      parts.forEach(part => tx.objectStore('parts').delete(part.id));
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || Error('录音已保存，备份标记未完成。'));
    });
  } finally { db.close(); }
}
function metadata(r) {
  return { id: r.id, accountId: r.accountId, title: r.title, language: r.language, template: r.template, saved: false };
}
async function partsFor(session) {
  const persisted = (await journal('parts', 'getAll')).filter(part => part.sessionId === session.id);
  const bySequence = new Map(persisted.map(part => [part.seq, part]));
  for (const part of session.volatileParts || []) bySequence.set(part.seq, part);
  const parts = [...bySequence.values()].sort((a, b) => a.seq - b.seq);
  if (!parts.length) throw Error('尚无可保存的录音片段。');
  if (parts.some((part, index) => part.seq !== index)) throw Error('本机备份不连续，已保留原有片段，请勿清除浏览器数据。');
  return parts;
}
function wav(parts) {
  const size = parts.reduce((n, part) => n + part.pcm.byteLength, 0);
  const header = new ArrayBuffer(44), view = new DataView(header);
  const str = (at, value) => [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  str(0, 'RIFF'); view.setUint32(4, 36 + size, true); str(8, 'WAVEfmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); str(36, 'data'); view.setUint32(40, size, true);
  return new Blob([header, ...parts.map(part => part.pcm)], { type: 'audio/wav' });
}
async function digest(pcm) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', pcm))].map(n => n.toString(16).padStart(2, '0')).join('');
}

export function installLiveUI({ request, getState, onSaved, toast }) {
  const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = '/live.css'; document.head.append(css);
  const entry = document.createElement('button');
  entry.type = 'button'; entry.id = 'live-entry'; entry.className = 'button secondary live-entry'; entry.textContent = '边录边转写 · 流式 2.0';
  document.querySelector('#record-setup').append(entry);
  const dialog = document.createElement('dialog');
  dialog.className = 'dialog live-dialog'; dialog.id = 'live-dialog'; dialog.setAttribute('aria-labelledby', 'live-title');
  dialog.innerHTML = '<div class="dialog-heading"><div><span class="eyebrow">跟上此刻的声音</span><h2 id="live-title">边录边转写</h2></div><button type="button" class="icon-button" id="live-close" aria-label="关闭">×</button></div><p class="live-copy">使用麦克风，支持中文、英文和中英混合。首遍文字会继续修正，停止后等待最终结果。请保持此页面在前台。</p><label class="live-consent"><input type="checkbox" id="live-consent">我已单独开通豆包流式识别 2.0，同意实时发送录音并按音频时长计费（当前约 1 元/小时）。</label><p class="live-status" id="live-status" role="status">原始声音同时缓存在此设备。</p><div class="live-readout" id="live-readout" aria-live="polite">准备好后，就开始说吧。</div><div class="dialog-footer"><button type="button" class="button secondary" id="live-backup" hidden>保存本机备份</button><button type="button" class="button secondary" id="live-download" hidden>下载录音备份</button><button type="button" class="button primary" id="live-start">开始实时转写</button><button type="button" class="button primary" id="live-stop" hidden>停止并保存</button></div>';
  document.body.append(dialog);
  const $ = selector => dialog.querySelector(selector);
  let run = null, starting = false, backup = null, recovering = false;
  const status = (message, error = false) => {
    $('#live-status').textContent = message; $('#live-status').classList.toggle('failed', error);
  };
  const syncFor = session => {
    const state = getState();
    if (state.accountId !== session.accountId || !state.sync) throw Error('请先连接录音时使用的同一账号。');
    return state.sync;
  };
  async function saveJournal(session) {
    const sync = syncFor(session);
    const id = 'local-' + session.id;
    let note = await sync.getNote(id);
    const parts = await partsFor(session);
    if (!note?.audioStored) note = await sync.saveLocalNote({
      id, title: session.title + ' · 本机备份', sourceName: session.title + '.wav',
      language: session.language, template: session.template, localOnly: true, transcript: '', summary: '', ...note
    }, wav(parts));
    await finishJournal(metadata(session), parts);
    session.volatileParts = [];
    await onSaved(note);
    return note;
  }
  async function recoverList() {
    const state = getState();
    if (!state.accountId || !state.sync || recovering) return;
    try {
      const saved = (await journal('sessions', 'getAll')).filter(s => s.accountId === state.accountId && !s.saved && s.id !== run?.id);
      document.querySelector('#live-recovery')?.remove();
      if (!saved.length) return;
      const bar = document.createElement('div'); bar.id = 'live-recovery'; bar.className = 'live-recover';
      const text = document.createElement('span'); text.textContent = '有未保存的实时录音备份';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary'; button.textContent = '恢复到此设备';
      button.onclick = async () => {
        if (recovering) return;
        recovering = true; button.disabled = true;
        try {
          for (const session of saved) await saveJournal(session.id === backup?.id ? backup : session);
          bar.remove(); toast('录音备份已恢复到此设备，请在设备与同步中上传。');
          if (backup && saved.some(s => s.id === backup.id)) { backup = null; $('#live-backup').hidden = true; $('#live-download').hidden = true; }
        } catch (error) { toast(error.message, true); button.disabled = false; }
        finally { recovering = false; }
      };
      bar.append(text, button); document.querySelector('.main').prepend(bar);
    } catch { /* An unavailable journal must not prevent the rest of the app opening. */ }
  }
  function disconnect(r) {
    clearInterval(r.poll); r.poll = null;
    for (const node of [r.source, r.node, r.silent]) { try { node?.disconnect(); } catch {} }
    for (const track of r.stream?.getTracks() || []) { try { track.stop(); } catch {} }
    try { r.context?.close().catch(() => {}); } catch {}
  }
  function failNetwork(r, message) {
    if (r.failed) return;
    r.failed = true;
    status(message + ' 已停止发送，录音继续保存在此设备。', true);
  }
  function stoppedUI(r) {
    disconnect(r);
    if (run === r) run = null;
    $('#live-stop').hidden = true; $('#live-stop').disabled = false;
    $('#live-start').hidden = false; $('#live-start').disabled = false; $('#live-consent').disabled = false;
    void recoverList();
  }
  function keepBackup(r, message) {
    backup = r; $('#live-backup').hidden = false; $('#live-download').hidden = false;
    status(message, true);
  }
  function remoteStop(r) {
    if (!r.noteId) return Promise.resolve(null);
    if (!r.stopRequest) r.stopRequest = request('/api/live/' + r.noteId + '/stop', {
      method: 'POST', body: { autoSummarize: false }, signal: AbortSignal.timeout(35000)
    });
    return r.stopRequest;
  }
  function receivePCM(r, event) {
    if (event.data.flushed) { r.flushed?.(); return; }
    const pcm = event.data.pcm;
    if (!(pcm instanceof ArrayBuffer) || !pcm.byteLength) return;
    const seq = r.seq++;
    const part = { id: r.id + ':' + String(seq).padStart(7, '0'), sessionId: r.id, seq, pcm };
    r.write = r.write.then(async () => {
      if (r.localFailure) { r.volatileParts.push(part); return; }
      try { await journal('parts', 'put', part); }
      catch {
        r.volatileParts.push(part); r.localFailure = true; r.failed = true;
        // Stop the microphone immediately; finish the worklet's existing tail without further capture.
        for (const track of r.stream?.getTracks() || []) track.stop();
        void stop();
        return;
      }
      if (r.failed) return;
      if (r.pending >= 8) { failNetwork(r, '网络跟不上录音速度。'); return; }
      r.pending++;
      r.send = r.send.then(async () => {
        if (r.failed) return;
        await request('/api/live/' + r.noteId + '/chunk/' + seq, {
          method: 'POST', body: pcm, headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': await digest(pcm) },
          signal: AbortSignal.timeout(12000)
        });
      }).catch(error => failNetwork(r, error.message)).finally(() => { r.pending--; });
    }).catch(() => {
      r.localFailure = true; r.failed = true;
      for (const track of r.stream?.getTracks() || []) track.stop();
      void stop();
    });
  }
  async function start() {
    if (run || starting) return;
    const state = getState();
    if (!state.connected || !state.sync) { toast('请先连接电脑并打开同一账号。', true); return; }
    if (!$('#live-consent').checked) { toast('请先确认已开通流式 2.0 及本次录音计费。', true); return; }
    if (backup?.volatileParts?.length) { toast('请先下载或保存上一次录音备份，避免丢失尚未写入的声音。', true); return; }
    starting = true; $('#live-start').disabled = true;
    const r = { id: crypto.randomUUID(), accountId: state.accountId, title: '实时录音 ' + new Date().toLocaleString().replace(/[/:]/g, '-'),
      language: document.querySelector('#new-language').value, template: document.querySelector('#new-template').value,
      seq: 0, saved: false, write: Promise.resolve(), send: Promise.resolve(), pending: 0, failed: false, stopping: false, volatileParts: [] };
    try {
      r.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      r.context = new AudioContext({ sampleRate: 16000 }); await r.context.resume();
      await r.context.audioWorklet.addModule('/pcm-worklet.js');
      // Build the audio graph before opening a billable remote session.
      r.node = new AudioWorkletNode(r.context, 'tingji-pcm');
      r.source = r.context.createMediaStreamSource(r.stream); r.silent = r.context.createGain(); r.silent.gain.value = 0;
      r.node.port.onmessage = event => receivePCM(r, event);
      await journal('sessions', 'put', metadata(r));
      const note = await request('/api/live/start', { method: 'POST',
        body: { title: r.title, language: r.language, template: r.template, audioUploadConsent: true }, signal: AbortSignal.timeout(20000) });
      r.noteId = note.id;
      if (!r.noteId) throw Error('未能确认实时任务编号，请检查电脑上的记录后再开始。');
      run = r; backup = null; $('#live-backup').hidden = true; $('#live-download').hidden = true;
      r.source.connect(r.node).connect(r.silent).connect(r.context.destination);
      $('#live-start').hidden = true; $('#live-stop').hidden = false; $('#live-consent').disabled = true;
      status('正在录音 · 原文会持续修正'); $('#live-readout').textContent = '正在听…';
      let reading = false;
      r.poll = setInterval(async () => {
        if (reading || r.failed || r.stopping) return;
        reading = true;
        try {
          const current = await request('/api/notes/' + r.noteId, { signal: AbortSignal.timeout(4000) });
          if (r.stopping || run !== r) return;
          $('#live-readout').textContent = current.transcript || '正在听…';
          if (current.status === 'error') failNetwork(r, current.error || '识别已中断。');
        } catch { /* Chunk requests establish send failure; local recording continues meanwhile. */ }
        finally { reading = false; }
      }, 1200);
    } catch (error) {
      disconnect(r);
      if (r.noteId) { try { await remoteStop(r); } catch {} }
      if (r.seq) keepBackup(r, error.message);
      else {
        // The microphone graph never captured a part, so this empty journal isn't a recoverable recording.
        try { await journal('sessions', 'delete', r.id); } catch {}
        status(error.message, true);
      }
      stoppedUI(r);
    } finally { starting = false; $('#live-start').disabled = false; }
  }
  async function stop() {
    const r = run;
    if (!r || r.stopping) return;
    r.stopping = true; $('#live-stop').disabled = true; clearInterval(r.poll);
    status(r.localFailure ? '本机缓存写入失败，已立即停止录音，正在保留已有片段…' : '正在保存录音并等待最终文字…', r.localFailure);
    try {
      await new Promise(resolve => {
        const timer = setTimeout(() => { r.flushMissing = true; resolve(); }, 1500);
        r.flushed = () => { clearTimeout(timer); resolve(); };
        r.node.port.postMessage('flush');
      });
      try { r.source.disconnect(); } catch {}
      await r.write; await r.send;
      disconnect(r);
      let note;
      try { note = await remoteStop(r); } catch (error) { failNetwork(r, error.message); }
      if (note) $('#live-readout').textContent = note.transcript || '没有返回可显示的文字，录音备份仍可保存。';
      if (note?.asrComplete && !r.failed && !r.flushMissing) {
        const parts = await partsFor(r);
        const cached = await syncFor(r).cacheAudio(note, wav(parts));
        if (!cached) {
          keepBackup(r, '此设备已有本地修改，未覆盖。最终文字已在电脑保存，录音备份仍保留在此页面。');
        } else {
          await finishJournal(metadata(r), parts);
          await onSaved(cached);
          $('#live-readout').textContent = note.transcript;
          status('最终原文与录音已保存，可以回到笔记中提炼。');
        }
      } else {
        keepBackup(r, r.localFailure ? '本机缓存写入失败，录音已停止。请立即下载录音备份，再关闭此页面。' :
          '实时识别未完整结束。本机录音仍在，可保存备份后重新转写。');
        if (note) await onSaved(note);
      }
    } catch (error) { keepBackup(r, error.message); }
    finally { stoppedUI(r); }
  }
  entry.onclick = () => {
    if (getState().recording) { toast('请先结束当前录音。', true); return; }
    document.querySelector('#record-dialog').close(); dialog.showModal();
  };
  $('#live-start').onclick = start; $('#live-stop').onclick = stop;
  $('#live-close').onclick = () => {
    if (run || starting) { toast('请先停止并保存录音。'); return; }
    dialog.close();
  };
  dialog.addEventListener('cancel', event => { if (run || starting) event.preventDefault(); });
  dialog.addEventListener('close', () => document.querySelector('#record-button')?.focus());
  $('#live-backup').onclick = async () => {
    if (!backup) return;
    $('#live-backup').disabled = true;
    try {
      await saveJournal(backup); backup = null; $('#live-backup').hidden = true; $('#live-download').hidden = true;
      status('本机备份已保存，可在设备与同步中上传。'); void recoverList();
    } catch (error) { status(error.message, true); }
    finally { $('#live-backup').disabled = false; }
  };
  $('#live-download').onclick = async () => {
    if (!backup) return;
    try {
      const url = URL.createObjectURL(wav(await partsFor(backup)));
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = backup.title + '.wav'; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status('录音备份已交给浏览器下载。请确认文件已保存，再关闭页面。', true);
      // Keep the journal and RAM until explicit save succeeds: triggering download is not proof it completed.
    } catch (error) { status(error.message, true); }
  };
  window.addEventListener('beforeunload', event => {
    if (run || starting || backup?.volatileParts?.length) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('tingji:sync-status', recoverList);
  setTimeout(recoverList, 1800);
  return { recover: recoverList, isRecording: () => Boolean(run || starting) };
}

