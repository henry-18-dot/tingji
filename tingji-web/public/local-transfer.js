// The shared app exposes the computer receiver only in local mode.
export function createLocalTransfer({api, toast, getCourses, onImported}) {
  const button = document.querySelector('#local-transfer-open');
  const dialog = document.querySelector('#local-transfer-dialog');
  const files = document.querySelector('#local-inbox-files');
  const error = document.querySelector('#local-transfer-error');
  const course = document.querySelector('#local-inbox-course');
  let active = false;
  function showError(value) { error.textContent = value?.message || ''; error.hidden = !value; }
  async function load() {
    showError(null);
    files.textContent = '正在读取电脑收件箱…';
    const [status, inbox] = await Promise.all([api('/local/status'), api('/local/inbox')]);
    const link = document.querySelector('#local-transfer-link');
    const url = status.transferUrl ? new URL(status.transferUrl) : null;
    const valid = url?.protocol === 'https:' && /^[a-z0-9-]+\.[a-z0-9-]+\.ts\.net$/.test(url.hostname);
    link.hidden = !valid;
    if (valid) { link.href = url.href; link.textContent = url.href; }
    document.querySelector('#local-transfer-help').textContent = valid
      ? 'iPad 打开 Tailscale，再打开下方地址选择录音。电脑需保持运行。'
      : '先在电脑上连接 Tailscale 并配置听记入口。';
    course.replaceChildren(new Option('暂不分类', ''));
    for (const item of getCourses()) course.add(new Option(item.name, item.id));
    files.replaceChildren();
    const pending = (inbox.files || []).filter(item => !item.imported);
    document.querySelector('#local-inbox-course-row').hidden = !pending.length;
    if (!pending.length) { files.textContent = '还没有待导入的录音。'; return; }
    for (const item of pending) {
      const row = document.createElement('div'); row.className = 'local-inbox-row';
      const name = document.createElement('span'); name.textContent = item.name;
      const size = document.createElement('small'); size.textContent = `${(item.size / 1024 ** 2).toFixed(1)} MB`;
      const action = document.createElement('button'); action.type = 'button'; action.className = 'secondary'; action.textContent = '导入并整理';
      action.onclick = async () => {
        if (active) return;
        active = true; showError(null);
        files.querySelectorAll('button').forEach(b => b.disabled = true);
        try {
          const result = await api('/local/inbox/import', {method:'POST', body:{fileId:item.id, courseId:course.value || null}});
          await onImported(result.note); toast('已导入，电脑正在处理。'); await load();
        } catch (e) { showError(e); }
        finally { active = false; files.querySelectorAll('button').forEach(b => b.disabled = false); }
      };
      const label = document.createElement('div'); label.append(name, size); row.append(label, action); files.append(row);
    }
  }
  button.onclick = () => { dialog.showModal(); load().catch(showError); };
  document.querySelector('#local-inbox-refresh').onclick = () => { if (!active) load().catch(showError); };
  return {configure(enabled) { button.hidden = !enabled; }};
}
