"""Persist a paid-action receipt before scheduling or connecting to a provider."""
import hashlib
import json
from . import storage, sync


def once(operation_id, action, note_id, data, callback):
    operation_id = sync.identifier(operation_id)
    fingerprint = hashlib.sha256(json.dumps({'action':action,'noteId':note_id,'body':data},
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Only serialize identical actions; live callbacks need jobs.LOCK on another thread.
    with sync.upload_lock('action-' + hashlib.sha256(operation_id.encode()).hexdigest()):
        with storage.LOCK, storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload_hash,result FROM action_receipts WHERE operation_id=?',(operation_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError('这个操作编号已经用于不同请求，请重新打开操作入口。')
                receipt = json.loads(row[1])
                if receipt.get('noteId'):
                    return storage.get_note(receipt['noteId'])
                raise ValueError(receipt.get('error') or '本次请求已记录，但结果尚未确认。请先查看笔记状态，不要重复发送。')
            db.execute('INSERT INTO action_receipts VALUES (?,?,?)',(operation_id,fingerprint,'{"state":"pending"}'))
        try:
            note = callback()
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else '本次操作未能完成。请先查看已保留的笔记状态，再决定是否手动重试。'
            with storage.LOCK, storage.connect() as db:
                db.execute('UPDATE action_receipts SET result=? WHERE operation_id=?',
                           (json.dumps({'error':message},ensure_ascii=False),operation_id))
            raise ValueError(message) from None
        with storage.LOCK, storage.connect() as db:
            db.execute('UPDATE action_receipts SET result=? WHERE operation_id=?',
                       (json.dumps({'noteId':note['id']}),operation_id))
        return note
