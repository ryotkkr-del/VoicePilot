"""v0.23.4: numeric-only local diagnostics without real mic or desktop IO."""
from __future__ import annotations

from contextlib import nullcontext
import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import RunOutcome
from app.main import VoicePilotApp, speech_model_idle_seconds
from app.performance import OperationProfiler, process_working_set_bytes


class PerformanceSampleTests(unittest.TestCase):
    def test_sections_and_resources_are_numeric_without_user_content(self):
        now = [10.0]
        cpu = [5.0]
        mem = iter([100 * 1024 * 1024, 125 * 1024 * 1024])
        sample = OperationProfiler(
            started_at=9.0,
            clock=lambda: now[0], cpu_clock=lambda: cpu[0],
            memory_reader=lambda: next(mem),
        )
        with sample.phase("asr"):
            now[0] += 0.5
        with sample.phase("execute"):
            now[0] += 3.5
        now[0] += 1.0
        cpu[0] += 2.1
        summary = sample.summary(voice=True, status="completed")
        self.assertIn("合計 6.00秒", summary)
        self.assertIn("認識 0.50秒", summary)
        self.assertIn("操作処理 3.50秒", summary)
        self.assertIn("CPU処理時間 2.10秒", summary)
        self.assertIn("100→125MB", summary)
        self.assertNotIn("音声のテストです", summary)

    def test_unknown_status_and_unavailable_memory_fail_closed(self):
        sample = OperationProfiler(
            clock=lambda: 42.0,
            cpu_clock=lambda: 1.0,
            memory_reader=lambda: None,
        )
        result = sample.summary(voice=False, status="secret prompt or URL")
        self.assertIn("unknown", result)
        self.assertIn("メモリ計測不可", result)
        self.assertNotIn("secret", result)
        self.assertNotIn("認識", result)

    def test_failed_stage_still_records_time(self):
        now = [0.0]
        sample = OperationProfiler(
            clock=lambda: now[0], cpu_clock=lambda: 0,
            memory_reader=lambda: None,
        )
        with self.assertRaises(RuntimeError):
            with sample.phase("execute"):
                now[0] = 3.0
                raise RuntimeError("test failure")
        self.assertIn("操作処理 3.00秒", sample.summary(voice=False, status="error"))

    def test_invalid_phase_is_rejected(self):
        sample = OperationProfiler(memory_reader=lambda: None)
        with self.assertRaises(ValueError):
            with sample.phase("private file contents"):
                pass

    def test_windows_sample_is_optional_and_non_windows_returns_none(self):
        if __import__("os").name == "nt":
            value = process_working_set_bytes()
            self.assertTrue(value is None or isinstance(value, int) and value > 0)
        else:
            self.assertIsNone(process_working_set_bytes())


class ModelIdlePolicyTests(unittest.TestCase):
    def test_medium_and_large_unload_sooner_but_small_is_unchanged(self):
        self.assertEqual(speech_model_idle_seconds("base"), 75)
        self.assertEqual(speech_model_idle_seconds("small"), 75)
        self.assertEqual(speech_model_idle_seconds("medium"), 45)
        self.assertEqual(speech_model_idle_seconds("large-v3-turbo"), 30)


class AppPerformanceWiringTests(unittest.TestCase):
    def make_app(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.log = Mock()
        app.mark_idle = Mock()
        app.stop_event = threading.Event()
        app.dictation_target = None
        app._run_agent = Mock(return_value=RunOutcome("completed", "done"))
        return app

    def test_text_operation_diagnostics_are_opt_in(self):
        app = self.make_app()
        with patch("app.main.OperationProfiler") as profiler_type:
            app.run_text_task("テスト指示", {"measure_performance": False})
        profiler_type.assert_not_called()
        app._run_agent.assert_called_once()
        self.assertEqual(app.log.call_count, 0)
        app.mark_idle.assert_called_once_with("completed", None)

    def test_enabled_text_measurement_reports_only_summary(self):
        app = self.make_app()
        with patch("app.main.OperationProfiler") as profiler_type:
            sample = profiler_type.return_value
            sample.phase.return_value = nullcontext()
            sample.summary.return_value = "NUMERIC_METRIC_ONLY"
            app.run_text_task("PRIVATE_REQUEST", {"measure_performance": True})
        sample.phase.assert_called_once_with("execute")
        sample.summary.assert_called_once_with(voice=False, status="completed")
        app.log.assert_called_once_with("NUMERIC_METRIC_ONLY")
        app.mark_idle.assert_called_once_with("completed", None)

    def test_voice_timing_splits_asr_and_execution_without_audio_storage(self):
        app = self.make_app()
        app.recognizer = Mock(model_name="small")
        app.recognizer.transcribe.return_value = "テスト指示"
        app.show_transcript = Mock()
        app._speak_result = Mock()
        with patch("app.main.OperationProfiler") as profiler_type:
            sample = profiler_type.return_value
            sample.phase.return_value = nullcontext()
            sample.summary.return_value = "NUMERIC_METRIC_ONLY"
            app.transcribe_then_execute(
                samples=Mock(), runtime={"measure_performance": True, "whisper_model": "small"},
                record_finished_at=100.0,
            )
        profiler_type.assert_called_once_with(started_at=100.0)
        self.assertEqual([c.args[0] for c in sample.phase.call_args_list], ["asr", "execute"])
        sample.summary.assert_called_once_with(voice=True, status="completed")
        app.mark_idle.assert_called_once_with("completed", None)


if __name__ == "__main__":
    unittest.main()
