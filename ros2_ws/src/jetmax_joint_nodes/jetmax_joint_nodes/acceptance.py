"""ROS-only, operator-observed commissioning. Never opens a hardware device."""
import argparse
import collections
import fcntl
import json
import math
from pathlib import Path
import queue
import statistics
import threading
import time
import uuid

ARMS = ('arm_base', 'arm_shoulder', 'arm_elbow')
JOINTS = ARMS + ('gripper',)


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def summarize(samples, seconds, invalid=0):
    if len(samples) < 2:
        return {'passed': False, 'reason': 'insufficient distinct position replies'}
    times = [s['receipt_ns'] / 1e9 for s in samples]
    values = [s['position_raw'] for s in samples]
    intervals = [b - a for a, b in zip(times, times[1:])]
    query = sorted(s['query_duration_ms'] for s in samples)
    tail = [s['position_raw'] for s in samples if s['receipt_ns'] >= samples[-1]['receipt_ns'] - 1_000_000_000]
    span = max(values) - min(values)
    gap = max(intervals)
    return dict(passed=span <= 3 and gap < .25 and invalid == 0,
                p0=int(statistics.median(tail)), distinct_replies=len(samples),
                observed_reply_hz=(len(samples)-1)/(times[-1]-times[0]),
                observation_seconds=seconds, raw_span=span, max_receipt_interval_ms=gap*1000,
                invalid_status_count=invalid, query_ms_median=statistics.median(query),
                query_ms_p95=query[math.ceil(.95*len(query))-1],
                sensor_sample_age='unknown', query_duration_is_sensor_age=False)


def hold_metrics(samples):
    if len(samples) < 3:
        return dict(passed=False, reason='insufficient hold samples')
    first, last = samples[0]['local_s'], samples[-1]['local_s']
    values = [s['position_raw'] for s in samples]
    head = [s['position_raw'] for s in samples if s['local_s'] <= first + .5]
    tail = [s['position_raw'] for s in samples if s['local_s'] >= last - .5]
    span = max(values)-min(values)
    drift = statistics.median(tail)-statistics.median(head)
    return dict(passed=last-first >= 2.7 and span <= 5 and abs(drift) <= 2,
                raw_span=span, endpoint_median_delta_raw=drift,
                observed_seconds=last-first, physical_stop_confirmed=False)


def main(args=None):
    import yaml
    import rclpy
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from rosidl_runtime_py.convert import message_to_ordereddict
    from jetmax_joint_interfaces.action import MoveServoRaw, SetGripperAngle
    from jetmax_joint_interfaces.msg import JointState
    from jetmax_joint_interfaces.srv import StopJoint

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('baseline', 'reach', 'stop_wait', 'cancel', 'timeout', 'gripper'), required=True)
    parser.add_argument('--joint', choices=ARMS, default='arm_base')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--config')
    parser.add_argument('--expected-backend', choices=('real', 'fake'), default='real')
    parser.add_argument('--fake-no-observer', action='store_true')
    options = parser.parse_args(args)
    if options.fake_no_observer and options.expected_backend != 'fake':
        parser.error('Observer bypass is allowed only with the fake backend')
    folder = Path(options.output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    log = (folder / 'events.jsonl').open('a', buffering=1)
    fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run_id = uuid.uuid4().hex
    def record(event, **data):
        log.write(json.dumps(clean(dict(event=event, run_id=run_id, monotonic_s=time.monotonic(),
                                       wall_ns=time.time_ns(), **data)), ensure_ascii=False, allow_nan=False)+'\n')
    rclpy.init(args=[])
    node = Node('jetmax_acceptance_' + run_id[:8])
    states, seen = {}, {}
    samples = {n: [] for n in ARMS}
    invalid = collections.Counter()
    watching = False
    origin = {}
    selected = 'gripper' if options.case == 'gripper' else options.joint
    clients, stops = {}, {}
    attempted = False
    def receive(msg):
        data = dict(message_to_ordereddict(msg))
        now = time.monotonic()
        data['local_s'] = now
        data['receipt_ns'] = msg.received_at.sec*1_000_000_000+msg.received_at.nanosec
        states[msg.joint] = data
        record('state', state=data)
        if msg.joint in ARMS:
            if watching and not msg.feedback_valid:
                invalid[msg.joint] += 1
            if msg.feedback_valid and data['receipt_ns'] != seen.get(msg.joint):
                seen[msg.joint] = data['receipt_ns']
                samples[msg.joint].append(data)
    subscriptions = [node.create_subscription(JointState, '/jetmax_hw/'+n+'/state', receive, 20) for n in JOINTS]
    for name in JOINTS:
        cls = SetGripperAngle if name == 'gripper' else MoveServoRaw
        suffix = 'set_angle' if name == 'gripper' else 'move'
        clients[name] = ActionClient(node, cls, '/jetmax_hw/'+name+'/'+suffix)
        stops[name] = node.create_client(StopJoint, '/jetmax_hw/'+name+'/stop')

    def check():
        for n in JOINTS:
            if n not in states or time.monotonic()-states[n]['local_s'] > .25:
                raise RuntimeError('State stream missing/stale: '+n)
            if states[n]['backend'] != options.expected_backend:
                raise RuntimeError('Unexpected hardware backend: '+n)
        for n in ARMS:
            if not states[n]['feedback_valid'] or states[n]['receipt_age_ms'] >= 250:
                raise RuntimeError('Invalid/stale position: '+n)
            if origin and n != selected and abs(states[n]['position_raw']-origin[n]) > 5:
                raise RuntimeError('Unselected joint displacement: '+n)

    def spin(seconds, guarded=True):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=min(.02, end-time.monotonic()))
            if guarded:
                check()

    def wait(future, timeout=10, guarded=True):
        end = time.monotonic()+timeout
        while not future.done() and time.monotonic() < end:
            spin(.02, guarded)
        if not future.done():
            raise TimeoutError('ROS response deadline; no motion retry')
        return future.result()

    def observe(message):
        if options.fake_no_observer:
            record('operator_observation', observation='not_applicable_fake', prompt=message)
            return
        responses = queue.Queue()
        def ask():
            try:
                responses.put(input(message+'\n确认看到预期效果输入 yes；其他输入停止：').strip().lower())
            except (EOFError, KeyboardInterrupt):
                responses.put('no')
        threading.Thread(target=ask, daemon=True).start()
        deadline = time.monotonic()+60
        while responses.empty() and time.monotonic() < deadline:
            spin(.05)
        answer = responses.get_nowait() if not responses.empty() else 'timeout'
        record('operator_observation', prompt=message, answer=answer)
        if answer != 'yes':
            raise RuntimeError('Operator did not confirm expected physical behavior')

    def send(name, target, duration=2000):
        nonlocal attempted
        check()
        if not states[name]['control_enabled'] or not states[name]['motion_configured']:
            raise RuntimeError('Motion not enabled/configured')
        if not clients[name].wait_for_server(timeout_sec=2):
            raise RuntimeError('Action server unavailable')
        goal = SetGripperAngle.Goal(target_deg=float(target)) if name == 'gripper' else MoveServoRaw.Goal(target_raw=int(target), duration_ms=duration)
        record('goal_request', joint=name, goal=dict(message_to_ordereddict(goal)))
        attempted = True
        handle = wait(clients[name].send_goal_async(goal))
        record('goal_response', joint=name, accepted=handle.accepted, goal_id=bytes(handle.goal_id.uuid).hex())
        if not handle.accepted:
            raise RuntimeError('Goal rejected; no retry')
        return handle, handle.get_result_async()

    def result(future, outcome, status):
        response = wait(future)
        record('action_result', status=response.status, result=dict(message_to_ordereddict(response.result)))
        if response.status != status or response.result.outcome != outcome:
            raise RuntimeError('Unexpected action result: '+response.result.outcome)
        return response.result

    def wait_written(handle, future):
        key = bytes(handle.goal_id.uuid).hex()
        end = time.monotonic()+2
        while time.monotonic() < end:
            spin(.02)
            state = states[selected]
            if state['goal_id'] == key and state['command_written']:
                return
            if future.done():
                break
        raise RuntimeError('No observed command write before result')

    def hold():
        spin(.5)
        begin = time.monotonic()
        spin(3)
        metrics = hold_metrics([s for s in samples[selected] if s['local_s'] >= begin])
        record('hold_window', joint=selected, **metrics)
        if not metrics['passed']:
            raise RuntimeError('Hold window exceeded displacement limits')
        observe('请确认 '+selected+' 已停止，并在等待期间没有继续运动。')

    def stop(name, guarded=True):
        record('stop_request', joint=name)
        if not stops[name].wait_for_service(timeout_sec=1):
            raise RuntimeError('Stop service unavailable')
        response = wait(stops[name].call_async(StopJoint.Request()), 2, guarded)
        record('stop_response', joint=name, response=dict(message_to_ordereddict(response)))
        if not response.accepted:
            raise RuntimeError('Stop request rejected')

    passed = False
    try:
        record('case_start', case=options.case, joint=selected, backend=options.expected_backend)
        spin(2, guarded=False)
        check()
        watching = True
        if options.case == 'baseline':
            if any(states[n]['control_enabled'] for n in JOINTS):
                raise RuntimeError('Baseline requires read-only nodes')
            samples = {n: [] for n in ARMS}
            invalid.clear()
            # Include the latest receipt so a cached first second cannot disappear.
            for n in ARMS:
                samples[n].append(states[n].copy())
            spin(60)
            summary = {n: summarize(samples[n], 60, invalid[n]) for n in ARMS}
            passed = all(d['passed'] for d in summary.values())
            (folder/'baseline.json').write_text(json.dumps(clean(dict(passed=passed, joints=summary)), ensure_ascii=False, indent=2)+'\n')
            record('baseline_result', passed=passed, joints=summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        else:
            if not options.config:
                raise RuntimeError('Acceptance config required')
            cfg = yaml.safe_load(Path(options.config).read_text())
            record('configuration', config=cfg)
            if cfg['profile_kind'] != options.expected_backend:
                raise RuntimeError('Configuration/backend mismatch')
            cap = cfg.get('goal_timeout_cap_s')
            if (options.case == 'timeout' and cap not in (1.0, 2.0)) or (options.case != 'timeout' and cap is not None):
                raise RuntimeError('Timeout case requires the reviewed 1 or 2 second cap; other cases require null')
            origin = {n: states[n]['position_raw'] for n in ARMS}
            if selected == 'gripper':
                message = '即将依次发送已确认的 PWM2 45°（开）、75°（闭）命令。先动作，再询问实际效果。'
                print(message, flush=True)
                record('motion_announcement', message=message)
                if not options.fake_no_observer:
                    spin(3)
                for value in (45, 75):
                    _, future = send(selected, value)
                    res = result(future, 'command_written', 4)
                    if res.final_state.measured_angle_valid or res.final_state.feedback_valid:
                        raise RuntimeError('Unexpected gripper measured feedback')
                    spin(1)
                    observe('请确认夹爪在命令 '+str(value)+'° 后按预期'+('打开' if value == 45 else '闭合')+'。')
            else:
                lo, hi = cfg['joints'][selected]['limits']
                if hi-lo != 40 or not lo-5 <= origin[selected] <= hi+5:
                    raise RuntimeError('Expected reviewed 40-raw local envelope')
                target = hi if origin[selected] <= (lo+hi)/2 else lo
                message = '3 秒后执行已确认的 '+selected+'：'+str(origin[selected])+' → '+str(target)+' 原始刻度。先动作，再询问实际效果。'
                print(message, flush=True)
                record('motion_announcement', message=message)
                if not options.fake_no_observer:
                    spin(3)
                if options.case == 'reach':
                    for value in (target, lo):
                        _, future = send(selected, value)
                        result(future, 'target_observed', 4)
                        observe('请确认 '+selected+' 运动方向与目标 '+str(value)+' 一致且已经到位。')
                else:
                    begin = time.monotonic()
                    handle, future = send(selected, target, 4000)
                    wait_written(handle, future)
                    if options.case == 'timeout':
                        res = result(future, 'timeout', 6)
                        movement = max((abs(s['position_raw']-origin[selected]) for s in samples[selected] if s['local_s'] >= begin), default=0)
                        record('timeout_motion_evidence', joint=selected, delta_raw=movement)
                        if not res.final_state.command_written or movement <= 3:
                            raise RuntimeError('Timeout without clearly observed motion; not an in-motion pass')
                    else:
                        spin(1)
                        if future.done():
                            raise RuntimeError('Goal completed before interruption')
                        if options.case == 'cancel':
                            response = wait(handle.cancel_goal_async())
                            record('cancel_response', response=dict(message_to_ordereddict(response)))
                            if not response.goals_canceling:
                                raise RuntimeError('Cancellation not accepted')
                            res = result(future, 'cancelled', 5)
                        else:
                            stop(selected)
                            res = result(future, 'cancelled', 6)
                    if res.final_state.stop_state != 'stop_written_unconfirmed' or res.final_state.physical_stop_confirmed:
                        raise RuntimeError('Unexpected STOP evidence')
                    hold()
                    if options.case == 'stop_wait':
                        # New Goal only after measured hold and operator observation.
                        for value in (target, lo):
                            _, future = send(selected, value)
                            result(future, 'target_observed', 4)
                            observe('请确认新 Goal 到 '+str(value)+' 完成，旧 Goal 没有自行恢复。')
            passed = True
    except BaseException as exc:
        record('case_error', error=type(exc).__name__+': '+str(exc))
        print('验收停止：'+str(exc), flush=True)
        if attempted:
            try:
                stop(selected, guarded=False)
            except Exception as error:
                record('cleanup_stop_failed', error=str(error))
                print('停止未确认，请现场检查并在必要时切断动力。', flush=True)
    finally:
        record('case_end', passed=passed, case=options.case, joint=selected)
        for client in clients.values():
            client.destroy()
        node.destroy_node()
        rclpy.shutdown()
        log.close()
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
