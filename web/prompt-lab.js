import { sessionFetch } from './session.js';
import { renderMarkdown } from './knowledge.js';
const $ = selector => document.querySelector(selector);
const noteId = new URLSearchParams(location.search).get('noteId') || '6175b175-966f-4726-8218-008263d5d4a3';
let saved, busy = false, currentSummary = '', history = [], originalLoaded = false;
const status = (text, error = false) => { $('#status').textContent = text; $('#status').classList.toggle('error', error); };
function controls() {
  $('#update').disabled = busy || !saved;
  $('#generate').disabled = busy || !saved?.hasOriginal || $('#prompt').value.trim() !== saved?.prompt;
  $('#prompt').readOnly = busy; $('#intent').readOnly = busy;
}
async function request(path, body) {
  const response = await sessionFetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '请求未完成。');
  return result;
}
function versions() {
  $('#versions').replaceChildren(new Option('当前笔记', 'current'));
  for (const [index, item] of history.entries()) {
    const label = item.model === 'reviewed-draft' ? '修订稿' : 'Flash 原始输出';
    $('#versions').add(new Option(label + ' · ' + new Date(item.createdAt).toLocaleString('zh-CN'), String(index)));
  }
}
$('#versions').addEventListener('change', () => renderMarkdown($('#versions').value === 'current' ? currentSummary : history[Number($('#versions').value)].summary, $('#preview')));
$('#prompt').addEventListener('input', () => {controls(); status('可直接修改提示词，再填写意图并点击更新。');});
$('#update').addEventListener('click', async () => {
  const intent = $('#intent').value.trim();
  if (!intent) { status('先写一句你想改动的方向。', true); $('#intent').focus(); return; }
  busy = true; controls(); status('正在按你的意图更新提示词…');
  try {
    const result = await request('/api/prompt-lab/update', {noteId, operationId:crypto.randomUUID(), revision:saved.revision, prompt:$('#prompt').value, intent});
    saved = {...saved, ...result}; $('#prompt').value = saved.prompt;
    status('提示词已保存。可从原文生成新版笔记。');
  } catch (error) { status(error.message + ' 若连接中断，先刷新查看结果。', true); }
  finally {busy = false; controls();}
});
$('#generate').addEventListener('click', async () => {
  busy = true; controls(); status('正在从保留的原文生成笔记，通常需要一两分钟…');
  try {
    const result = await request('/api/prompt-lab/generate', {noteId, operationId:crypto.randomUUID(), revision:saved.revision});
    history.unshift(result);
    if (result.applied) currentSummary = result.summary;
    versions(); $('#versions').value = result.applied ? 'current' : '0';
    renderMarkdown(result.summary, $('#preview'));
    status(result.applied ? '新版笔记已保存。' : '候选已保存；笔记曾在别处修改，未覆盖。');
  } catch (error) { status(error.message + ' 若连接中断，先刷新查看结果。', true); }
  finally {busy = false; controls();}
});
$('#original-details').addEventListener('toggle', async () => {
  if (!$('#original-details').open || originalLoaded) return;
  try {const result = await request('/api/prompt-lab/original?noteId=' + encodeURIComponent(noteId)); $('#original').textContent = result.transcript; originalLoaded = true;}
  catch(error) {$('#original').textContent = error.message;}
});
try {
  saved = await request('/api/prompt-lab?noteId=' + encodeURIComponent(noteId));
  $('#course-name').textContent = saved.title; $('#prompt').value = saved.prompt;
  currentSummary = saved.summary; history = saved.history; versions();
  renderMarkdown(currentSummary, $('#preview'));
  status(saved.hasOriginal ? '填写意图后，点击更新。' : '没有保留的原文；可先调整提示词。');
} catch(error) {status(error.message, true);}
controls();
