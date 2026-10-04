#!/usr/bin/env python3
"""Record the P1 sensor stream through system ROS, without robot commands."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import struct
import time
import zlib

def stamp_ns(stamp):
    return stamp.sec*10**9+stamp.nanosec

def image_png(msg):
    channels = {"rgb8":3, "rgba8":4, "bgr8":3, "bgra8":4}.get(msg.encoding)
    if not channels:
        raise ValueError("Unsupported preview encoding: "+msg.encoding)
    raw = bytes(msg.data)
    rows = []
    for y in range(msg.height):
        row = raw[y*msg.step:y*msg.step+msg.width*channels]
        if channels==3 and msg.encoding=="rgb8":
            rgb=row
        else:
            rgb=bytearray(msg.width*3)
            order=(2,1,0) if msg.encoding.startswith("bgr") else (0,1,2)
            for out_channel,in_channel in enumerate(order):
                rgb[out_channel::3]=row[in_channel::channels]
        rows.append(b"\0"+bytes(rgb))
    def chunk(kind,payload):
        return struct.pack("!I",len(payload))+kind+payload+struct.pack("!I",zlib.crc32(kind+payload)&0xffffffff)
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack("!2I5B",msg.width,msg.height,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(b"".join(rows)))+chunk(b"IEND",b"")

def summary(rows):
    if not rows:
        return {"count":0,"simulation_span_s":None,"simulation_hz":None,"wall_hz":None}
    stamps=[r["stamp_ns"] for r in rows]
    received=[r["received_monotonic_ns"] for r in rows]
    ds=(stamps[-1]-stamps[0])/1e9
    dw=(received[-1]-received[0])/1e9
    return {"count":len(rows),"simulation_span_s":ds,"simulation_hz":(len(rows)-1)/ds if ds>0 else None,
            "wall_hz":(len(rows)-1)/dw if dw>0 else None,
            "non_increasing_stamps":sum(b<=a for a,b in zip(stamps,stamps[1:])),
            "frames":sorted({r.get("frame_id","") for r in rows})}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir",required=True)
    p.add_argument("--sensor-profile",choices=("RGB_LIDAR_2D","RGB_LIDAR_3D"),required=True)
    p.add_argument("--duration",type=float,default=30,help="Seconds of simulation-stamped complete sensor coverage")
    p.add_argument("--startup-timeout",type=float,default=300)
    p.add_argument("--wall-timeout",type=float,default=900)
    p.add_argument("--preview",action="store_true",help="Diagnostic only, cannot count as P1 acceptance")
    args=p.parse_args()
    if args.duration <= 0:
        p.error("duration must be positive")
    import rclpy
    from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy, ReliabilityPolicy
    from rclpy.serialization import serialize_message
    from sensor_msgs.msg import Image,CameraInfo,LaserScan,PointCloud2
    from rosgraph_msgs.msg import Clock
    from std_msgs.msg import String
    from tf2_msgs.msg import TFMessage
    from tf2_ros import Buffer,TransformListener
    from rclpy.time import Time
    output=Path(args.output_dir).resolve()
    output.mkdir(parents=True,exist_ok=True)
    raw_dir=output/"raw"
    raw_dir.mkdir(exist_ok=True)
    records=defaultdict(list)
    last_saved={}
    errors=[]
    rate_diagnostics=[]
    manifest={}
    immutable_manifest=None
    manifest_hashes=set()
    first_complete=None
    received_started=time.monotonic()
    rclpy.init()
    node=rclpy.create_node("astrex_mm_sensor_capture")
    buffer=Buffer()
    listener=TransformListener(buffer,node)
    index=(output/"messages.jsonl").open("w")
    required=["image","camera_info","lidar","clock"]
    def receive(kind,msg):
        stamp=msg.clock if kind=="clock" else msg.header.stamp
        row={"kind":kind,"stamp_ns":stamp_ns(stamp),"received_monotonic_ns":time.monotonic_ns()}
        if kind!="clock":
            row["frame_id"]=msg.header.frame_id
        if kind=="image":
            row.update(width=msg.width,height=msg.height,encoding=msg.encoding,bytes=len(msg.data))
        elif kind=="camera_info":
            row.update(width=msg.width,height=msg.height,k=list(msg.k),distortion_model=msg.distortion_model)
        elif kind=="lidar":
            if args.sensor_profile=="RGB_LIDAR_2D":
                row.update(points=len(msg.ranges),finite_ranges=sum(math.isfinite(v) for v in msg.ranges))
            else:
                row.update(points=msg.width*msg.height,bytes=len(msg.data),fields=[f.name for f in msg.fields])
        if kind!="clock" and row["stamp_ns"]-last_saved.get(kind,-10**18)>=10**9:
            filename=f"{kind}_{row['stamp_ns']}.cdr"
            (raw_dir/filename).write_bytes(serialize_message(msg))
            row["raw_path"]="raw/"+filename
            last_saved[kind]=row["stamp_ns"]
            if kind=="image" and not (output/"camera_preview.png").exists():
                try:
                    (output/"camera_preview.png").write_bytes(image_png(msg))
                except ValueError as exc:
                    errors.append(str(exc))
        records[kind].append(row)
        index.write(json.dumps(row)+"\n")
    def receive_manifest(msg):
        nonlocal manifest, immutable_manifest
        manifest=json.loads(msg.data)
        if immutable_manifest is None:
            # Freeze the first configuration; later heartbeat timestamps do not
            # change the TF reference used for this capture.
            immutable_manifest={key:value for key,value in manifest.items()
                if key not in {"simulation_stamp_ns","published_monotonic_ns"}}
        manifest_hashes.add(manifest.get("config_hash"))
    subscriptions=[
        node.create_subscription(Image,"/astrex/mm/camera/image_raw",lambda m:receive("image",m),
            QoSProfile(depth=5,reliability=ReliabilityPolicy.RELIABLE)),
        node.create_subscription(CameraInfo,"/astrex/mm/camera/camera_info",lambda m:receive("camera_info",m),qos_profile_sensor_data),
        node.create_subscription(Clock,"/clock",lambda m:receive("clock",m),qos_profile_sensor_data),
        node.create_subscription(LaserScan if args.sensor_profile=="RGB_LIDAR_2D" else PointCloud2,
            "/astrex/mm/scan" if args.sensor_profile=="RGB_LIDAR_2D" else "/astrex/mm/points",
            lambda m:receive("lidar",m),qos_profile_sensor_data),
        node.create_subscription(String,"/astrex/mm/sensor_manifest",receive_manifest,
            QoSProfile(depth=1,durability=DurabilityPolicy.TRANSIENT_LOCAL)),
    ]
    try:
        while rclpy.ok():
            rclpy.spin_once(node,timeout_sec=0.1)
            elapsed=time.monotonic()-received_started
            if all(records[k] for k in required):
                if first_complete is None:
                    first_complete=time.monotonic()
                spans=[(records[k][-1]["stamp_ns"]-records[k][0]["stamp_ns"])/1e9 for k in required]
                if min(spans)>=args.duration:
                    break
            if first_complete is None and elapsed>args.startup_timeout:
                errors.append("Required sensor streams did not all arrive before startup timeout")
                break
            if elapsed>args.wall_timeout:
                errors.append("Capture did not reach requested simulation coverage before wall timeout")
                break
        streams={k:summary(records[k]) for k in required}
        publishers=node.get_publishers_info_by_topic("/clock")
        if len(publishers)!=1:
            errors.append(f"Expected one clock publisher, found {len(publishers)}")
        for k,row in streams.items():
            if row["count"]<2 or row["simulation_span_s"]<args.duration:
                errors.append(f"{k}: incomplete continuous capture")
            if row.get("non_increasing_stamps"):
                errors.append(f"{k}: timestamps did not increase")
        if len(manifest_hashes)!=1 or None in manifest_hashes:
            errors.append("Missing or changing sensor configuration hash")
        for kind,expected_frame in (("image","mm_camera_optical"),("camera_info","mm_camera_optical"),("lidar","mm_lidar")):
            if streams[kind].get("frames")!=[expected_frame]:
                errors.append(f"{kind}: unexpected frame ID")
        for kind,target_hz in (("image",15),("camera_info",15),("lidar",10)):
            measured=streams[kind]["simulation_hz"]
            if measured is not None and abs(measured-target_hz)>target_hz*0.15:
                rate_diagnostics.append({"stream":kind,"configured_hz":target_hz,"received_simulation_hz":measured,
                    "note":"Exceeds an additional 15% frequency diagnostic band; not a P1 business acceptance gate"})
        if records["image"] and any((r["width"],r["height"])!=(640,480) for r in records["image"]):
            errors.append("Image resolution differs from 640x480")
        if records["camera_info"] and any(r["k"][0]<=0 or r["k"][4]<=0 for r in records["camera_info"]):
            errors.append("CameraInfo focal lengths are invalid")
        if manifest.get("sensor_profile_id")!=args.sensor_profile:
            errors.append("Sensor manifest/profile mismatch or missing manifest")
        transforms={}
        tf_reference_frame=(immutable_manifest or {}).get("sensors",{}).get("frame_id","world")
        for frame in ("mm_camera_optical","mm_lidar"):
            try:
                tf=buffer.lookup_transform(tf_reference_frame,frame,Time())
                transforms[frame]={"translation":[tf.transform.translation.x,tf.transform.translation.y,tf.transform.translation.z],
                    "rotation_xyzw":[tf.transform.rotation.x,tf.transform.rotation.y,tf.transform.rotation.z,tf.transform.rotation.w]}
            except Exception as exc:
                errors.append(f"Missing TF {tf_reference_frame}->{frame}: {exc}")
        image_stamps=[r["stamp_ns"] for r in records["image"]]
        lidar_stamps=[r["stamp_ns"] for r in records["lidar"]]
        pairs=[min(abs(t-s) for s in image_stamps)/1e6 for t in lidar_stamps] if image_stamps else []
        paired=sum(v<=50 for v in pairs)
        result={"status":"FAIL" if errors else ("PREVIEW_PASS" if args.preview else "PASS"),
            "formal_acceptance":not args.preview and not errors and args.duration>=30,
            "requested_simulation_seconds":args.duration,"wall_seconds":time.monotonic()-received_started,
            "sensor_profile_id":args.sensor_profile,"manifest":manifest,"streams":streams,"errors":errors,
            "acceptance_basis":"P1: >=30 simulation seconds of real ROS streams with valid clock, frame, calibration and configuration association; measured rates reported separately",
            "rate_diagnostics":rate_diagnostics,
            "clock_publishers":[{"name":i.node_name,"namespace":i.node_namespace} for i in publishers],
            "tf_reference_frame":tf_reference_frame,
            "tf_reference_manifest_hash":(immutable_manifest or {}).get("config_hash"),
            "tf":transforms,"pairing":{"threshold_ms":50,"paired":paired,"total":len(pairs),
                "maximum_delta_ms":max(pairs) if pairs else None},
            "raw_sampling_rule":"First received sensor message and then first at least 1 simulation second after last saved"}
        (output/"capture_result.json").write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps(result,indent=2))
        return 1 if errors else 0
    finally:
        index.close()
        node.destroy_node()
        rclpy.shutdown()

if __name__=="__main__":
    raise SystemExit(main())
