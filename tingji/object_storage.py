"""Private TOS audio transport using Python's standard library.

The caller MUST obtain per-recording cloud-upload consent before calling
prepare_and_upload. Explicit deletion is limited to recorded objects of one note.
This module never creates buckets, changes bucket ACLs, retries uploads, or logs
credentials/pre-signed download URLs.
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_AUDIO_BYTES = 512 * 1024 * 1024
DOWNLOAD_TTL_SECONDS = 24 * 3600
PREFIX = 'tingji/'
ALGORITHM = 'TOS4-HMAC-SHA256'


class ObjectStorageError(ValueError):
    pass


def configured(config):
    return config.get('tosPrivateConfirmed') in (True, 'true', 'True', '1') and all(
        str(config.get(key) or '').strip() for key in ('tosBucket', 'tosAccessKeyId', 'tosSecretAccessKey'))


def validate_config(config):
    """Validate local values only; never accesses the bucket or tests credentials."""
    if config.get('tosPrivateConfirmed') not in (True, 'true', 'True', '1'):
        raise ObjectStorageError('请先在设置中确认 TOS 桶为私有，且没有允许公开访问的桶策略。')
    region = str(config.get('tosRegion') or 'cn-beijing').strip()
    bucket = str(config.get('tosBucket') or '').strip()
    if not re.fullmatch(r'(?:cn|ap|eu|us)-[a-z]+(?:-\d+)?', region):
        raise ObjectStorageError('TOS 地域格式不正确，例如 cn-beijing；请填写桶实际所在地域。')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ObjectStorageError('请填写 TOS 私有存储桶名称，使用 3–63 位小写字母、数字和连字符。')
    for key, label in (('tosAccessKeyId', 'Access Key ID'), ('tosSecretAccessKey', 'Secret Access Key')):
        value = str(config.get(key) or '').strip()
        if not value or len(value) > 4096 or re.search(r'[^\x21-\x7e]', value):
            raise ObjectStorageError(f'请填写有效的 TOS {label}，不要使用豆包语音 API Key。')
    token = str(config.get('tosSessionToken') or '').strip()
    if len(token) > 16384 or re.search(r'[^\x21-\x7e]', token):
        raise ObjectStorageError('TOS 临时凭证 Security Token 格式不正确。')
    return {'region': region, 'bucket': bucket, 'host': f'{bucket}.tos-{region}.volces.com'}


def _timestamp(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _query(params):
    return '&'.join(urllib.parse.quote(str(key), safe='-_.~') + '=' +
                    urllib.parse.quote(str(params[key]), safe='-_.~') for key in sorted(params))


def _signature(method, object_key, query, signed_headers, config, timestamp, payload_hash='UNSIGNED-PAYLOAD'):
    """TOS4 differs from AWS4: key derivation starts with the raw Secret Key."""
    region = str(config.get('tosRegion') or 'cn-beijing').strip()
    date = timestamp[:8]
    scope = f'{date}/{region}/tos/request'
    headers = {key.lower(): str(value).strip() for key, value in signed_headers.items()}
    names = ';'.join(sorted(headers))
    canonical_headers = ''.join(f'{name}:{headers[name]}\n' for name in sorted(headers))
    canonical_request = '\n'.join((method.upper(), '/' + urllib.parse.quote(object_key, safe='/~'),
                                   _query(query), canonical_headers, names, payload_hash))
    to_sign = '\n'.join((ALGORITHM, timestamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()))
    key = str(config['tosSecretAccessKey']).strip().encode()
    for component in (date, region, 'tos', 'request'):
        key = hmac.new(key, component.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(), scope, names


def _presigned_url(object_key, config, host, expires, now, method='GET'):
    timestamp = now.strftime('%Y%m%dT%H%M%SZ')
    region = str(config.get('tosRegion') or 'cn-beijing').strip()
    params = {'X-Tos-Algorithm': ALGORITHM,
              'X-Tos-Credential': f'{str(config["tosAccessKeyId"]).strip()}/{timestamp[:8]}/{region}/tos/request',
              'X-Tos-Date': timestamp, 'X-Tos-Expires': str(expires), 'X-Tos-SignedHeaders': 'host'}
    if config.get('tosSessionToken'):
        params['X-Tos-Security-Token'] = str(config['tosSessionToken']).strip()
    signature, _, _ = _signature(method, object_key, params, {'host': host}, config, timestamp)
    params['X-Tos-Signature'] = signature
    return f'https://{host}/' + urllib.parse.quote(object_key, safe='/~') + '?' + _query(params)


def presign_download(config, object_key, expires=DOWNLOAD_TTL_SECONDS, *, now=None):
    """Return a sensitive, bearer GET URL; pure local signing, no cloud request."""
    values = validate_config(config)
    if not isinstance(expires, int) or not 60 <= expires <= DOWNLOAD_TTL_SECONDS:
        raise ObjectStorageError('听记下载链接有效期应在 60 秒至 24 小时之间。')
    if not isinstance(object_key, str) or not object_key.startswith(PREFIX) or any(
            part in ('', '.', '..') for part in object_key.split('/')) or '\\' in object_key:
        raise ObjectStorageError('仅允许签名听记 tingji/ 目录中的音频对象。')
    if any(ord(character) < 32 for character in object_key):
        raise ObjectStorageError('音频对象名称包含无效字符。')
    return _presigned_url(object_key, config, values['host'], expires, _timestamp(now))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ObjectStorageError('TOS 返回了重定向，已停止上传。请核对存储桶地域；不会把音频转发到其他地址。')


def _upload(path, host, object_key, headers):
    url = f'https://{host}/' + urllib.parse.quote(object_key, safe='/~')
    try:
        # With an explicit Content-Length urllib/http.client streams this file.
        # No Base64 expansion, whole-file read, automatic retry, or redirect.
        with path.open('rb') as source:
            request = urllib.request.Request(url, data=source, headers=headers, method='PUT')
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=600) as response:
                if not 200 <= response.status < 300:
                    raise ObjectStorageError('TOS 未确认上传完成。请核对控制台后再决定是否重试。')
                return dict(response.headers)
    except urllib.error.HTTPError as error:
        error.close()
        hints = {400: '请求被拒绝，请核对地域和本机时间。', 401: '存储凭证无效。',
                 403: '存储凭证无权限或已过期，请检查 tingji/ 前缀的上传权限。',
                 404: '找不到存储桶，请核对桶名及地域。', 413: '文件过大。',
                 429: '存储服务请求受限，请稍后重试。'}
        raise ObjectStorageError(f'TOS 上传失败（HTTP {error.code}）：{hints.get(error.code, "存储服务暂不可用。") }') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ObjectStorageError('TOS 上传连接失败或超时，云端是否已保存尚不确定。原录音仍在本机；请先检查控制台，避免直接重复上传。') from None


def prepare_and_upload(path, config, note_id):
    """Hash and upload an already prepared audio file after caller-side consent.

    Returns a dict containing a SENSITIVE downloadUrl. Do not put the whole
    dict into frontend responses or logs. No audio transcode occurs here.
    """
    values = validate_config(config)
    source = Path(path).resolve()
    if not source.is_file():
        raise ObjectStorageError('找不到待上传音频，原始记录不会被删除。')
    size = source.stat().st_size
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise ObjectStorageError('上传音频必须非空且不超过 512 MB；请先压缩或拆分音频。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(note_id)):
        raise ObjectStorageError('记录编号无效，已取消上传。')
    suffix = source.suffix.lower()
    if suffix not in {'.wav', '.mp3', '.ogg', '.opus', '.m4a', '.mp4', '.flac', '.aac', '.webm'}:
        raise ObjectStorageError('请先将音视频准备为可识别的音频文件后上传。')
    digest = hashlib.sha256()
    with source.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    # No source filename/person name appears in the remote object key.
    object_key = f'{PREFIX}{note_id}/{sha256[:32]}{suffix}'
    instant = _timestamp()
    timestamp = instant.strftime('%Y%m%dT%H%M%SZ')
    content_type = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
    signed = {'host': values['host'], 'content-type': content_type, 'x-tos-date': timestamp,
              'x-tos-content-sha256': sha256, 'x-tos-storage-class': 'STANDARD', 'x-tos-acl': 'private'}
    if config.get('tosSessionToken'):
        signed['x-tos-security-token'] = str(config['tosSessionToken']).strip()
    signature, scope, names = _signature('PUT', object_key, {}, signed, config, timestamp, sha256)
    headers = {**signed, 'Content-Length': str(size),
               'Authorization': f'{ALGORITHM} Credential={str(config["tosAccessKeyId"]).strip()}/{scope}, SignedHeaders={names}, Signature={signature}'}
    response_headers = _upload(source, values['host'], object_key, headers)
    download_at = _timestamp()
    return {'objectKey': object_key,
            'downloadUrl': presign_download(config, object_key, now=download_at),
            'expiresAt': (download_at + timedelta(seconds=DOWNLOAD_TTL_SECONDS)).isoformat(),
            'bucket': values['bucket'], 'region': values['region'], 'fileSize': size,
            'sha256': sha256, 'etag': next((v for k, v in response_headers.items() if k.lower() == 'etag'), ''),
            'versionId': next((v for k, v in response_headers.items() if k.lower() == 'x-tos-version-id'), '')}


def _delete_object(config, object_key, version_id=''):
    values = validate_config(config)
    timestamp = _timestamp().strftime('%Y%m%dT%H%M%SZ')
    payload_hash = hashlib.sha256(b'').hexdigest()
    signed = {'host': values['host'], 'x-tos-date': timestamp, 'x-tos-content-sha256': payload_hash}
    if config.get('tosSessionToken'):
        signed['x-tos-security-token'] = str(config['tosSessionToken']).strip()
    query = {'versionId': version_id} if version_id else {}
    signature, scope, names = _signature('DELETE', object_key, query, signed, config, timestamp, payload_hash)
    headers = {**signed, 'Authorization': f'{ALGORITHM} Credential={str(config["tosAccessKeyId"]).strip()}/{scope}, SignedHeaders={names}, Signature={signature}'}
    url = f'https://{values["host"]}/' + urllib.parse.quote(object_key, safe='/~')
    if query:
        url += '?' + _query(query)
    request = urllib.request.Request(url, headers=headers, method='DELETE')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise ObjectStorageError('云端录音删除失败，请稍后重试。')
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status == 404:
            return
        hint = '请检查存储密钥的删除权限。' if status in (401, 403) else '请稍后重试。'
        raise ObjectStorageError('云端录音删除失败。' + hint) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ObjectStorageError('云端录音删除未完成，请重试。本机录音保留。') from None


def delete_note_objects(note, config):
    """Delete only the exact uploaded objects recorded on this completed note.

    No prefix listing or bucket-wide deletion. Retries of an explicit delete are
    safe: absent objects count as deleted. Recorded versions use versionId.
    """
    note_id = str(note.get('id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', note_id) or not note.get('asrComplete'):
        raise ObjectStorageError('转写完成后可删除录音。')
    tasks = [note.get('asrTask') or {}, *(note.get('asrTaskHistory') or [])]
    targets = {}
    for task in tasks:
        cloud = task.get('cloudObject') or {}
        key = cloud.get('objectKey')
        if not key:
            continue
        attempt = str(task.get('attemptId') or '')
        allowed = {f'{PREFIX}{note_id}/'}
        if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', attempt):
            allowed.add(f'{PREFIX}{note_id}-{attempt}/')
        if not any(key.startswith(prefix) for prefix in allowed) or any(
                part in ('', '.', '..') for part in key.split('/')) or '\\' in key or any(ord(c) < 32 for c in key):
            raise ObjectStorageError('云端录音路径无效。')
        scoped = {**config, 'tosBucket': cloud.get('bucket'), 'tosRegion': cloud.get('region')}
        validate_config(scoped)
        targets[(scoped['tosBucket'], scoped['tosRegion'], key, cloud.get('versionId') or '')] = scoped
    # Validate the whole target set before making any destructive request.
    for (_, _, key, version), scoped in targets.items():
        _delete_object(scoped, key, version)
    return len(targets)


def delete_cloud_audio(note_id, config=None):
    """Delete only the recorded cloud objects for one note.

    The local original and all device caches are outside this function's scope.
    A durable ``cloudAudioDeletedAt`` marker makes retries idempotent while
    keeping the local ``audioDeletedAt`` state independent.
    """
    from . import jobs, storage

    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', str(note_id)):
        raise ObjectStorageError('记录编号无效。')
    with jobs.LOCK, storage.LOCK:
        note = storage.get_note(note_id)
        task = note.get('asrTask') or {}
        processing = (note_id in jobs.ACTIVE or note.get('status') in ('transcribing', 'summarizing')
                      or (task.get('requestId') and task.get('state') not in ('completed', 'failed', 'rejected')))
        if processing:
            raise ObjectStorageError('录音正在处理，完成后再删除。')
        if note.get('cloudAudioDeletedAt'):
            return note
        if not note.get('asrComplete'):
            raise ObjectStorageError('转写完成后可删除云端录音。')
        delete_note_objects(note, config if config is not None else storage.settings(secrets=True))
        return storage.update_note(note_id, cloudAudioDeletedAt=storage.now())
