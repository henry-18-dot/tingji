"""Local-only test service: real media/store/jobs, simulated cloud responses."""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('TINGJI_POLL_SECONDS', '1')
from tingji import providers, storage, object_storage, fast_providers
import server

COUNTS = {}


def event(kind, task_id=''):
    import json
    with (storage.DATA / 'qa-events.jsonl').open('a', encoding='utf-8') as file:
        file.write(json.dumps({'event': kind, 'id': task_id, 'time': time.time()}) + '\n')


def fake_upload(path, config, note_id):
    event('upload', note_id)
    return {'objectKey': 'tingji/qa/audio.mp3', 'downloadUrl': 'https://qa-private.tos-cn-beijing.volces.com/tingji/audio.mp3?qa-signed=mock',
            'expiresAt': '2099-01-01T00:00:00+00:00', 'bucket': 'qa-private', 'region': 'cn-beijing', 'fileSize': path.stat().st_size}


def fake_submit(audio_url, config, request_id, language='auto', audio_format='mp3'):
    event('submit', request_id)
    if '__QA_UNCERTAIN__' in config.get('hotwords', ''):
        raise providers.AsrSubmitUncertain('模拟提交回执丢失，只能查询原任务', request_id=request_id)
    return {'state': 'accepted', 'request_id': request_id, 'task_id': 'remote-' + request_id, 'code': '20000000'}


def fake_query(config, request_id):
    event('query', request_id)
    count = COUNTS.get(request_id, 0) + 1
    COUNTS[request_id] = count
    if '__QA_QUERY_FAILURE__' in config.get('hotwords', ''):
        raise providers.AsrQueryError('模拟查询中断，原任务保留', retryable=False, request_id=request_id)
    if count < 3:
        return {'state': 'queued' if count == 1 else 'processing', 'code': '20000002' if count == 1 else '20000001'}
    return {'state': 'completed', 'code': '20000000', 'result': {
        'text': '这是模拟 API 返回的测试转写。扭矩上限为 2 N·m。', 'utterances': [
            {'text': '这是模拟 API 返回的测试转写。', 'start_time': 0, 'end_time': 1200, 'additions': {'speaker': '1'}},
            {'text': '扭矩上限为 2 N·m。', 'start_time': 1300, 'end_time': 2500, 'additions': {'speaker': '1'}}]}}


def fake_deepseek(messages, config, max_tokens=8192):
    event('deepseek')
    time.sleep(0.3)
    return '# 模拟服务集成测试\n\n- 已读取原文中的扭矩上限：**2 N·m**。\n- 此结果仅用于本地界面与接口衔接验收，不代表真实 AI 质量。'


if __name__ == '__main__':
    if not os.environ.get('TINGJI_DATA_DIR') or Path(os.environ['TINGJI_DATA_DIR']).resolve() == (storage.ROOT / 'data').resolve():
        raise SystemExit('Fixture requires an isolated TINGJI_DATA_DIR.')
    providers.submit_transcription = fake_submit
    providers.query_transcription = fake_query
    providers.deepseek = fake_deepseek
    def fake_fast(path, config, request_id, language='auto'):
        event('fast', request_id)
        if '__QA_UNCERTAIN__' in config.get('hotwords', ''):
            raise providers.AsrSubmitUncertain('模拟极速响应中断，需要明确重试。', request_id=request_id)
        return {'text':'这是模拟极速转写。扭矩上限为 2 N·m。', 'utterances':[
            {'text':'这是模拟极速转写。扭矩上限为 2 N·m。', 'start_time':0, 'end_time':1000}]}
    fast_providers.transcribe_fast = fake_fast
    def forbid_live(*args, **kwargs):
        raise ValueError('此隔离服务禁止连接真实流式语音接口。')
    fast_providers.StreamSession = forbid_live
    object_storage.prepare_and_upload = fake_upload
    storage.initialize()
    storage.save_settings({'asrApiKey': 'qa-fake-asr', 'deepseekApiKey': 'qa-fake-ds',
        'tosAccessKeyId': 'qa-ak', 'tosSecretAccessKey': 'qa-sk', 'tosBucket': 'qa-private', 'tosRegion': 'cn-beijing',
        'tosPrivateConfirmed': True, 'hotwords': ''})
    sys.argv = [sys.argv[0], '--port', '18767']
    server.main()
