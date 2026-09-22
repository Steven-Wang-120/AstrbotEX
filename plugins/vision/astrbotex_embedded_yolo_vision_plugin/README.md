# AstrBotEX Embedded YOLO Vision Plugin

This plugin is an independent vision provider. The AstrBotEX host starts only
the standard-library wrapper and a separate `worker/yolo_worker.py` process;
`torch`, `cv2`, `numpy`, and `ultralytics` are imported only inside that worker.

## Deployment

Keep this plugin disabled while validating it in `video_file` mode. The current
Windows + Linux Docker deployment uses `mjpeg_http` with
`http://host.docker.internal:8769/video`. Do not enable `camera_index` unless
the worker environment exposes a real device. Install `pyzmq` in the plugin
runtime, and provide a platform-specific runtime under `runtime/` for a formal
deployment. The default config publishes no TopicBus topics.

## Protocol

The worker emits one JSON object per stdout line. Detection packets always carry
monotonic `frame_id`, Unix `timestamp`, integer `bbox_xyxy`, both legacy aliases
(`class_name`/`class`, `confidence`/`score`), and empty packets when no object is
detected. JPEG is optional base64 on the packet and is sent separately from JSON.

The wrapper sends `system.hello` on the text and vision channels and defaults to
`vision.json.publish` on port 8766 and `vision.jpeg.publish` on port 8768. Both
methods can be overridden for older A.E.B deployments. A.E.B failures are
reported and reconnect without stopping the worker.

## External training and rollback

Training remains in `C:\Users\17088\yolo11n-workspace`. Validate a candidate,
atomically replace `active.pt`, and restart the worker. Never run this plugin
and the legacy YOLO producer at the same time. To roll back, disable this plugin,
enable the old producer, and restore the external YOLO container.

## Tests

Run `python -m pytest tests` from this directory. The tests cover packet
validation, compatibility aliases, latest-value queue semantics, worker event
parsing, and MJPEG frame extraction without requiring YOLO or a camera.

## Production gates

Before formal enablement, build and record the Linux/Windows worker runtime,
dependency versions, package/resource metrics, perform 10-minute and 1-hour
stability runs, verify A.E.B JSON/JPEG caches and matching frame IDs, test
camera/A.E.B restart recovery, and exercise the legacy-plugin rollback.
