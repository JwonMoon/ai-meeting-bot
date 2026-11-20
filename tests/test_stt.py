"""음성 인식 부품 시험. ffmpeg 와 모델 없이 돈다."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingbot import audio, stt  # noqa: E402
from meetingbot.audio import Chunk  # noqa: E402
from meetingbot.config import ConfigError, Settings  # noqa: E402

from test_core import SETTINGS  # noqa: E402

FFMPEG_LOG = """
[silencedetect @ 0x1] silence_start: 2.04806
[silencedetect @ 0x1] silence_end: 5.05594 | silence_duration: 3.00787
[silencedetect @ 0x1] silence_start: 75.0079
[silencedetect @ 0x1] silence_end: 76.032 | silence_duration: 1.02406
[silencedetect @ 0x1] silence_start: 81.024
"""


def with_stt(**overrides) -> Settings:
    values = dict(SETTINGS.__dict__)
    values.update(overrides)
    return Settings(**values)


class SilenceParsingTest(unittest.TestCase):
    def test_parses_pairs_and_closes_trailing_silence(self):
        silences = audio.parse_silences(FFMPEG_LOG, duration=101.0)
        self.assertEqual(len(silences), 3)
        self.assertAlmostEqual(silences[0][0], 2.048, places=2)
        self.assertEqual(silences[2], (81.024, 101.0))

    def test_no_silence(self):
        self.assertEqual(audio.parse_silences("nothing here", 10.0), [])


class ChunkPlanningTest(unittest.TestCase):
    def test_silence_is_left_out_and_speech_is_grouped(self):
        silences = [(2.0, 5.0), (75.0, 76.0), (81.0, 101.0)]
        chunks = audio.plan_chunks(101.0, silences, target_seconds=60, max_seconds=90)
        self.assertEqual(len(chunks), 2)
        # 첫 2초와 뒤 70초 말소리는 한 묶음 (합쳐도 90초 아래), 마지막 20초 무음은 빠짐.
        self.assertAlmostEqual(chunks[0].start, 0.0)
        self.assertAlmostEqual(chunks[0].end, 75.2)
        self.assertAlmostEqual(chunks[1].start, 75.8)
        self.assertAlmostEqual(chunks[1].end, 81.2)

    def test_long_speech_without_silence_is_split_at_target(self):
        chunks = audio.plan_chunks(200.0, [], target_seconds=60, max_seconds=90)
        self.assertEqual([round(c.seconds) for c in chunks], [60, 60, 80])
        self.assertAlmostEqual(chunks[-1].end, 200.0)

    def test_many_short_utterances_are_merged_up_to_target(self):
        # 10초 말, 1초 쉼 반복: 60초 넘을 때까지 묶는다.
        silences = [(10 * k + 10, 10 * k + 11) for k in range(0, 20, 1)]
        silences = [(s, e) for s, e in silences if e <= 220]
        chunks = audio.plan_chunks(220.0, silences, target_seconds=60, max_seconds=90)
        self.assertTrue(all(c.seconds <= 90 for c in chunks))
        self.assertTrue(all(c.seconds >= 50 for c in chunks[:-1]))

    def test_all_silence_gives_no_chunks(self):
        self.assertEqual(audio.plan_chunks(30.0, [(0.0, 30.0)], 60, 90), [])

    def test_tiny_blips_are_ignored(self):
        self.assertEqual(audio.plan_chunks(30.1, [(0.0, 30.0)], 60, 90), [])

    def test_timestamp(self):
        self.assertEqual(audio.format_timestamp(3725.9), "01:02:05")


class FfmpegLocationTest(unittest.TestCase):
    def tearDown(self):
        audio.configure("")

    def test_tool_uses_configured_dir(self):
        audio.configure("/opt/ff/")
        self.assertEqual(audio.tool("ffmpeg"), "/opt/ff/ffmpeg")
        audio.configure("")
        self.assertEqual(audio.tool("ffprobe"), "ffprobe")

    def test_missing_tools_mention_setting(self):
        audio.configure("/nowhere")
        with self.assertRaises(audio.AudioError) as caught:
            audio.ensure_ffmpeg()
        self.assertIn("FFMPEG_DIR", str(caught.exception))

    def test_ffmpeg_without_mp3_is_rejected(self):
        fake = mock.Mock(returncode=0, stdout=" A..... aac  AAC\n A..... flac FLAC\n")
        with mock.patch.object(audio.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(audio, "_run", return_value=fake):
            with self.assertRaises(audio.AudioError) as caught:
                audio.ensure_ffmpeg()
        self.assertIn("mp3", str(caught.exception))


class VolumeAndFallbackTest(unittest.TestCase):
    def test_parse_volume(self):
        text = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -27.3 dB\n[Parsed_volumedetect_0 @ 0x1] max_volume: -5.0 dB\n"
        self.assertEqual(audio.parse_volume(text), (-27.3, -5.0))
        self.assertEqual(audio.parse_volume("nothing"), (None, None))

    def test_fixed_chunks(self):
        self.assertEqual([round(c.seconds) for c in audio.fixed_chunks(200.0, 60)], [60, 60, 80])
        self.assertEqual(audio.fixed_chunks(30.0, 60), [Chunk(0.0, 30.0)])
        self.assertEqual(audio.fixed_chunks(0.0, 60), [])

    def _prepare(self, settings, mean_db, silences, duration=600.0):
        notes = []
        with mock.patch.object(audio, "ensure_ffmpeg"), \
             mock.patch.object(audio, "to_wav", return_value=Path("/w/audio.wav")), \
             mock.patch.object(audio, "probe_duration", return_value=duration), \
             mock.patch.object(audio, "measure_volume", return_value=(mean_db, -3.0)), \
             mock.patch.object(audio, "detect_silences", return_value=silences) as detect, \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "mkdir"):
            _, _, chunks = stt.prepare(settings, Path("/in/a.mp3"), Path("/w"), notes.append)
        return chunks, detect, notes

    def test_quiet_recording_lowers_threshold(self):
        settings = with_stt(stt_command="x {audio}", stt_silence_db=-40.0)
        _, detect, _ = self._prepare(settings, mean_db=-55.0, silences=[])
        self.assertEqual(detect.call_args[0][2], -65.0)  # 평균 -55 보다 10 아래
        _, detect, _ = self._prepare(settings, mean_db=-20.0, silences=[])
        self.assertEqual(detect.call_args[0][2], -40.0)  # 시끄러우면 설정값 그대로

    def test_low_speech_coverage_falls_back_to_whole_file(self):
        settings = with_stt(stt_command="x {audio}")
        chunks, _, notes = self._prepare(settings, mean_db=-30.0, silences=[(30.0, 600.0)])
        self.assertAlmostEqual(sum(c.seconds for c in chunks), 600.0)
        self.assertTrue(any("경고" in n for n in notes))

    def test_normal_coverage_keeps_silence_skipping(self):
        settings = with_stt(stt_command="x {audio}")
        chunks, _, notes = self._prepare(settings, mean_db=-30.0, silences=[(100.0, 130.0)])
        self.assertAlmostEqual(sum(c.seconds for c in chunks), 570.4, places=1)
        self.assertFalse(any("경고" in n for n in notes))

    def test_skip_silence_off(self):
        settings = with_stt(stt_command="x {audio}", stt_skip_silence=False)
        chunks, detect, _ = self._prepare(settings, mean_db=-30.0, silences=[(100.0, 130.0)])
        detect.assert_not_called()
        self.assertEqual(len(chunks), 10)


class CommandTest(unittest.TestCase):
    def test_per_file_command_substitutes_placeholders(self):
        settings = with_stt(stt_command="docker exec box python3 /w/t.py --lang {language} --max {max_new_tokens} {audio}", stt_language="Korean")
        command, batch = stt.build_command(settings, ["/data/001.wav"])
        self.assertFalse(batch)
        self.assertEqual(command, ["docker", "exec", "box", "python3", "/w/t.py", "--lang", "Korean", "--max", "400", "/data/001.wav"])

    def test_batch_command_expands_all_files(self):
        settings = with_stt(stt_command="python3 w.py {audios}")
        command, batch = stt.build_command(settings, ["a.wav", "b.wav"])
        self.assertTrue(batch)
        self.assertEqual(command, ["python3", "w.py", "a.wav", "b.wav"])
        self.assertTrue(stt.is_batch(settings))

    def test_missing_placeholder_or_command_is_config_error(self):
        with self.assertRaises(ConfigError):
            stt.build_command(with_stt(stt_command="python3 w.py"), ["a.wav"])
        with self.assertRaises(ConfigError):
            stt.is_batch(with_stt(stt_command=""))

    def test_path_map(self):
        self.assertEqual(stt.map_path(Path("/home/bot/data/j/001.wav"), "/home/bot/data:/data"), "/data/j/001.wav")
        self.assertEqual(stt.map_path(Path("/x/001.wav"), ""), "/x/001.wav")
        with self.assertRaises(ConfigError):
            stt.map_path(Path("/elsewhere/001.wav"), "/home/bot/data:/data")

    def test_text_output_prefers_json_line(self):
        self.assertEqual(stt.parse_text_output("loading...\n{\"text\": \" 안녕 \"}\n"), "안녕")
        self.assertEqual(stt.parse_text_output("  그냥 글  \n"), "그냥 글")


class RunnerTest(unittest.TestCase):
    """실제 자식 프로세스(파이썬 한 줄짜리)를 띄워 시간 초과와 결과 처리를 확인한다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pieces = [(Chunk(0.0, 10.0), self.tmp / "001.wav"), (Chunk(12.0, 20.0), self.tmp / "002.wav")]

    def test_per_chunk_runner_collects_segments(self):
        settings = with_stt(stt_command=sys.executable + " -c \"import sys; print('글', sys.argv[1][-7:])\" {audio}")
        segments = list(stt._run_per_chunk(settings, self.pieces, ""))
        self.assertEqual([s.text for s in segments], ["글 001.wav", "글 002.wav"])
        self.assertEqual((segments[1].start, segments[1].end), (12.0, 20.0))

    def test_per_chunk_failure_reports_stderr(self):
        settings = with_stt(stt_command=sys.executable + " -c \"import sys; sys.exit('모델 없음')\" {audio}")
        with self.assertRaises(stt.SttError) as caught:
            list(stt._run_per_chunk(settings, self.pieces, ""))
        self.assertIn("모델 없음", str(caught.exception))

    def test_per_chunk_timeout(self):
        settings = with_stt(stt_command=sys.executable + " -c \"import time; time.sleep(5)\" {audio}", stt_chunk_timeout_seconds=1)
        with self.assertRaises(stt.SttError) as caught:
            list(stt._run_per_chunk(settings, self.pieces, ""))
        self.assertIn("1초", str(caught.exception))

    def test_batch_runner_reads_json_lines(self):
        code = "import sys, json; [print(json.dumps({'file': f, 'text': 'T' + f[-7:], 'seconds': 50})) for f in sys.argv[1:]]"
        settings = with_stt(stt_command=sys.executable + " -c " + json.dumps(code) + " {audios}", stt_slow_factor=2.0)
        segments = list(stt._run_batch(settings, self.pieces, ""))
        self.assertEqual([s.text for s in segments], ["T001.wav", "T002.wav"])
        self.assertTrue(segments[0].slow)  # 10초 분량에 50초

    def test_batch_runner_timeout_between_lines(self):
        code = "import sys, json, time; print(json.dumps({'file': sys.argv[1], 'text': 'a'}), flush=True); time.sleep(5)"
        settings = with_stt(stt_command=sys.executable + " -c " + json.dumps(code) + " {audios}", stt_chunk_timeout_seconds=1)
        with self.assertRaises(stt.SttError) as caught:
            list(stt._run_batch(settings, self.pieces, ""))
        self.assertIn("1/2", str(caught.exception))

    def test_batch_runner_missing_results(self):
        code = "import sys, json; print(json.dumps({'file': sys.argv[1], 'text': 'a'}))"
        settings = with_stt(stt_command=sys.executable + " -c " + json.dumps(code) + " {audios}")
        with self.assertRaises(stt.SttError) as caught:
            list(stt._run_batch(settings, self.pieces, ""))
        self.assertIn("1/2", str(caught.exception))


class TranscriptTest(unittest.TestCase):
    def test_text_and_round_trip(self):
        transcript = stt.Transcript("a.mp3", 100.0, 80.0, [stt.Segment(0.0, 60.0, "첫 묶음", 3.0), stt.Segment(61.0, 80.0, "", 1.0)])
        self.assertEqual(transcript.text, "[00:00:00] 첫 묶음")
        again = stt.Transcript.from_dict(json.loads(json.dumps(transcript.to_dict())))
        self.assertEqual(again, transcript)


if __name__ == "__main__":
    unittest.main()
