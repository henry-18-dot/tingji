import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from tingji import actions, storage


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        for target,value in [('DATA',root),('DB',root/'notes.sqlite3')]:
            p=patch.object(storage,target,value);p.start();self.addCleanup(p.stop)
        storage.initialize()

    def test_response_loss_or_reload_does_not_repeat_provider_action(self):
        note=storage.create_note('本次请求')
        callback=Mock(return_value=note)
        first=actions.once('paid-action-one','live','',{},callback)
        storage.update_note(note['id'],status='error',error='模拟已发送但连接失败')
        storage.initialize()
        retry=actions.once('paid-action-one','live','',{},callback)
        self.assertEqual(first['id'],retry['id']);self.assertEqual(callback.call_count,1)

    def test_rejected_and_pending_receipts_never_run_again(self):
        callback=Mock(side_effect=ValueError('模拟失败'))
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'模拟失败'):
                actions.once('paid-action-two','transcribe','note-id',{'explicitRetry':True},callback)
        self.assertEqual(callback.call_count,1)
        with storage.connect() as db: db.execute("UPDATE action_receipts SET result=?",(json.dumps({'state':'pending'}),))
        with self.assertRaisesRegex(ValueError,'尚未确认'):
            actions.once('paid-action-two','transcribe','note-id',{'explicitRetry':True},callback)
        self.assertEqual(callback.call_count,1)

    def test_reused_id_for_changed_body_is_rejected(self):
        callback=Mock(return_value=storage.create_note('标题'))
        actions.once('paid-action-three','summarize','note-id',{},callback)
        with self.assertRaisesRegex(ValueError,'不同请求'):
            actions.once('paid-action-three','summarize','note-id',{'template':'meeting'},callback)
        self.assertEqual(callback.call_count,1)
