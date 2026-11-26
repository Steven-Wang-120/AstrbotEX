"""Ordered primitive binding on EX's existing backend and control workers.

No ROS executor, hardware writer, physical-success evaluator or replay queue.
The ingress receipt log remains the durable duplicate-request authority.
"""
from __future__ import annotations

import copy
import uuid


class PrimitivePipeline:
    def __init__(self, service, provider):
        self.service, self.provider = service, provider
        self.catalog = provider.catalog
        self.batch = None
        self.epoch = None
        self.backend = None
        self.index = 0
        self.phase = 'idle'
        self.reason = ''
        self.steps = []
        self.goal_id = None
        self.goal_revision = None
        self.owned_goals = set()
        self.binding = None
        self.instruction_received_ns = None

    @property
    def active(self):
        return self.phase in {'binding', 'ready', 'submitting', 'running', 'next'}

    def begin(self, batch):
        # Called with the service condition held, after ordinary ingress checks.
        self.batch, self.epoch, self.backend = batch, self.service.config_revision, self.service.backend
        self.index, self.phase, self.reason = 0, 'binding', ''
        self.instruction_received_ns = self.service._clock()
        self.steps, self.binding = [], None
        self.goal_id, self.goal_revision = None, None
        self.owned_goals.clear()

    def owns(self, goal):
        return goal is not None and goal.payload()['goal_id'] in self.owned_goals

    def cancel(self, reason):
        if self.active:
            self.phase, self.reason = 'canceled', reason
            for row in self.service._rows:
                if json_command(row).get('goal_id') == self.goal_id:
                    if self.steps:
                        self.steps[-1].update(command_id=row.command_id, state=row.status,
                                              result=copy.deepcopy(row.details))
                    if row.status in {'rejected', 'failed', 'unknown', 'timed_out'}:
                        self.phase = row.status
            self.service._primitive_request = None

    def _current(self, batch_id, index):
        return (self.active and self.batch is not None and self.batch.batch_id == batch_id
                and self.index == index and self.epoch == self.service.config_revision
                and self.backend is self.service.backend and not self.service._closed)

    def bind_round(self, backend, batch_id, index):
        # This method runs only on DecisionService's existing backend worker.
        with self.service._condition:
            if not self._current(batch_id, index):
                return None
            batch = self.batch
            segment = batch.segments[index]
            started = self.service._clock()
        try:
            structured = segment.structured_action
            if structured is not None:
                bound = self.provider.bind_structured(structured)
            else:
                if hasattr(backend, "require_image"):
                    observations, image, jpeg = self.provider.structured_context(require_image=backend.require_image)
                else:
                    observations, image, jpeg = self.provider.context()
                bound = backend.bind_primitive(segment.text, observations, image, jpeg,
                                               provider=self.provider, batch=batch.to_wire())
        except Exception as exc:
            with self.service._condition:
                if self._current(batch_id, index):
                    self.phase = 'rejected' if isinstance(exc, ValueError) else 'failed'
                    self.reason = getattr(exc, 'code', str(exc)[:128] if isinstance(exc, ValueError) else type(exc).__name__)
                    self.steps.append({'segment_id': segment.segment_id, 'ordinal': segment.ordinal,
                                       'state': self.phase, 'reason': self.reason, 'binding_started_ns': started,
                                       'binding_completed_ns': self.service._clock()})
            raise
        with self.service._condition:
            if not self._current(batch_id, index):
                return None
            self.binding = bound
            self.phase = 'ready'
            self.steps.append({'segment_id': segment.segment_id, 'ordinal': segment.ordinal,
                               'state': 'bound', 'binding': copy.deepcopy(bound), 'binding_started_ns': started,
                               'binding_completed_ns': self.service._clock()})
            self.service._condition.notify_all()
        return bound

    def tick(self):
        # Only the control worker submits Goals; HTTP callbacks only fill fields.
        service = self.service
        with service._condition:
            if not self.active:
                return
            if self.epoch != service.config_revision or self.backend is not service.backend:
                self.cancel('configuration_changed')
                return
            if service.goals.phase == 'blocked':
                self.phase, self.reason = 'unknown', service.goals.reason
                return
            if self.phase == 'running':
                for row in service._rows:
                    command = json_command(row)
                    if command.get('goal_id') != self.goal_id:
                        continue
                    self.steps[-1].update(command_id=row.command_id, state=str(row.status),
                                          result=copy.deepcopy(row.details))
                    if row.status in {'rejected', 'failed', 'unknown', 'timed_out', 'canceled'}:
                        self.phase, self.reason = str(row.status), self.provider.failure_prefix + str(row.status)
                        return
                return  # Existing completion hook retires the Goal before next.
            if self.phase == 'next':
                if (service._instruction_running is None and service._backend_input is None
                        and service._backend_result is None and service._live_snapshot is None
                        and service._primitive_request is None and service._primitive_running is None):
                    service._primitive_request = (self.batch.batch_id, self.index)
                    self.phase = 'binding'
                    service._condition.notify_all()
                return
            if self.phase != 'ready':
                return
            if self.binding['state'] != 'ready':
                self.phase = 'needs_input'
                self.reason = self.binding['reason'] or 'missing_fields'
                self.steps[-1]['state'] = self.phase
                return
            if service.mode != 'execute' or service.actions.control_mode != 'decision':
                self.phase, self.reason = 'bound', 'execution_disabled'
                return
            batch_id, index = self.batch.batch_id, self.index
            binding = self.binding
            segment = self.batch.segments[index]
            self.phase = 'submitting'
        # Plugin workers may publish observations while holding their own lock.
        # Resolve the target without the service lock, then recheck the batch.
        try:
            params = self.provider.execution_params(binding)
        except Exception as exc:
            with service._condition:
                if self._current(batch_id, index) and self.phase == 'submitting':
                    self.phase, self.reason = 'rejected', getattr(exc, 'code', str(exc))
                    self.steps[-1].update(state='rejected', reason=self.reason)
            return
        with service._condition:
            if not self._current(batch_id, index) or self.phase != 'submitting':
                return
            try:
                self.goal_id = 'primitive-' + uuid.uuid4().hex
                self.owned_goals.add(self.goal_id)
                action = self.binding['action_id']
                receipt = service._submit_goal({
                    'schema_version': 1, 'request_id': 'primitive-' + uuid.uuid4().hex,
                    'ex_session': self.batch.ex_session, 'task_id': self.batch.plan_id,
                    'step_id': segment.segment_id, 'goal_id': self.goal_id,
                    'goal_text_en': 'Execute this one bound primitive once: ' + segment.text,
                    'allowed_actions': [action], 'parameters': {action: params},
                    'completion': {'required_success_actions': [action]},
                    'lease_ms': 60000, 'expected_revision': service.goals.revision,
                }, pipeline=self)
                self.goal_revision = receipt['revision']
                self.steps[-1].update(state='submitted', goal_id=self.goal_id,
                                      goal_revision=self.goal_revision, execution_params=params,
                                      receipt=receipt)
                self.phase = 'running'
            except Exception as exc:
                self.phase, self.reason = 'rejected', getattr(exc, 'code', str(exc))
                self.steps[-1].update(state='rejected', reason=self.reason)

    def complete(self, goal, epoch, summary):
        """Consume the existing lower-result completion; no success evaluator."""
        service = self.service
        with service._condition:
            if not self.owns(goal) or not service.goals.finish_stopped(goal, epoch):
                return False
            commands = []
            for row in service._rows:
                if json_command(row).get('goal_id') == goal.payload()['goal_id']:
                    commands.append({'command_id': row.command_id, 'status': str(row.status),
                                     'details': copy.deepcopy(row.details)})
            step = next((s for s in self.steps if s.get('goal_id') == goal.payload()['goal_id']), None)
            if step is not None:
                step.update(state=summary['status'], commands=commands, completed_ns=service._clock(),
                            result_scope=self.provider.result_scope, task_success_verified=False)
            if not self.active:
                return True
            if summary['status'] != 'succeeded':
                self.phase, self.reason = summary['status'], summary.get('reason_code', '')
            elif self.index + 1 == len(self.batch.segments):
                self.phase, self.reason = 'completed', ''
            else:
                self.index += 1
                self.binding = None
                self.phase = 'next'
            service._condition.notify_all()
            return True

    def status(self):
        with self.service._lock:
            return {'phase': self.phase, 'reason': self.reason,
                    'batch_id': self.batch.batch_id if self.batch else None,
                    'instruction_received_ns': self.instruction_received_ns,
                    'plan_id': self.batch.plan_id if self.batch else None,
                    **self.provider.status_fields, 'task_success_verified': False,
                    'steps': copy.deepcopy(self.steps)}


def json_command(row):
    import json
    return json.loads(row.canonical_command)['command']
