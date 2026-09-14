"""Durable local request allowance, not a provider balance or invoice.

Price policy checked 2026-09-12:
* Doubao recording recognition 2.0 (volc.seedasr.auc): public 0.8 CNY/hour,
  https://www.volcengine.com/ and https://www.volcengine.com/product/ark .
  We reserve 1 CNY/hour (1.25 safety factor), rounded up to whole seconds.
* https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ : peak, uncached
  Flash input/output 2/8 CNY per million tokens; V4 Pro 9/27. No cache or
  off-peak discount is assumed. Text input is bounded by UTF-8 byte length
  plus framing allowance; output uses the request's explicit max_tokens.

All amounts are integer millionths of CNY. SQLite BEGIN IMMEDIATE serializes
concurrent reservations across processes. Unknown dispatched requests retain
their allowance. Provider usage is recorded separately, never called a bill.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from . import storage

LOCAL_ZONE = timezone(timedelta(hours=8))
UNIT = 1_000_000
DEFAULT_LIMIT = 10
ASR_YUAN_PER_HOUR = Decimal('1')
DEEPSEEK_RATES = {
    'deepseek-flash': (2, 8),
    'deepseek-v4-flash': (2, 8),
    'deepseek-v4-flash-vision-exp': (2, 8),
    'deepseek-v4-pro': (9, 27),
}


class BudgetExceeded(ValueError):
    def __init__(self, message, snapshot=None):
        super().__init__(message)
        self.snapshot = snapshot or {}
        self.resetsAt = self.snapshot.get('resetsAt')


class RequestUncertain(ValueError):
    pass


def _clock():
    return datetime.now(LOCAL_ZONE)


def _day():
    now = _clock().astimezone(LOCAL_ZONE)
    return now.date().isoformat(), (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')


def _units(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('额度必须是有效金额。') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('额度必须是非负有效金额。')
    return int((amount * UNIT).to_integral_value(rounding=ROUND_CEILING))


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS budget_requests ('
               'request_key TEXT PRIMARY KEY, budget_date TEXT NOT NULL, '
               'amount_units INTEGER NOT NULL, state TEXT NOT NULL, '
               'kind TEXT NOT NULL, usage TEXT NOT NULL DEFAULT "{}", '
               'result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS budget_requests_day ON budget_requests(budget_date)')


def _limit(db):
    row = db.execute("SELECT value FROM settings WHERE key='dailyBudgetYuan'").fetchone()
    try:
        limit = _units(json.loads(row[0]) if row else DEFAULT_LIMIT)
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('每日额度设置无效，请在设置中填写 0 至 10000 元。') from None
    if limit > 10000 * UNIT:
        raise ValueError('每日额度设置不能超过 10000 元。')
    return limit


def _snapshot(db):
    day, resets = _day()
    limit = _limit(db)
    used = db.execute("SELECT COALESCE(SUM(amount_units),0) FROM budget_requests "
                      "WHERE budget_date=? AND state!='released'", (day,)).fetchone()[0]
    return {'dailyBudgetYuan': limit / UNIT, 'usedYuan': used / UNIT,
            'remainingYuan': max(0, limit - used) / UNIT,
            'budgetDate': day, 'resetsAt': resets}


def status():
    with storage.connect() as db:
        _schema(db)
        return _snapshot(db)


def _row(row):
    if not row:
        return None
    return {'key': row[0], 'budgetDate': row[1], 'amountYuan': row[2] / UNIT,
            'state': row[3], 'kind': row[4], 'usage': json.loads(row[5]),
            'result': json.loads(row[6]) if row[6] is not None else None}


def get(key):
    with storage.connect() as db:
        _schema(db)
        return _row(db.execute('SELECT * FROM budget_requests WHERE request_key=?', (key,)).fetchone())


def reserve(key, amount, *, kind='request'):
    if not isinstance(key, str) or not key or len(key) > 240:
        raise ValueError('请求额度编号无效。')
    units = _units(amount)
    with storage.connect() as db:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        existing = _row(db.execute('SELECT * FROM budget_requests WHERE request_key=?', (key,)).fetchone())
        if existing and existing['state'] != 'released':
            if _units(existing['amountYuan']) != units or existing['kind'] != kind:
                raise ValueError('同一请求的额度估算发生变化，已停止自动提交。')
            return existing
        snapshot = _snapshot(db)
        if units >= 20 * UNIT:
            raise BudgetExceeded('单次预留达到 20 元，已停止自动请求；请缩短材料。', snapshot)
        if units > _units(snapshot['dailyBudgetYuan']):
            raise BudgetExceeded('本次预留超过每日额度，请在设置中提高额度或缩短材料。', snapshot)
        if units > _units(snapshot['remainingYuan']):
            raise BudgetExceeded('今日额度不足，已排队等待北京时间次日 00:00。', snapshot)
        stamp = _clock().isoformat(timespec='seconds')
        db.execute('INSERT OR REPLACE INTO budget_requests VALUES (?,?,?,?,?,?,?,?,?)',
                   (key, snapshot['budgetDate'], units, 'reserved', kind, '{}', None, stamp, stamp))
        return _row(db.execute('SELECT * FROM budget_requests WHERE request_key=?', (key,)).fetchone())


def dispatch(key):
    """Claim the single network send; move an unsent old-day hold to today."""
    with storage.connect() as db:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        row = _row(db.execute('SELECT * FROM budget_requests WHERE request_key=?', (key,)).fetchone())
        if not row or row['state'] != 'reserved':
            raise RequestUncertain('这次请求已发出或状态不明，未自动重复提交；请核对原任务。')
        snapshot = _snapshot(db)
        if row['budgetDate'] == snapshot['budgetDate'] and snapshot['usedYuan'] > snapshot['dailyBudgetYuan']:
            raise BudgetExceeded('每日额度已调低，本次尚未发送；请调整额度后继续。', snapshot)
        if row['budgetDate'] != snapshot['budgetDate'] and row['amountYuan'] > snapshot['remainingYuan']:
            raise BudgetExceeded('今日额度不足，已排队等待北京时间次日 00:00。', snapshot)
        db.execute("UPDATE budget_requests SET state='dispatched',budget_date=?,updated_at=? WHERE request_key=?",
                   (snapshot['budgetDate'], _clock().isoformat(timespec='seconds'), key))


def release(key):
    """Release only an allocation that never reached the network-send marker."""
    with storage.connect() as db:
        _schema(db)
        db.execute("UPDATE budget_requests SET state='released',updated_at=? WHERE request_key=? AND state='reserved'",
                   (_clock().isoformat(timespec='seconds'), key))


def complete(key, *, result=None, usage=None):
    # Store provider token counts, not prompts, credentials, response headers,
    # or signed audio URLs. Retain the conservative allocation for the day.
    safe_usage = {name: value for name, value in (usage or {}).items()
                  if name in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                              'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
                  and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    with storage.connect() as db:
        _schema(db)
        db.execute("UPDATE budget_requests SET state='completed',usage=?,result=?,updated_at=? "
                   "WHERE request_key=? AND state='dispatched'",
                   (json.dumps(safe_usage), json.dumps(result, ensure_ascii=False) if result is not None else None,
                    _clock().isoformat(timespec='seconds'), key))


def estimate_asr(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise ValueError('缺少有效音频时长，不能估算转录额度。') from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('缺少有效音频时长，不能估算转录额度。')
    return _units(Decimal(math.ceil(seconds)) * ASR_YUAN_PER_HOUR / 3600) / UNIT


def estimate_deepseek(messages, model, max_tokens):
    if model not in DEEPSEEK_RATES:
        raise ValueError('这个 DeepSeek 模型尚无本地价格规则，已停止请求；请使用 deepseek-flash。')
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 384000:
        raise ValueError('输出长度无效，不能估算整理额度。')
    if not isinstance(messages, list) or any(not isinstance(item, dict) or
            not isinstance(item.get('content'), str) for item in messages):
        raise ValueError('当前额度估算只支持纯文字请求。')
    # One token cannot consume less than one UTF-8 byte for these text BPE
    # models. Include serialized roles and a generous chat framing allowance.
    input_bound = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 512 + 64 * len(messages)
    input_rate, output_rate = DEEPSEEK_RATES[model]
    return (input_bound * input_rate + max_tokens * output_rate) / UNIT
