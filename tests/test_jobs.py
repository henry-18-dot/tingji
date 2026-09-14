"""Media and editing invariants retained after migration to standard ASR."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from tingji import jobs, providers, storage


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji-jobs-test-")
        self.root = Path(self.temp.name)
        self.data_patch = patch.object(storage, "DATA", self.root)
        self.db_patch = patch.object(storage, "DB", self.root / "notes.sqlite3")
        self.data_patch.start()
        self.db_patch.start()
        storage.initialize()

    def tearDown(self):
        jobs.ACTIVE.clear()
        self.db_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def make_audio_note(self):
        source = self.root / "audio" / "original.wav"
        source.write_bytes(b"original-recording-must-be-retained")
        return storage.create_note("Test", audioFile=source.name, language="zh", template="general"), source

    def test_active_job_blocks_duplicate_processing_and_uses_new_retry_options(self):
        note, _ = self.make_audio_note()
        storage.update_note(note["id"], asrComplete=True)
        with patch.object(storage, "settings", return_value={"asrApiKey": "test-key", "tosPrivateConfirmed": True, "tosBucket": "test-bucket", "tosAccessKeyId": "test-ak", "tosSecretAccessKey": "test-sk"}), patch.object(jobs.POOL, "submit") as submit:
            started = jobs.start(note["id"], "transcribe", {"language": "en", "template": "lecture", "cloudUploadConsent": True})
            self.assertEqual(started["language"], "en")
            self.assertEqual(started["template"], "lecture")
            self.assertFalse(started["asrComplete"])
            with self.assertRaisesRegex(ValueError, "正在处理"):
                jobs.start(note["id"], "transcribe")
            with self.assertRaisesRegex(ValueError, "正在处理"):
                jobs.chat(note["id"], "What happened?")
            self.assertEqual(submit.call_count, 1)

    def test_partial_recording_cannot_be_summarized_as_complete(self):
        note = storage.create_note("Part", "只有第一段", asrComplete=False)
        with patch.object(storage, "settings", return_value={"deepseekApiKey": "test-key"}), patch.object(jobs.POOL, "submit") as submit:
            with self.assertRaisesRegex(ValueError, "整条录音转写"):
                jobs.start(note["id"], "summarize")
            self.assertFalse(submit.called)

    def test_repeated_statements_within_one_chunk_are_not_deleted(self):
        within_chunk = [
            {"start": 10, "end": 11, "text": "同意。", "speaker": "1"},
            {"start": 12, "end": 13, "text": "同意。", "speaker": "2"},
        ]
        self.assertEqual(jobs.merge_segments([], within_chunk), within_chunk)
        previous = [{"start": 588, "end": 590, "text": "保留这段边界内容。", "speaker": "1"}]
        incoming = [
            {"start": 588.2, "end": 590, "text": "保留这段边界内容", "speaker": "1"},
            {"start": 591, "end": 593, "text": "保留这段边界内容。", "speaker": "2"},
        ]
        merged = jobs.merge_segments(previous, incoming)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[-1]["start"], 591)

    @unittest.skipUnless(jobs.FFMPEG and jobs.FFPROBE, "Existing FFmpeg installation is required")
    def test_real_ffmpeg_reads_and_converts_synthetic_wav(self):
        source = self.root / "audio" / "synthetic & 中文.wav"
        with wave.open(str(source), "wb") as file:
            file.setnchannels(2)
            file.setsampwidth(2)
            file.setframerate(48000)
            file.writeframes(b"\x00\x00\x00\x00" * 48000)
        self.assertAlmostEqual(jobs.duration(source), 1, delta=0.03)
        converted = self.root / "mono.wav"
        jobs.run_media([jobs.FFMPEG, "-nostdin", "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(converted)])
        with wave.open(str(converted), "rb") as file:
            self.assertEqual(file.getnchannels(), 1)
            self.assertEqual(file.getframerate(), 16000)
            self.assertEqual(file.getsampwidth(), 2)
            self.assertEqual(file.getnframes(), 16000)

    def test_segment_times_use_global_offsets_without_claiming_global_speaker_identity(self):
        result = {"utterances": [{"text": "Example", "start_time": 2000, "end_time": 5000, "additions": {"speaker": "1"}}]}
        segment = jobs.normalize_segments(result, 588, 1, True, 592)[0]
        self.assertEqual(segment["start"], 590)
        self.assertEqual(segment["end"], 593)
        self.assertIn("第 2 段", segment["speaker"])


if __name__ == "__main__":
    unittest.main()
