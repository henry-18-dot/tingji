from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
import uuid

from .config import get_settings


ASR_RESOURCE_ID = "volc.seedasr.auc"
ASR_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
ASR_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SubmitUncertain(ProviderError):
    pass


class SubmitRejected(ProviderError):
    pass


class OutputLengthError(ProviderError):
    def __init__(self, output: str, usage: dict):
        super().__init__("DeepSeek 输出达到长度上限，未保存不完整结果。")
        self.output, self.usage = output, usage


class QueryError(ProviderError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def asr_headers(request_id: str) -> dict[str, str]:
    cfg = get_settings()
    headers = {"X-Api-Resource-Id": ASR_RESOURCE_ID,
               "X-Api-Request-Id": request_id, "X-Api-Sequence": "-1"}
    if cfg.volcengine_asr_api_key:
        headers["X-Api-Key"] = cfg.volcengine_asr_api_key
    elif cfg.volcengine_asr_app_id and cfg.volcengine_asr_access_token:
        headers.update({"X-Api-App-Key": cfg.volcengine_asr_app_id,
                        "X-Api-Access-Key": cfg.volcengine_asr_access_token})
    else:
        raise ProviderError("火山标准语音识别尚未配置，请联系站长。")
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _asr_post(url: str, body: dict, headers: dict[str, str], timeout: int) -> tuple[int, dict, dict | None]:
    request = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                     {"Content-Type": "application/json", **headers}, method="POST")
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status, response_headers, raw = response.getcode(), dict(response.headers), response.read()
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    return status, response_headers, parsed


def _asr_response(headers: dict, body: dict | None) -> tuple[str, str, str, dict]:
    body = body if isinstance(body, dict) else {}
    embedded = body.get("headers") if isinstance(body.get("headers"), dict) else {}
    fields = {str(k).lower(): str(v) for k, v in {**embedded, **body, **headers}.items()}
    payload = body.get("body") if isinstance(body.get("body"), dict) else body
    return fields.get("x-api-status-code", ""), fields.get("x-api-message", ""), fields.get("x-tt-logid", ""), payload


def submit_asr(audio_url: str, request_id: str, *, hotwords: str = "") -> dict:
    headers = asr_headers(request_id)
    words = list(dict.fromkeys(x.strip() for x in re.split(r"[,，;；\n]", hotwords) if x.strip()))[:100]
    request_body = {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
                    "enable_ddc": False, "show_utterances": True, "enable_speaker_info": True,
                    "ssd_version": "300"}
    if words:
        request_body["corpus"] = {"context": json.dumps({"hotwords": [{"word": x} for x in words]}, ensure_ascii=False)}
    try:
        status, response_headers, body = _asr_post(
            ASR_SUBMIT_URL, {"audio": {"url": audio_url, "format": "mp3"}, "request": request_body}, headers, 60
        )
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise SubmitUncertain("语音任务提交结果未知，将只查询原请求编号。") from exc
    code, message, log_id, payload = _asr_response(response_headers, body)
    if code == "20000000" and 200 <= status < 300:
        task_id = payload.get("task_id") or request_id
        return {"state": "accepted", "taskId": str(task_id), "code": code, "logId": log_id}
    if re.search(r"duplicat|already|重复", message, re.I) or status in {408, 409} or status >= 500 or code.startswith("550") or not code:
        raise SubmitUncertain("语音任务受理状态未知，将只查询原请求编号。")
    raise SubmitRejected(f"语音任务被拒绝（{code or 'HTTP ' + str(status)}）。")


def query_asr(request_id: str) -> dict:
    headers = asr_headers(request_id)
    headers.pop("X-Api-Sequence", None)
    try:
        status, response_headers, body = _asr_post(ASR_QUERY_URL, {}, headers, 30)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise QueryError("暂时无法查询语音任务。") from exc
    code, message, log_id, payload = _asr_response(response_headers, body)
    base = {"code": code, "logId": log_id}
    if code in {"20000001", "20000002"} and 200 <= status < 300:
        return {**base, "state": "processing" if code == "20000001" else "queued"}
    if code == "20000003" and 200 <= status < 300:
        return {**base, "state": "completed", "result": {"text": "", "utterances": [], "silent": True}}
    if code == "20000000" and 200 <= status < 300:
        result = payload.get("result")
        if isinstance(result, list):
            result = result[0] if len(result) == 1 else None
        if not isinstance(result, dict):
            raise QueryError("语音任务完成但结果暂不完整。")
        return {**base, "state": "completed", "result": result, "audioInfo": payload.get("audio_info", {})}
    if re.search(r"not\s+(?:found|exist)|does\s+not\s+exist|任务不存在|未找到任务", message, re.I):
        return {**base, "state": "not_found"}
    if code.startswith("550") or status in {408, 429} or status >= 500 or not code:
        raise QueryError("本次查询没有得到有效状态。", retryable=status not in {400, 401, 402, 403, 404})
    return {**base, "state": "failed"}


def deepseek(messages: list[dict], max_tokens: int = 16000, *, thinking: bool = False,
             json_output: bool = False, model: str | None = None) -> tuple[str, dict]:
    cfg = get_settings()
    if not cfg.deepseek_api_key:
        raise ProviderError("DeepSeek 尚未配置，请联系站长。")
    body = {"model": model or cfg.deepseek_model, "messages": messages, "stream": False,
            "max_tokens": max_tokens, "thinking": {"type": "enabled" if thinking else "disabled"}}
    if thinking:
        body["reasoning_effort"] = "low"
    if json_output:
        body["response_format"] = {"type": "json_object"}
        body["temperature"] = 0.2
    request = urllib.request.Request(DEEPSEEK_URL, json.dumps(body, ensure_ascii=False).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + cfg.deepseek_api_key},
                                     method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=600) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise ProviderError(f"DeepSeek 请求失败（HTTP {status}）。", retryable=status == 429) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise SubmitUncertain("DeepSeek 请求结果未知，已停止自动重发。") from exc
    choices = result.get("choices") or []
    choice = choices[0] if choices else {}
    usage = {k: v for k, v in (result.get("usage") or {}).items() if isinstance(v, int) and v >= 0}
    if choice.get("finish_reason") == "length":
        raise OutputLengthError(str((choice.get("message") or {}).get("content") or ""), usage)
    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("DeepSeek 没有返回可用内容。")
    return content.strip(), usage


def new_request_id() -> str:
    return str(uuid.uuid4())
