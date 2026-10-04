#!/usr/bin/env python3
"""One Isaac entry point for sensor-only and protected robot experiments."""
import argparse
import importlib
import hashlib
import tomllib
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sim"))
from mobile_manipulation.spec import PROFILES, config_hash, effective_config, load_spec

def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("sensor_only", "control"), default="sensor_only")
    parser.add_argument("--sensor-profile", choices=tuple(PROFILES), default="RGB_LIDAR_2D")
    parser.add_argument("--config")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--seed", type=int, default=101)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--duration", type=float, default=0, help="Simulation seconds; 0 runs until stopped")
    parser.add_argument("--robot-usd")
    parser.add_argument("--robot-prim", default="/World/Robot")
    parser.add_argument("--control-hook", help="module:function with install(world, config) signature")
    parser.add_argument("--hook-python-path", help="Pure project Python directory, never ROS site-packages")
    args = parser.parse_args(argv)
    if args.mode == "sensor_only" and (args.control_hook or args.robot_usd):
        parser.error("sensor_only cannot load a robot or execution hook")
    if args.mode == "control" and not (args.robot_usd and args.control_hook):
        parser.error("control requires both a robot USD and final execution gate hook")
    if args.duration < 0:
        parser.error("duration must not be negative")
    return args

def main(argv=None):
    args = arguments(argv)
    spec, raw = load_spec(args.config)
    sensor_rendering=raw.get("sensor_rendering",True)
    if type(sensor_rendering) is not bool:
        raise ValueError("sensor_rendering must be boolean")
    if args.mode=="sensor_only" and not sensor_rendering:
        raise ValueError("sensor_only requires active sensors")
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    effective = effective_config(spec, args.sensor_profile, args.seed, args.mode)
    effective.update({"robot_usd": args.robot_usd, "robot_prim": args.robot_prim,
                      "scene": raw.get("scene", {}), "robot": raw.get("robot", {}),
                      "execution": raw.get("execution", {})})
    effective["sensor_rendering"] = sensor_rendering
    render_settings = {}
    preset = raw.get("scene", {}).get("rendering_preset")
    if preset is not None:
        if preset != "isaaclab_performance_effects":
            raise ValueError("Unsupported rendering preset")
        preset_path = Path(os.environ["ASTREX_ISAAC_LAB_ROOT"]) / "apps/rendering_modes/performance.kit"
        preset_bytes = preset_path.read_bytes()
        def flatten_settings(value, prefix=""):
            for key, item in value.items():
                path = prefix + "/" + key
                if isinstance(item, dict):
                    yield from flatten_settings(item, path)
                else:
                    yield path, item
        official_settings = dict(flatten_settings(tomllib.loads(preset_bytes.decode())))
        # Keep shadows, brightness, antialiasing and sensor sampling unchanged.
        effect_keys = ("/rtx/translucency/enabled", "/rtx/reflections/enabled",
                       "/rtx/indirectDiffuse/enabled", "/rtx/ambientOcclusion/enabled")
        render_settings = {key:official_settings[key] for key in effect_keys}
        effective["rendering"] = {"preset":preset,"source_path":str(preset_path),
            "source_sha256":hashlib.sha256(preset_bytes).hexdigest(),"settings":render_settings}
    if not sensor_rendering:
        effective.update({"sensor_profile_id":None,"requested_sensor_profile_id":args.sensor_profile,
                          "lidar":None,"input_source":"ORACLE_CONTROL_ONLY"})
    effective["config_hash"] = config_hash({k:v for k,v in effective.items() if k != "config_hash"})
    effective.update({"pid": os.getpid(), "started_wall_ns": time.time_ns()})
    (output / "simulation_config.json").write_text(json.dumps(effective, indent=2) + "\n")
    from isaacsim import SimulationApp
    experience = str(Path(os.environ["ASTREX_ISAAC_LAB_ROOT"]) / os.environ["ASTREX_ISAAC_EXPERIENCE"])
    app = SimulationApp({"headless": args.headless, "width": 1280, "height": 800,
                         "renderer": "RayTracedLighting", "anti_aliasing": 2,
                         "sync_loads": True,
                         "enable_motion_bvh": bool(raw.get("robot", {}).get("sensor_parent_prim"))}, experience=experience)
    hook = node = publisher = None
    callback_seconds = 0.
    callback_failure = None
    step_timings = []
    stopped = False
    def request_stop(signum, frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        from isaacsim.core.utils.extensions import enable_extension
        enable_extension("isaacsim.ros2.bridge")
        enable_extension("isaacsim.sensors.rtx")
        enable_extension("isaacsim.sensors.camera")
        app.update()
        import rclpy
        from isaacsim.core.api import World
        from mobile_manipulation.scene import create_scene, attach_sensors, ScenePublisher
        world = World(stage_units_in_meters=1.0, physics_dt=1.0/120, rendering_dt=1.0/spec.render_hz)
        render_before = {}
        if render_settings:
            import carb.settings
            settings = carb.settings.get_settings()
            render_before = {key:settings.get(key) for key in render_settings}
            for key,value in render_settings.items():
                settings.set(key,value)
        create_scene(world, spec, args.seed, raw.get("scene", {}))
        robot = None
        if args.mode == "control":
            # Evaluation wrappers and contact APIs must precede PhysX views.
            if args.control_hook == "mobile_manipulation.evaluate:install":
                from mobile_manipulation.evaluate import prepare_evaluation_bodies
                prepare_evaluation_bodies(world)
            from isaacsim.core.utils.stage import add_reference_to_stage
            from isaacsim.core.prims import SingleArticulation
            add_reference_to_stage(args.robot_usd, args.robot_prim)
            from mobile_manipulation.robot import prepare_robot
            prepare_robot(world.stage, args.robot_prim, {**raw.get("robot", {}),
                "profile_path": raw.get("execution", {}).get("profile_path")})
            robot = world.scene.add(SingleArticulation(prim_path=args.robot_prim, name="mobile_robot"))
        sensors = (attach_sensors(world, spec, args.sensor_profile,
            parent_prim=raw.get("robot", {}).get("sensor_parent_prim")) if sensor_rendering else None)
        from rclpy.signals import SignalHandlerOptions
        rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
        node = rclpy.create_node("astrex_mm_sim")
        publisher = ScenePublisher(node, spec, sensors, effective, output)
        world.reset()
        if render_settings:
            render_actual = {key:settings.get(key) for key in render_settings}
            (output/"render_settings.json").write_text(json.dumps({
                "before":render_before,"requested":render_settings,"actual":render_actual,
                "source_sha256":effective["rendering"]["source_sha256"]},indent=2)+"\n")
            if render_actual != render_settings:
                raise RuntimeError("RENDER_PRESET_READBACK_MISMATCH")
        publisher.calibration()
        if args.mode == "control":
            if args.hook_python_path:
                sys.path.insert(0, str(Path(args.hook_python_path).resolve()))
            module, function = args.control_hook.split(":")
            hook = getattr(importlib.import_module(module), function)(
                world, {**raw.get("execution", {}), "robot": robot, "ros_node": node,
                        "output_dir": str(output), "robot_prim": args.robot_prim,
                        "effective_config_hash": effective["config_hash"]})
            def final_gate_step(dt):
                nonlocal callback_seconds, callback_failure
                if callback_failure is not None:
                    return
                callback_start = time.monotonic()
                try:
                    # Drain a bounded batch: ROS control can publish faster than
                    # wall-clock physics when GUI rendering runs below real time.
                    for _ in range(32):
                        rclpy.spin_once(node, timeout_sec=0)
                        if time.monotonic()-callback_start >= .002:
                            break
                    hook.step(float(world.current_time), dt)
                except Exception as exc:
                    # Kit logs callback exceptions without failing world.step.
                    # Propagate the first failure to the owner loop and close.
                    callback_failure = exc
                finally:
                    callback_seconds += time.monotonic()-callback_start
            world.add_physics_callback("astrex_final_gate", final_gate_step)
        (output / "ready.json").write_text(json.dumps({
            "ready": True, "mode": args.mode, "pid": os.getpid(),
            "sensor_profile_id": args.sensor_profile if sensor_rendering else None,
            "sensor_rendering":sensor_rendering,
            "note": "Startup only; no formal P1 acceptance implied"}) + "\n")
        print("ASTREX_MM_READY " + str(output), flush=True)
        started = time.monotonic()
        screenshot_requested = False
        while app.is_running() and not stopped and not (output / "stop_requested").exists():
            step_start = time.monotonic()
            callback_seconds = 0.
            world.step(render=True)
            if callback_failure is not None:
                raise RuntimeError("CONTROL_CALLBACK_FAILED") from callback_failure
            step_timings.append((time.monotonic()-step_start, callback_seconds))
            if len(step_timings) >= 300:
                timing = {'scope':'last_300_GUI_world_steps','samples':len(step_timings),
                          'simulation_stamp':float(world.current_time),'monotonic':time.monotonic()}
                from omni.kit.viewport.utility import get_active_viewport
                active_viewport=get_active_viewport()
                timing['viewport_resolution']=list(active_viewport.resolution) if active_viewport else None
                for index,key in enumerate(('world_step_wall_seconds','control_callback_wall_seconds')):
                    values=sorted(row[index] for row in step_timings)
                    timing[key]={'p50':values[len(values)//2],'p95':values[int(.95*(len(values)-1))],
                                 'max':max(values),'mean':sum(values)/len(values)}
                (output/'runtime_timing.json').write_text(json.dumps(timing,indent=2)+'\n')
                with (output/'runtime_timing.jsonl').open('a') as timing_stream:
                    timing_stream.write(json.dumps(timing)+'\n')
                step_timings.clear()
            if not world.is_playing():
                rclpy.spin_once(node, timeout_sec=0)
                if hook and hasattr(hook, "poll_watchdog"):
                    hook.poll_watchdog()
                continue
            sim_time = float(world.current_time)
            publisher.step(sim_time)
            if not hook:
                rclpy.spin_once(node, timeout_sec=0)
            if not screenshot_requested and world.current_time_step_index > 120:
                screenshot_requested = True
                from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport
                if get_active_viewport():
                    capture_viewport_to_file(get_active_viewport(), str(output / "gui_preview.png"))
            if args.duration and sim_time >= args.duration:
                break
        (output / "simulation_result.json").write_text(json.dumps({
            "status": "STOPPED", "simulation_seconds": float(world.current_time),
            "wall_seconds_after_ready": time.monotonic()-started,
            "requested_stop": stopped, "formal_acceptance": False}, indent=2) + "\n")
    except BaseException as exc:
        import traceback
        failure = {"error_type": type(exc).__name__, "error": str(exc), "traceback": traceback.format_exc(),
                   "pid": os.getpid(), "ready": (output / "ready.json").exists()}
        (output / "startup_failure.json").write_text(json.dumps(failure, indent=2) + "\n")
        traceback.print_exc()
        sys.stderr.flush()
        raise
    finally:
        if hook:
            hook.close()
        if node:
            node.destroy_node()
            rclpy.try_shutdown()
        app.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
