"""Isaac-only scene construction and native ROS sensor publishers.

This module is imported after SimulationApp starts. Truth geometry stays in the
simulation/evaluation process; only RGB, lidar and calibration reach perception.
"""
import json
import time
import numpy as np
from pxr import Gf, UsdGeom, UsdLux, UsdPhysics
from mobile_manipulation.spec import PROFILES, obstacle_layout

def create_scene(world, spec, seed, scene_config=None):
    scene_config = scene_config or {}
    from isaacsim.core.api.objects import DynamicCuboid, FixedCuboid
    from isaacsim.core.utils.viewports import set_camera_view
    stage = world.stage
    world.scene.add_default_ground_plane()
    light = UsdLux.DomeLight.Define(stage, "/World/Lighting")
    light.CreateIntensityAttr(1200)
    world.scene.add(FixedCuboid(prim_path="/World/Table", name="table",
        position=np.array(spec.table_position), scale=np.array(spec.table_dimensions),
        size=1.0, color=np.array([0.46, 0.48, 0.5])))
    rng = np.random.default_rng(seed)
    offset = rng.uniform(-0.025, 0.025, 2)
    world.scene.add(DynamicCuboid(prim_path="/World/RedCube", name="red_cube",
        position=np.array([0.5+offset[0], -0.08+offset[1], 0.78]),
        scale=np.array([0.05]*3), size=1.0, mass=0.08, color=np.array([0.85, 0.03, 0.02])))
    cylinder = UsdGeom.Cylinder.Define(stage, "/World/BlueCylinder")
    cylinder.CreateRadiusAttr(0.025)
    cylinder.CreateHeightAttr(0.05)
    cylinder.CreateAxisAttr("Z")
    cylinder.AddTranslateOp().Set(Gf.Vec3d(0.65, 0.10, 0.78))
    cylinder.CreateDisplayColorAttr([Gf.Vec3f(0.02, 0.12, 0.9)])
    UsdPhysics.CollisionAPI.Apply(cylinder.GetPrim())
    UsdPhysics.RigidBodyAPI.Apply(cylinder.GetPrim())
    UsdPhysics.MassAPI.Apply(cylinder.GetPrim()).CreateMassAttr(0.08)
    world.scene.add(FixedCuboid(prim_path="/World/PlaceRegion", name="place_region",
        position=np.array(scene_config.get("place_position", [0.87, -0.02, 0.753])),
        scale=np.array([0.14, 0.14, 0.005]),
        size=1.0, color=np.array([0.02, 0.7, 0.1])))
    for i, obstacle in enumerate(obstacle_layout(scene_config)):
        world.scene.add(FixedCuboid(prim_path=f"/World/Obstacle{i}", name=f"obstacle_{i}",
            position=np.array(obstacle["position"]), scale=np.array(obstacle["dimensions"]), size=1.0,
            color=np.array([0.6, 0.43, 0.18])))
    walls = scene_config.get("channel", {}).get("walls", [])
    if walls and len(walls) != 2:
        raise ValueError("A configured research channel requires exactly two walls")
    for i, wall in enumerate(walls):
        position = np.asarray(wall["position"], dtype=float)
        dimensions = np.asarray(wall["dimensions"], dtype=float)
        if position.shape != (3,) or dimensions.shape != (3,) or not np.isfinite(position).all() or not np.isfinite(dimensions).all() or (dimensions <= 0).any():
            raise ValueError("Channel wall position/dimensions must be finite 3-vectors with positive dimensions")
        world.scene.add(FixedCuboid(prim_path=f"/World/ChannelWall{i}", name=f"channel_wall_{i}",
            position=position, scale=dimensions, size=1.0, color=np.array([0.4,0.3,0.6])))
    set_camera_view(eye=np.array(scene_config.get("viewport_eye",[2.0,-2.2,1.8])),
                    target=np.array(scene_config.get("viewport_target",[0.5,0,0.5])))
    # The GUI display texture is separate from the recorded RGB sensor.
    resolution = scene_config.get("viewport_resolution")
    if resolution is not None:
        if len(resolution)!=2 or any(type(x) is not int or x<=0 for x in resolution):
            raise ValueError("Viewport resolution requires two positive integers")
        from omni.kit.viewport.utility import get_active_viewport
        viewport=get_active_viewport()
        if viewport is not None:
            viewport.set_texture_resolution(tuple(resolution))
    return {"target_prim": "/World/RedCube", "place_prim": "/World/PlaceRegion"}

def attach_sensors(world, spec, profile, parent_prim=None):
    import omni.graph.core as og
    import omni.kit.commands
    import omni.replicator.core as rep
    from isaacsim.sensors.camera import Camera
    rig = (parent_prim.rstrip("/") + "/SensorRig") if parent_prim else "/World/Rig"
    UsdGeom.Xform.Define(world.stage, rig)
    camera = Camera(prim_path=rig+"/Camera", name="rgb_camera",
                    translation=np.array(spec.camera_position), frequency=spec.camera_hz,
                    resolution=(spec.width, spec.height))
    look = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*spec.camera_position),
            Gf.Vec3d(*spec.camera_target), Gf.Vec3d(0,0,1)).GetInverse().ExtractRotationQuat()
    quaternion = np.array([look.GetReal(), *look.GetImaginary()])
    camera.set_local_pose(translation=np.array(spec.camera_position), orientation=quaternion, camera_axes="usd")
    camera.set_focal_length(0.024)
    camera.set_horizontal_aperture(0.036)
    camera.set_vertical_aperture(0.027)
    camera.set_clipping_range(0.05, 100.0)
    camera_product = rep.create.render_product(rig+"/Camera", (spec.width, spec.height))
    success, lidar = omni.kit.commands.execute("IsaacSensorCreateRtxLidar",
        path=rig+"/Lidar", parent=None, config=PROFILES[profile]["asset"],
        translation=Gf.Vec3d(*map(float, spec.lidar_position)), orientation=Gf.Quatd(1,0,0,0))
    if not success or lidar is None:
        raise RuntimeError("Native RTX lidar creation failed")
    # Native rotary profiles accumulate a complete scan before ROS publication.
    # Do not additionally decimate full-scan output: this would turn 10 Hz into 1.67 Hz.
    rate_attributes = [a for a in lidar.GetAttributes() if a.GetName().endswith("scanRateBaseHz")]
    if rate_attributes:
        rate_attributes[0].Set(float(spec.lidar_hz))
    # Trace each render tick and accumulate one complete 10 Hz rotation.
    # Stock profiles set sensor tickRate=10, which showed irregular missed
    # full-scan publications on the 60 Hz renderer during P1 diagnostics.
    lidar.GetAttribute("omni:sensor:tickRate").Set(float(spec.render_hz))
    for name,value in (("nearRangeM",spec.lidar_min_range),("farRangeM",spec.lidar_max_range)):
        if value is not None:
            lidar.GetAttribute("omni:sensor:Core:"+name).Set(float(value))
    lidar_product = rep.create.render_product(lidar.GetPath(), (128,128),
        render_vars=["GenericModelOutput", "RtxSensorMetadata"])
    nodes = [("Tick", "omni.graph.action.OnPlaybackTick"),
             ("Context", "isaacsim.ros2.bridge.ROS2Context"),
             ("RGB", "isaacsim.ros2.bridge.ROS2CameraHelper"),
             ("CameraInfo", "isaacsim.ros2.bridge.ROS2CameraInfoHelper"),
             ("Lidar", "isaacsim.ros2.bridge.ROS2RtxLidarHelper")]
    values = [("Context.inputs:useDomainIDEnvVar", True)]
    connections = []
    for name in ("RGB", "CameraInfo", "Lidar"):
        connections.extend([("Tick.outputs:tick", f"{name}.inputs:execIn"),
                            ("Context.outputs:context", f"{name}.inputs:context")])
        values.append((f"{name}.inputs:queueSize", 5))
    for name, topic in (("RGB","/astrex/mm/camera/image_raw"),("CameraInfo","/astrex/mm/camera/camera_info")):
        values.extend([(f"{name}.inputs:renderProductPath",camera_product.path),
                       (f"{name}.inputs:frameId",spec.camera_frame),
                       (f"{name}.inputs:topicName",topic),
                       (f"{name}.inputs:frameSkipCount",spec.render_hz//spec.camera_hz-1)])
    values.extend([("RGB.inputs:type","rgb"),
                   ("Lidar.inputs:renderProductPath",lidar_product.path),
                   ("Lidar.inputs:frameId",spec.lidar_frame),
                   ("Lidar.inputs:topicName",PROFILES[profile]["topic"]),
                   ("Lidar.inputs:type",PROFILES[profile]["type"]),
                   ("Lidar.inputs:fullScan",profile=="RGB_LIDAR_3D"),
                   ("Lidar.inputs:frameSkipCount",0),
                   ("Lidar.inputs:showDebugView",False)])
    og.Controller.edit({"graph_path":"/World/SensorGraph","evaluator_name":"execution"},
        {og.Controller.Keys.CREATE_NODES:nodes, og.Controller.Keys.SET_VALUES:values,
         og.Controller.Keys.CONNECT:connections})
    return {"camera":camera, "lidar":lidar, "parent_prim":parent_prim,
            "products":[camera_product,lidar_product]}

class ScenePublisher:
    def __init__(self, node, spec, sensors, config, output):
        from rosgraph_msgs.msg import Clock
        from tf2_ros import StaticTransformBroadcaster
        from std_msgs.msg import String
        from rclpy.qos import QoSProfile, DurabilityPolicy
        self.node, self.spec, self.sensors = node, spec, sensors
        self.config, self.output = config, output
        self.clock = node.create_publisher(Clock, "/clock", 10)
        self.static_tf = StaticTransformBroadcaster(node)
        self.manifest = node.create_publisher(String, "/astrex/mm/sensor_manifest",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.last_manifest = -1
    def calibration(self):
        if self.sensors is None:
            (self.output/"calibration.json").write_text(json.dumps({
                "active":False,"reason":"ORACLE_CONTROL_ONLY_NO_SENSOR_RENDER_PRODUCTS",
                "sensor_tf_published":False},indent=2)+"\n")
            return
        from geometry_msgs.msg import TransformStamped
        camera_position, camera_q = self.sensors["camera"].get_local_pose(camera_axes="ros")
        if self.sensors["parent_prim"] is None and self.spec.frame_id != "world":
            raise ValueError("Fixed sensor rig must use world parent frame")
        if self.sensors["parent_prim"] is not None and self.spec.frame_id == "world":
            raise ValueError("Mobile sensor rig must declare its moving parent frame")
        transforms = []
        for child, position, quat in [
            (self.spec.camera_frame, camera_position, camera_q),
            (self.spec.lidar_frame, self.spec.lidar_position, (1,0,0,0)),
        ]:
            tf = TransformStamped()
            tf.header.frame_id = self.spec.frame_id
            tf.child_frame_id = child
            tf.transform.translation.x, tf.transform.translation.y, tf.transform.translation.z = map(float, position)
            tf.transform.rotation.w, tf.transform.rotation.x, tf.transform.rotation.y, tf.transform.rotation.z = map(float, quat)
            transforms.append(tf)
        self.static_tf.sendTransform(transforms)
        attributes = {a.GetName():str(a.Get()) for a in self.sensors["lidar"].GetAttributes()
                      if "sensor" in a.GetName().lower()}
        calib = {"calibration_id":self.spec.calibration_id, "parent_frame":self.spec.frame_id,
                 "camera_position":list(map(float,camera_position)),
                 "camera_quaternion_wxyz":list(map(float,camera_q)),
                 "lidar_position":list(self.spec.lidar_position),
                 "lidar_quaternion_wxyz":[1,0,0,0], "native_lidar_attributes":attributes}
        (self.output/"calibration.json").write_text(json.dumps(calib,indent=2)+"\n")

    def step(self, sim_time):
        from rosgraph_msgs.msg import Clock
        from std_msgs.msg import String
        msg = Clock()
        ns = round(sim_time*1e9)
        msg.clock.sec, msg.clock.nanosec = divmod(ns,10**9)
        self.clock.publish(msg)
        if sim_time-self.last_manifest >= 1:
            self.last_manifest = sim_time
            value = {**self.config, "simulation_stamp_ns":ns, "published_monotonic_ns":time.monotonic_ns()}
            message = String()
            message.data = json.dumps(value)
            self.manifest.publish(message)
