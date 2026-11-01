"""Native verifier control-flow regression. No ROS, network or subprocesses."""
import copy
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout, redirect_stderr

spec = importlib.util.spec_from_file_location('native_diagnostic_verifier', Path(__file__).resolve().parents[1] / 'scripts/verify_ros2_diagnostics.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def complete_report():
    return {'rounds': [{'checks': {'new_input': True}, 'elapsed_sec': 30, 'rx_delta': 10, 'consume_delta': 9} for _ in range(2)],
            'cleanup': {'idle_gate_closed': True}, 'sse_error': None, 'sse_frames': 4}


class NativeVerifierTests(unittest.TestCase):
    def test_embedded_publisher_payload_is_complete_base64_jpeg(self):
        import base64
        payload = base64.b64decode(verifier.TINY_JPEG_B64, validate=True)
        self.assertTrue(payload.startswith(b'\xff\xd8'))
        self.assertTrue(payload.endswith(b'\xff\xd9'))
        self.assertGreater(len(payload), 150)

    def test_success_requires_all_rounds_nonempty_checks_and_positive_deltas(self):
        report = complete_report()
        self.assertTrue(verifier.report_passes(report, repeat=2, duration=30))
        mutations = [lambda value: value['rounds'].pop(),
            lambda value: value['rounds'][0].pop('checks'),
            lambda value: value['rounds'][0].update(checks={}),
            lambda value: value['rounds'][0].update(checks={'x': False}),
            lambda value: value['rounds'][0].update(rx_delta=0),
            lambda value: value['rounds'][0].update(consume_delta=0),
            lambda value: value['rounds'][0].update(elapsed_sec=29.99),
            lambda value: value.update(cleanup={}),
            lambda value: value.update(cleanup={'restore': False}),
            lambda value: value.update(sse_error='SSE_CLOSE_TIMEOUT'),
            lambda value: value.update(sse_frames=0)]
        for mutate in mutations:
            value = copy.deepcopy(report); mutate(value)
            self.assertFalse(verifier.report_passes(value, repeat=2, duration=30))

    def test_entry_starts_an_independent_process_for_every_round(self):
        clock = SimpleNamespace(now=0)
        def sleep(seconds): clock.now += seconds
        count = SimpleNamespace(value=0)
        def get(path):
            if path == 'plugins/yolo/ros2': return {'ros2': {'bindings': {}}}
            count.value += 1
            return {'received': [{'port_id': 'image', 'rx_received': count.value, 'rx_consumed': count.value}]}
        api = Mock(); api.get.side_effect = get
        processes = [Mock(pid=111), Mock(pid=222)]
        for process in processes: process.poll.return_value = None; process.wait.return_value = 0
        sse = Mock(); sse.error = None; sse.snapshot.return_value = [{}]
        with tempfile.TemporaryDirectory() as root:
            recorder = Mock(output=Path(root))
            args = SimpleNamespace(entry='/test/run-yolo', duration=30, repeat=2)
            with patch.object(verifier, '_initial_state', return_value=({'config': {'ros2': {}}}, {'yolo': False})), \
                 patch.object(verifier, 'SseReader', return_value=sse), \
                 patch.object(verifier, '_select_mode'), patch.object(verifier, '_idle_gate_closed', return_value=True), \
                 patch.object(verifier, '_restore', return_value={'restored': True}), patch.object(verifier, '_save_sse'), \
                 patch.object(verifier, '_stop_entry', return_value=True), \
                 patch.object(verifier.time, 'monotonic', side_effect=lambda: clock.now), \
                 patch.object(verifier.time, 'sleep', side_effect=sleep), \
                 patch('subprocess.Popen', side_effect=processes) as popen:
                result = verifier.run_entry(api, args, recorder)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual([row['entry_pid'] for row in result['rounds']], [111, 222])
        self.assertTrue(all(call.kwargs['start_new_session'] for call in popen.call_args_list))
        self.assertTrue(verifier.report_passes(result, repeat=2, duration=30))

    def test_dry_run_never_reads_token_or_imports_ros(self):
        import sys
        with tempfile.TemporaryDirectory() as root, patch.object(verifier, 'load_token', side_effect=AssertionError('secret read')), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(['--dry-run', '--output', root]), 0)
            self.assertTrue((Path(root) / 'plan.json').exists())
            self.assertNotIn('rclpy', sys.modules)

    def test_non_linux_is_not_run_before_secret_or_network(self):
        with tempfile.TemporaryDirectory() as root, patch.object(verifier.sys, 'platform', 'win32'), \
                patch.object(verifier, 'load_token', side_effect=AssertionError('secret read')), \
                redirect_stderr(io.StringIO()):
            token_path = Path(root) / 'dummy.token'
            token_path.write_text('test-only', encoding='utf-8')
            self.assertEqual(verifier.main(['--token-file', str(token_path), '--output', root]), 3)

    def test_endpoints_are_the_counter_source(self):
        self.assertEqual(verifier.find_port({'received': [{'port_id': 'image', 'rx_received': 7}]}, 'image')['rx_received'], 7)
