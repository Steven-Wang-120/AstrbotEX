# Simulation execution interface

Run the gateway in system Jazzy Python. Isaac imports only the package source directory and reuses its own ROS bridge node. No CartPole package or interface is restored.

## Profile

`RobotProfile.load(path)` requires `arm_joint_names`, `gripper_joint_names`, and `joint_limits` keyed by those names. Each limit has `lower`, `upper`, `velocity`, `acceleration`, and `effort` in SI joint units. `profile_hash` is the SHA256 of canonical JSON, available as `RobotProfile.profile_hash`.

Optional fields: `group_name`, `base_frame`, `tcp_frame`, `moveit_namespace`, `arm_action`, `gripper_action`, `gripper_touch_links`, and the documented execution deadlines. Defaults are in `core.py`. One gripper drive joint is used. Physical mimic joints remain simulator/model constraints.

`gripper_velocity` sets the development speed cap in rad/s. It cannot exceed the profile joint limit. Isaac applies this cap and reports its readback. The current position controller does not independently enforce optional Action effort or velocity fields.

A mobile profile adds `base`: `wheel_joints` with `left`/`right` name lists, `wheel_radius`, `wheel_separation`, `wheel_effort_limit`, `initial_pose` [x,y,yaw], `max_linear_velocity` (at most 0.15 m/s), and `max_angular_velocity` (default 0.5 rad/s). Positive wheel joint velocity must roll forward. Wheel feedback supplies odometry. Stop evidence also uses actual PhysX root velocities. Every wheel surface speed must be at most 0.02 m/s. The root linear speed must be at most 0.02 m/s. Its angular speed must be at most 0.05 rad/s. Signed wheel averages and command velocities cannot prove a stop. `final_status.base_stop_measurement` records each wheel speed, the root vectors, their norms, and the physics source. Missing or invalid root feedback clears the stop window and holds the robot.

## One command per execution slot

`/astrex/mobile_mvp/{command,cancel,lease,ack,feedback,result,status}` use reliable, volatile `std_msgs/String` and bounded JSON (64 KiB).

Each command contains:

```json
{"schema":"astrex.mm.v1","command_id":"trial01/approach","ex_session":"trial-session","goal_revision":1,"execution_epoch":1,"deadline_monotonic":0,"robot_config_hash":"actual-hash","op":"arm","payload":{}}
```

Replace the deadline with this host's `time.monotonic()+duration`. Epoch increases for each command in a session. Duplicate identical commands replay the previous receipt/result and do not execute again. The same identity with different content is rejected. Send a lease every 100 ms with the same four identity fields and increasing integer `seq`. The local lease is 500 ms and cannot exceed the command deadline. Cancel is independent of planning.

- `arm`: payload has all `joint_targets`, or `target_pose` with position [x,y,z] and quaternion [x,y,z,w]. `cartesian:true` uses a complete collision-checked Cartesian service path. No external trajectory is accepted. `plan_only:true` validates the planned trajectory without an Action or an actuator target. Its result scope is `plan_only`.
- `gripper`: payload `position` is the actual revolute drive angle in radians; `max_effort` is N·m and cannot exceed the profile. This uses the standard `ParallelGripperCommand` Action. It is not a meter-valued `GripperCommand` alias.
- `move`: payload `target_pose` is a planar map pose; the gateway calls Nav2. Collision Monitor output is `TwistStamped`; the gateway adds execution identity and lease before Isaac receives it.

Arm payload can contain `scene`: `objects`, `attached`, `allow_contacts`, and `support_contacts`. Each object has stable `id`, primitive `shape` (box/sphere/cylinder), positive `dimensions`, `position`, and `orientation`; `remove:true` removes it. World objects use the profile base frame. An attached object uses the TCP frame. Allowed contacts can change only object/finger pairs from `gripper_touch_links`. Preserve allowed contacts explicitly between approach/release phases. The adapter preserves the existing MoveIt self-collision matrix.

`support_contacts` uses `{object_id, support_id, allowed}`. The only permitted pairs are `red_cube/table` and `red_cube/place_region`, as listed in `allowed_support_contacts`. Unspecified support pairs revert to `false` on each scene update. This permission never allows arm contact with the table.

`MoveItAdapter.update_scene` uses the same validated scene mutation as planning. It returns `scene_ack` after `ApplyPlanningScene` succeeds. The A2 checker always removes its diagnostic probe in `finally` and reads the scene back. Scene cleanup requires an idle gateway without a pending planner request. It submits no controller Action.

## Gateway and simulator

```bash
/usr/bin/python3 -m astrex_mobile_manipulation.gateway --ros-args -p profile:=PROFILE.json -p evidence_dir:=EVIDENCE -p use_sim_time:=true
```

MoveIt must expose planning, Cartesian, apply-scene and get-scene services under `moveit_namespace` with automatic execution disabled. Controller manager uses `joint_state_topic_hardware_interface/JointStateTopicSystem`, publishing `/astrex/mm/joint_command_raw` and reading `/astrex/mm/joint_states_raw`. Controller outputs require advancing source stamps.

Isaac hook: `astrex_mobile_manipulation.isaac_gate:install(world, config)`, where config supplies initialized `robot`, native `ros_node`, and `profile_path`. Call `step(sim_time, dt)` each physics cycle. It applies drive effort and velocity limits, then reads them back. It publishes actual joint states and holds on expired leases independently of ROS planning. It only receives guarded commands. Drive output effort remains unmeasured if the simulator exposes only applied effort commands; contact forces are not substituted for drive effort.

For Nav2, `navigation.launch.py` uses Smac 2D, RPP and Collision Monitor. The shipped 8×6 m map contains only the fixed boundary. Runtime obstacles must come from `/astrex/mm/scan`. Do not also publish `odom -> base_link`: the hook owns wheel odometry and that TF. The launch owns static known `map -> odom`.

## Result semantics and checks

`result.scope="controller_stage"`, `physical_complete=false`: an Action result never proves grasp or placement. The shared trial evaluator must check actual contact, lift, placement and collision evidence. Stop evidence references fresh raw feedback and a final-gate hold; resource release also waits for the old Action to terminate. Unproven stop remains blocked.

Raw state must arrive within 200 ms. Stop evidence requires all controlled joint speeds below 0.02 rad/s for 500 ms. These deadlines use wall time. Slow or paused simulation does not extend them. Raw state and final status use the same fresh physics sample. Truth sampling uses a 50 ms simulation interval or a 50 ms wall interval, whichever occurs first. Simulation time must advance before either stream publishes. Each truth publication reads current PhysX values. This removes publication delays but cannot remove a long render frame. A long render frame can therefore stop an active command. The evidence records this as a timing failure, not a successful execution.

`client.GatewayClient` is a helper for the single shared trial runner. It calls the same gateway protocol and renews its lease. It cannot drive actuators directly.

Pure tests: `PYTHONPATH=ros2_ws/src/astrex_mobile_manipulation /usr/bin/python3 -m unittest discover -s ros2_ws/src/astrex_mobile_manipulation/test -v`. These tests are not Isaac execution evidence.


## Local fault tests (R07/R08; not yet exercised in Isaac)

The normal configuration has no fault injection. To run a reviewed fixed-base
fault experiment, create an owner-only directory and a JSON file with mode
`0600`. Set the simulator's `execution.local_test_faults` to its absolute path.
Both the simulator and runner use that same file. Its fields are:

- `schema`: `astrex.mm.local-fault-test.v1`.
- `test_id`: a unique string, 16–80 characters.
- `profile_hash`: the actual fixed-base robot profile hash.
- `control_file`: a separate absolute file path in the same private directory.
- `expires_monotonic`: host monotonic time, at most two hours ahead.

Start Isaac and the standard controllers/MoveIt without an external gateway.
After the required GUI review, select the existing runner's
`--mode arm_checks --sections R_FAULTS --fault-config FILE`.
Do not combine this section with ordinary A1/A2/R checks.

R07 suppresses only raw JointState publication for a bounded interval after
actual motion begins. PhysX, final_status, truth and controller processes
continue. It checks that the gateway rejects stale raw feedback even if the
joint-state broadcaster continues publishing, then requires a new stop window.
Requests bind simulator session, command identity and sequence. A replay cannot
extend the pause. The maximum pause is two seconds.

R08 starts and owns one gateway child. After binding an actual moving command,
it intentionally sends SIGKILL only to that child. No external PID is accepted.
The final gate must stop independently while JTC still produces old targets.
COMMAND_STALE may stop it before the unchanged 500 ms lease expires; the report
records the actual reason and checks that the gate remains closed after expiry.
A restarted gateway must remain idle without forwarding the old target cache.
Normal cleanup uses SIGINT. The crash does not create a controller terminal
result or prove EX resource release. Executor restart and the upstream unknown
state still require their corresponding integration checks.

`sample_monotonic`, `lease_until_monotonic` and
`last_target_received_monotonic` are diagnostic fields. Unset times use JSON
null; internal guard clocks and thresholds are unchanged.

The 26 pure checks cover guard logic, fault opt-in, request identity, expiry,
replay, file permissions and owned-child signaling through mocks. They send no
real signals and are not physical R07/R08 acceptance evidence.
