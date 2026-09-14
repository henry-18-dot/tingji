import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tingji import audio_details as details, storage, jobs


class AudioDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'audio').mkdir()
        (self.root / 'audio' / 'record.webm').write_bytes(b'original')
        self.note = {'id': 'duration-note', 'audioFile': 'record.webm'}
        self.patches = [patch.object(storage, 'DATA', self.root), patch.object(details, 'PENDING', set()),
                        patch.object(details, 'FAILED', set()), patch.object(jobs, 'FFPROBE', 'fixture')]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def test_local_duration_updates_metadata_and_keeps_original(self):
        with patch.object(details.POOL, 'submit', side_effect=lambda callback: callback()), \
             patch.object(jobs, 'duration', return_value=123.5), \
             patch.object(storage, 'get_note', return_value=self.note), patch.object(storage, 'update_note') as update:
            details.ensure_duration(self.note)
            update.assert_called_once_with('duration-note', duration=123.5)
        self.assertEqual((self.root / 'audio' / 'record.webm').read_bytes(), b'original')

    def test_existing_duration_and_outside_path_do_not_start_work(self):
        with patch.object(details.POOL, 'submit') as submit:
            details.ensure_duration({**self.note, 'duration': 99})
            details.ensure_duration({**self.note, 'audioFile': '../../outside.wav'})
            submit.assert_not_called()

    def test_failed_probe_is_not_repeated_on_each_page_read(self):
        with patch.object(details.POOL, 'submit', side_effect=lambda callback: callback()) as submit, \
             patch.object(jobs, 'duration', side_effect=ValueError('invalid fixture')):
            details.ensure_duration(self.note)
            details.ensure_duration(self.note)
            self.assertEqual(submit.call_count, 1)


if __name__ == '__main__':
    unittest.main()
