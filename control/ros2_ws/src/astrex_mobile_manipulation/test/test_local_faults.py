"""Check opt-in fault boundaries without starting ROS or signaling processes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from astrex_mobile_manipulation.test_faults import (
    FAULT_SCHEMA, LocalFaultControl, OwnedGateway, load_fault_config,
    write_fault_request,
)


class LocalFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.value = dict(schema=FAULT_SCHEMA, test_id='unit_test_session_123',
                          profile_hash='profile', control_file=str(self.root/'control.json'),
                          expires_monotonic=200.)
        self.config.write_text(json.dumps(self.value))
        self.config.chmod(0o600)
        self.key = ('command', 'ex', 1, 1)

    def control(self):
        return LocalFaultControl(str(self.config), 'profile', now=100.)

    def request(self, control, **changes):
        value = dict(schema=FAULT_SCHEMA, test_id=self.value['test_id'], seq=1,
                     isaac_session='isaac', op='raw_feedback_pause', until_monotonic=101.,
                     command_id='command', ex_session='ex', goal_revision=1, execution_epoch=1)
        value.update(changes)
        write_fault_request(control.settings, value)

    def test_default_off_does_not_read_a_control_file(self):
        control = LocalFaultControl(None, 'profile', now=100.)
        self.assertFalse(control.poll(100., 'isaac', self.key))
        self.assertEqual(control.status(False), {'enabled': False})

    def test_pause_expires_and_same_sequence_cannot_extend_it(self):
        control = self.control()
        self.request(control)
        self.assertTrue(control.poll(100., 'isaac', self.key))
        self.request(control, until_monotonic=102.)
        self.assertFalse(control.poll(101.1, 'isaac', self.key))

    def test_old_command_or_simulator_cannot_pause_current_feedback(self):
        for changes in ({'execution_epoch': 2}, {'isaac_session': 'old'}):
            with self.subTest(changes=changes):
                control = self.control()
                self.request(control, **changes)
                self.assertFalse(control.poll(100., 'isaac', self.key))
                self.assertIn('SESSION_OR_COMMAND', control.error)

    def test_changed_execution_key_immediately_stops_suppression(self):
        control = self.control()
        self.request(control)
        self.assertTrue(control.poll(100., 'isaac', self.key))
        self.assertFalse(control.poll(100.01, 'isaac', ('new', 'ex', 1, 2)))

    def test_config_permissions_profile_and_expiry_are_enforced(self):
        with self.assertRaises(ValueError):
            load_fault_config(self.config, 'other', now=100.)
        with self.assertRaises(ValueError):
            load_fault_config(self.config, 'profile', now=201.)
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            self.control()

    def test_unknown_operation_and_long_pause_are_rejected(self):
        for changes in ({'op': 'disable_guard'}, {'until_monotonic': 103.}):
            with self.subTest(changes=changes):
                control = self.control()
                self.request(control, **changes)
                self.assertFalse(control.poll(100., 'isaac', self.key))

    def test_owned_gateway_crash_needs_live_child_and_task_binding(self):
        owner = OwnedGateway(self.root/'profile.json', self.root/'evidence')
        with self.assertRaises(RuntimeError):
            owner.crash('command', 'session')
        child = Mock(pid=123)
        child.poll.return_value = None
        owner.process = child
        with self.assertRaises(RuntimeError):
            owner.crash('', 'session')
        child.send_signal.assert_not_called()
        owner.crash('command', 'session')
        import signal
        child.send_signal.assert_called_once_with(signal.SIGKILL)
        child.wait.return_value = -9
        self.assertEqual(owner.confirm_crashed(), -9)


if __name__ == '__main__':
    unittest.main()
