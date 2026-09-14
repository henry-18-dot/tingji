"""Run with python server.py; received recordings enter the automatic queue."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import secrets
import sys
import tempfile
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tingji import jobs, storage, sync, remote, actions, lifecycle, object_storage, course_naming, audio_details
from tingji import workflow, group_jobs, timetable, automation, prompt_lab

TOKEN = secrets.token_urlsafe(32)
WEB = storage.ROOT / 'web'
MAX_UPLOAD = 1024 * 1024 * 1024
EXTENSIONS = {'.mp3', '.mp4', '.wav', '.m4a', '.webm', '.ogg', '.opus', '.flac', '.mov', '.avi', '.mkv', '.aac', '.wma'}


class SessionExpiredError(PermissionError):
    """The request was rejected before any mutation was executed."""


def demo():
    existing = next((n for n in storage.list_notes() if n['isDemo']), None)
    if existing:
        return storage.get_note(existing['id'])
    segments = [
        {'start': 0, 'end': 14, 'speaker': '说话人 1', 'text': '今天讨论机械臂的位置控制实验。第一阶段先做单关节闭环，用编码器读位置，控制周期先定为十毫秒。'},
        {'start': 15, 'end': 32, 'speaker': '说话人 2', 'text': '先用 PID 做基线。注意角度统一用弧度，电机输出有限幅，积分项需要 anti-windup。我们还没有测过负载变化时的表现。'},
        {'start': 33, 'end': 51, 'speaker': '说话人 1', 'text': '小林负责记录阶跃响应，周五之前给出超调量和稳态误差。能不能把控制周期减到五毫秒，还要看串口往返延迟，今天先不作决定。'},
    ]
    return storage.create_note('机械臂控制讨论 · 示例', jobs.format_transcript(segments), isDemo=True, status='ready',
        sourceName='内置示例文稿', segments=segments, template='meeting', language='zh',
        summary='## 一句话概览\n\n先完成单关节位置闭环，用 PID 建立实验基线，再依据测量结果调整控制周期。\n\n## 已确定\n\n- 初始控制周期：10 ms。\n- 角度统一用弧度；输出设置限幅，积分项加入抗积分饱和（anti-windup）。\n- 小林在周五前记录阶跃响应，给出超调量和稳态误差。\n\n## 待确认\n\n- 5 ms 控制周期尚未决定，需先测量串口往返延迟。\n- 负载变化对控制性能的影响尚未验证。\n\n## 下一步\n\n1. 完成单关节闭环和实验记录。\n2. 测量通信延迟，再评估更短的控制周期。')


class Handler(BaseHTTPRequestHandler):
    server_version = 'Tingji/3.0'

    def log_message(self, fmt, *args):
        # No transcripts, request bodies, credentials, or query strings in logs.
        pass

    def gate(self, mutate=False):
        self.local_browser = remote.allowed_request(self.headers, self.server.server_port)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise PermissionError('不接受其他网站发起的请求。')
        if mutate and not secrets.compare_digest(self.headers.get('X-App-Token', ''), TOKEN):
            raise SessionExpiredError('连接已更新，请重试。')

    def response_headers(self, status=200, mime='application/json; charset=utf-8', size=None, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if size is not None:
            self.send_header('Content-Length', str(size))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def send_json(self, data, status=200):
        if isinstance(data, dict) and 'id' in data and 'transcript' in data:
            data = storage.public_note(data)
            if data.get('sourceNoteIds'):
                data = group_jobs.group_public(data)
        content = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.response_headers(status, size=len(content))
        self.wfile.write(content)

    def read_json(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('请求长度不正确。') from None
        if size < 0 or size > 5 * 1024 * 1024:
            raise ValueError('文本过长，请拆分后导入。')
        self.connection.settimeout(30)
        data = json.loads(self.rfile.read(size) or b'{}')
        if not isinstance(data, dict):
            raise ValueError('请求格式不正确。')
        return data

    def dispatch(self, method):
        try:
            self.gate(method != 'GET')
            path = urllib.parse.urlparse(self.path).path
            if method == 'GET':
                self.get(path)
            else:
                self.mutate(method, path)
        except SessionExpiredError as exc:
            self.send_json({'error': str(exc), 'code': 'session_expired'}, 403)
        except PermissionError as exc:
            self.send_json({'error': str(exc)}, 403)
        except KeyError:
            self.send_json({'error': '找不到这条记录或文件。'}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({'error': str(exc) or '请求内容无效。'}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            self.send_json({'error': '本地处理失败。请确认磁盘空间足够，重新打开听记后重试。'}, 500)

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PATCH(self):
        self.dispatch('PATCH')

    def do_PUT(self):
        self.dispatch('PUT')

    def do_OPTIONS(self):
        self.send_json({'error': '不支持跨站请求。'}, 403)

    def get(self, path):
        if path in ('/api/prompt-lab', '/api/prompt-lab/original'):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            note_id = query.get('noteId', [prompt_lab.EXAMPLE_ID])[0]
            return self.send_json({'transcript': prompt_lab.source(note_id)} if path.endswith('/original') else prompt_lab.state(note_id))
        if path == '/api/health':
            return self.send_json({'ok': True, 'app': 'tingji', 'version': '4.0.0', 'asr': 'standard'})
        if path == '/api/bootstrap':
            return self.send_json({'token': TOKEN, 'settings': storage.settings(),
                                  'accountId': storage.account_id(),
                                  'remote': remote.public_status(), 'localBrowser': self.local_browser,
                                  'capabilities': {'ffmpeg': bool(jobs.FFMPEG and jobs.FFPROBE)}, 'notes': storage.list_notes()})
        if path == '/api/automation':
            return self.send_json(automation.status())
        if path == '/api/notes':
            return self.send_json({'notes': storage.list_notes()})
        if path == '/api/timetable':
            return self.send_json(timetable.load())
        if path == '/api/timetable/week':
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self.send_json(timetable.week(query.get('start', [''])[0]))
        if path == '/api/batches':
            return self.send_json({'batches': workflow.list_batches()})
        if re.fullmatch(r'/api/batches/[\w-]+', path):
            return self.send_json(workflow.get_batch(path.rsplit('/', 1)[1]))
        if re.fullmatch(r'/api/notes/[\w-]+', path):
            note = storage.get_note(path.rsplit('/', 1)[1])
            audio_details.ensure_duration(note)
            return self.send_json(note)
        if re.fullmatch(r'/api/audio/[\w-]+', path):
            note = storage.get_note(path.rsplit('/', 1)[1])
            if not lifecycle.audio_available(note):
                raise KeyError()
            name = course_naming.download_name(note, Path(note['audioFile']).suffix)
            return self.file(storage.DATA / 'audio' / note['audioFile'], ranges=True, download_name=name)
        if path.startswith('/api/'):
            raise KeyError()
        filename = 'index.html' if path == '/' else urllib.parse.unquote(path.lstrip('/'))
        target = (WEB / filename).resolve()
        if not target.is_relative_to(WEB.resolve()) or not target.is_file():
            raise KeyError()
        return self.file(target)

    def file(self, path, ranges=False, download_name=None):
        if not path.is_file():
            raise KeyError()
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        extras = {}
        if download_name:
            extras['Content-Disposition'] = "inline; filename*=UTF-8''" + urllib.parse.quote(download_name, safe='')
        if ranges:
            extras['Accept-Ranges'] = 'bytes'
            request = self.headers.get('Range')
            if request:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', request)
                if not match or not (match[1] or match[2]):
                    return self.range_error(size)
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start >= size or start > end:
                    return self.range_error(size)
                status = 206
                extras['Content-Range'] = f'bytes {start}-{end}/{size}'
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix == '.js':
            mime = 'application/javascript; charset=utf-8'
        if path.suffix == '.css':
            mime = 'text/css; charset=utf-8'
        self.response_headers(status, mime, end - start + 1, extras)
        with path.open('rb') as file:
            file.seek(start)
            remain = end - start + 1
            while remain > 0:
                chunk = file.read(min(remain, 256 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)

    def range_error(self, size):
        self.response_headers(416, size=0, extra={'Content-Range': f'bytes */{size}'})

    def mutate(self, method, path):
        if method == 'POST' and path == '/api/upload':
            return self.upload()
        if method == 'POST' and path == '/api/timetable/import':
            return self.import_timetable()
        if path.startswith('/api/live/'):
            raise ValueError('应用内录音已取消，请从语音备忘录导入文件。')
        part = re.fullmatch(r'/api/sync/uploads/([\w-]+)/(\d+)', path)
        if method == 'PUT' and part:
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValueError('录音分块长度无效。') from None
            self.connection.settimeout(120)
            return self.send_json(sync.put_part(part[1], int(part[2]), self.rfile, length,
                                               self.headers.get('X-Chunk-SHA256', '')))
        data = self.read_json()
        if method == 'POST' and path == '/api/prompt-lab/update':
            return self.send_json(prompt_lab.update(data))
        if method == 'POST' and path == '/api/prompt-lab/generate':
            return self.send_json(prompt_lab.generate(data))
        if method == 'POST' and path == '/api/automation':
            storage.save_settings({'autoProcess': data.get('enabled')})
            return self.send_json(automation.status())
        if method == 'POST' and path == '/api/audio/batch-archive':
            return self.send_json(workflow.archive_batch(data.get('noteIds'), data.get('archived', True)))
        if method == 'POST' and path == '/api/timetable':
            return self.send_json(timetable.save(data.get('schedule', data), base_revision=data.get('baseRevision')))
        if method == 'POST' and path == '/api/groups':
            note = group_jobs.create_group(data.get('sourceIds'), title=str(data.get('title') or ''),
                                          lesson_id=str(data.get('lessonId') or ''), operation_id=data.get('operationId'))
            if data.get('lesson'):
                note = workflow.assign_lesson(note['id'], data['lesson'])
            return self.send_json(note, 201)
        if method == 'POST' and path == '/api/notes/merge':
            return self.send_json(group_jobs.merge_notes(data.get('noteIds'), title=str(data.get('title') or ''),
                                                        operation_id=data.get('operationId')), 201)
        grouped = re.fullmatch(r'/api/groups/([\w-]+)/(process)', path)
        if method == 'POST' and grouped:
            if data.get('autoSummarize') is False:
                raise ValueError('听记不提供只转文字，请生成整理笔记。')
            data['autoSummarize'] = True
            return self.send_json(group_jobs.start_group(grouped[1], data), 202)
        if method == 'POST' and path == '/api/batches':
            return self.send_json(workflow.create_batch(data.get('noteIds'), data.get('options') or {}, data.get('operationId')), 202)
        batch_control = re.fullmatch(r'/api/batches/([\w-]+)/control', path)
        if method == 'POST' and batch_control:
            if not isinstance(data.get('paused'), bool):
                raise ValueError('请选择暂停或继续批处理。')
            return self.send_json(workflow.pause_batch(batch_control[1], data['paused']))
        if method == 'POST' and path == '/api/audio/batch-delete':
            return self.send_json(workflow.delete_batch(data.get('noteIds'), data.get('scope', 'cache')))
        association = re.fullmatch(r'/api/notes/([\w-]+)/lesson', path)
        if method == 'POST' and association:
            return self.send_json(workflow.assign_lesson(association[1], data.get('lesson')))
        if method == 'POST' and path == '/api/sync/push':
            return self.send_json(sync.push(data))
        if method == 'POST' and path == '/api/sync/uploads':
            return self.send_json(sync.start_upload(data))
        complete = re.fullmatch(r'/api/sync/uploads/([\w-]+)/complete', path)
        if method == 'POST' and complete:
            result = sync.complete_upload(complete[1])
            received = result.get('note', result)
            automation.enqueue(received['id'])
            audio_details.ensure_duration(received)
            return self.send_json(result)
        if method == 'POST' and path == '/api/settings':
            # Older tabs submit their entire stale form. Only a deliberate mode
            # change in the current UI may replace the server's selected mode.
            if data.pop('asrModeChanged', False) is not True:
                data.pop('asrMode', None)
            saved = storage.save_settings(data)
            if 'dailyBudgetYuan' in data:
                automation.wake_budget_waiters()
            return self.send_json(saved)
        if method == 'POST' and path == '/api/demo':
            return self.send_json(demo())
        if method == 'POST' and path == '/api/notes':
            text = str(data.get('text', '')).strip()
            if not text or len(text) > 800000:
                raise ValueError('请粘贴文稿，单条最多 80 万字符。')
            return self.send_json(storage.create_note(str(data.get('title', '')), text,
                 language=self.language(data.get('language')), template=self.template(data.get('template')), sourceName='粘贴的文稿'), 201)
        match = re.fullmatch(r'/api/notes/([\w-]+)(?:/(transcribe|summarize|chat|query|audio-delete|archive|delete|restore))?', path)
        if not match:
            raise KeyError()
        note_id, action = match.groups()
        note = storage.get_note(note_id)
        if method == 'POST' and action in ('archive', 'delete', 'restore'):
            with storage.LOCK:
                note = storage.get_note(note_id)
                if action == 'archive':
                    if not isinstance(data.get('archived'), bool):
                        raise ValueError('请选择归档或移回。')
                    fields = {'archivedAt': (note.get('archivedAt') or storage.now()) if data['archived'] else None}
                else:
                    fields = {'trashedAt': (note.get('trashedAt') or storage.now()) if action == 'delete' else None}
                return self.send_json(storage.update_note(note_id, **fields))
        if method == 'POST' and action == 'audio-delete':
            return self.send_json(workflow.delete_one(note_id, data.get('scope', 'computer')))
        if method == 'PATCH' and not action:
            with jobs.LOCK:
                note = storage.get_note(note_id)
                title_only = 'title' in data and set(data) <= {'title', 'nameSource', 'baseRevision', 'baseTitle'}
                if title_only:
                    fields = sync.clean_metadata({'title': data['title'], 'nameSource': 'manual'})
                    base = data.get('baseRevision')
                    if isinstance(base, bool) or not isinstance(base, int):
                        raise ValueError('请刷新后改名。')
                    if base != note.get('revision', 0) and note.get('nameSource') == 'manual' and data.get('baseTitle') != note['title']:
                        raise ValueError('名称已在另一设备修改，请刷新后重试。')
                    return self.send_json(storage.update_note(note_id, **fields, titleUpdatedAt=storage.now()))
                base_revision = data.get('baseRevision')
                if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision != note.get('revision', 0):
                    raise ValueError('笔记有新版本，请同步后再编辑。')
                if note_id in jobs.ACTIVE or note.get('status') in ('transcribing', 'summarizing'):
                    raise ValueError('正在处理这条记录，请完成后再编辑。')
                task = note.get('asrTask') or {}
                if task.get('requestId') and task.get('state') not in ('completed', 'failed', 'rejected'):
                    raise ValueError('原转写任务尚未结束，请继续查询；完成后再编辑，以免覆盖修改。')
                clean = {}
                for key in ('title', 'transcript', 'summary'):
                    if key in data:
                        value = str(data[key])
                        if len(value) > (160 if key == 'title' else 800000):
                            raise ValueError('内容过长。')
                        clean[key] = value
                if 'transcript' in clean and clean['transcript'] != note['transcript']:
                    clean.update(summaryStale=bool(note['summary']), transcriptEdited=True)
                if 'summary' in clean:
                    clean['summaryStale'] = False
                updated = storage.update_note(note_id, **clean)
                if clean.get('summary', '').strip() and not updated.get('summaryStale'):
                    updated = lifecycle.apply_retention(note_id)
                return self.send_json(updated)
        if method != 'POST':
            raise KeyError()
        if action in ('transcribe', 'summarize', 'query'):
            if note.get('trashedAt'):
                raise ValueError('请先从回收站恢复笔记。')
            if 'language' in data:
                data['language'] = self.language(data['language'])
            if 'template' in data:
                data['template'] = self.template(data['template'])
            if data.get('autoSummarize') is False:
                raise ValueError('听记不提供只转文字，请生成整理笔记。')
            data['autoSummarize'] = True
            if note.get('sourceNoteIds'):
                return self.send_json(group_jobs.start_group(note_id, data), 202)
            if action == 'query':
                return self.send_json(jobs.start(note_id, action, data), 202)
            return self.send_json(actions.once(self.headers.get('X-Action-Id'), action, note_id, data,
                                              lambda: jobs.start(note_id, action, data)), 202)
        if action == 'chat':
            message = str(data.get('message', '')).strip()
            if not message or len(message) > 4000:
                raise ValueError('请输入问题，最多 4000 字。')
            return self.send_json(jobs.chat(note_id, message))
        raise KeyError()

    @staticmethod
    def language(value):
        return value if value in ('zh', 'en', 'auto') else 'auto'

    @staticmethod
    def template(value):
        return value if value in providers_templates else 'general'

    def upload(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('文件长度无效。') from None
        if not 0 < size <= MAX_UPLOAD:
            raise ValueError('文件为空或超过 1 GB，请拆分后导入。')
        original = urllib.parse.unquote(self.headers.get('X-File-Name', 'recording.webm'))
        name = original.replace('\\', '/').rsplit('/', 1)[-1][:200]
        suffix = Path(name).suffix.lower()
        if suffix not in EXTENSIONS:
            raise ValueError('不支持此格式，请导入 MP3、WAV、M4A、MP4、WebM 等音视频。')
        file_id = str(uuid.uuid4())
        metadata = {}
        if self.headers.get('X-Recorded-At'):
            metadata['recordedAt'] = self.headers['X-Recorded-At']
        if self.headers.get('X-Duration'):
            try:
                metadata['duration'] = float(self.headers['X-Duration'])
            except ValueError:
                raise ValueError('录音时长无效。') from None
        metadata['nameSource'] = self.headers.get('X-Name-Source', 'auto')
        checked = sync.clean_note(metadata)
        metadata = {k: checked[k] for k in ('recordedAt', 'duration', 'nameSource') if k in checked}
        target = storage.DATA / 'audio' / (file_id + suffix)
        temp = target.with_suffix('.uploading')
        self.connection.settimeout(120)
        try:
            digest = hashlib.sha256()
            with temp.open('xb') as file:
                remain = size
                while remain:
                    chunk = self.rfile.read(min(remain, 1024 * 1024))
                    if not chunk:
                        raise ValueError('文件传输中断，未保存为完整录音，请重新导入。')
                    file.write(chunk)
                    digest.update(chunk)
                    remain -= len(chunk)
            fingerprint = digest.hexdigest()
            with storage.LOCK:
                duplicate = next((n for n in storage.all_notes() if n.get('sourceHash') == fingerprint
                                  and n.get('fileSize') == size and not n.get('trashedAt')), None)
                if duplicate:
                    temp.unlink(missing_ok=True)
                    automation.enqueue(duplicate['id'])
                    return self.send_json({**storage.get_note(duplicate['id']), 'duplicateUpload': True}, 200)
                temp.replace(target)
                note = storage.create_note(Path(name).stem, sourceName=name, audioFile=target.name,
                        language=self.language(self.headers.get('X-Language')), template=self.template(self.headers.get('X-Template')),
                        fileSize=size, sourceHash=fingerprint, stage='已收到，等待自动转录', uploadedAt=storage.now(),
                        autoProcessState='waiting', autoQueuedAt=storage.now(), **metadata)
                note = storage.update_note(note['id'], audioUrl='/api/audio/' + note['id'])
            note = automation.enqueue(note['id'])
            audio_details.ensure_duration(note)
            return self.send_json(note, 201)
        except Exception:
            temp.unlink(missing_ok=True)
            raise

    def import_timetable(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('课表文件长度无效。') from None
        if not 0 < size <= 20 * 1024 * 1024:
            raise ValueError('请导入不超过 20 MB 的课表图片或 PDF。')
        name = urllib.parse.unquote(self.headers.get('X-File-Name', 'schedule.png')).replace('\\', '/').rsplit('/', 1)[-1][:200]
        suffix = Path(name).suffix.lower()
        if suffix not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.pdf'):
            raise ValueError('课表支持图片和 PDF；照片可先导出为 JPG。')
        folder = storage.DATA / 'imports'
        folder.mkdir(exist_ok=True)
        self.connection.settimeout(180)
        with tempfile.TemporaryDirectory(prefix='schedule-', dir=folder) as directory:
            target = Path(directory) / ('source' + suffix)
            with target.open('xb') as out:
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError('课表上传中断，请重新导入。')
                    out.write(chunk)
                    remaining -= len(chunk)
            return self.send_json(timetable.import_file(target, name, config=storage.settings(secrets=True)))


providers_templates = {'general', 'meeting', 'lecture', 'interview'}


def main():
    parser = argparse.ArgumentParser(description='听记本地服务')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    storage.initialize()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    jobs.resume_queries()
    group_jobs.recover()
    group_jobs.start_scheduler()
    workflow.start_scheduler()
    automation.start_scheduler()
    print(f'Tingji ready: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        from tingji.standard_jobs import SCHEDULER_STOP
        SCHEDULER_STOP.set()
        group_jobs.stop_scheduler()
        workflow.stop_scheduler()
        automation.stop_scheduler()


if __name__ == '__main__':
    main()
