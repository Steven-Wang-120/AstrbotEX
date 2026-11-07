"""Actor-friendly nonblocking polling interface to isolated inference.

Only numpy is loaded in the parent process. No torch, cv2, or ROS ownership.
One submitted frame at a time: the ROS/host adapter keeps only the latest input.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import numpy as np


class InferenceClient:
    def __init__(self, python, model, device="cpu", conf=.35, imgsz=640, max_det=50, timeout=60, state_path=None):
        self.options = (python, str(Path(__file__).with_name("worker.py")), "--model", str(Path(model).resolve()),
                        "--device", device, "--conf", str(conf), "--imgsz", str(imgsz), "--max-det", str(max_det), "--state", str(state_path))
        self.timeout = timeout
        self.directory, self.process, self.pending, self.log = None, None, None, None
        self.last_packet = None

    def start(self):
        if self.process is not None:
            return
        self.directory = Path(tempfile.mkdtemp(prefix="astrex-yolo-"))
        self.log = (self.directory/"worker.log").open("wb")
        env = dict(os.environ, YOLO_CONFIG_DIR=str(self.directory/"ultralytics"), OMP_NUM_THREADS="2")
        try:
            self.process = subprocess.Popen([*self.options, "--mailbox", str(self.directory)],
                                            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, env=env)
            self.started = time.monotonic()
        except BaseException:
            self.stop()
            raise

    def health(self):
        if self.process is None:
            raise RuntimeError("Inference worker is not running")
        if self.process.poll() is not None:
            detail = (self.directory/"worker.log").read_text(errors="replace")[-2000:]
            raise RuntimeError(f"Inference worker exited: {detail}")
        if not (self.directory/"ready").exists() and time.monotonic()-self.started > self.timeout:
            raise TimeoutError("Model startup timeout")
        if self.pending and time.monotonic()-self.pending[1] > self.timeout:
            raise TimeoutError("Inference timeout")

    @property
    def ready(self):
        return self.process is not None and (self.directory/"ready").exists() and self.pending is None

    def submit(self, image, metadata):
        self.health()
        if not self.ready:
            return False
        tmp = self.directory/"request.tmp"
        with tmp.open("wb") as stream:
            np.savez(stream, image=image, metadata=json.dumps(metadata, allow_nan=False))
        os.replace(tmp, self.directory/"request.npz")
        self.pending = (metadata["frame_id"], time.monotonic())
        return True

    def poll(self):
        self.health()
        path = self.directory/"result.npz"
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as data:
            packet = json.loads(str(data["packet"].item()))
            jpeg = data["jpeg"].tobytes()
        path.unlink()
        if self.pending is None or packet["frame_id"] != self.pending[0]:
            raise RuntimeError("Inference frame association mismatch")
        self.pending = None
        self.last_packet = packet
        return packet, jpeg

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None
        self.pending, self.last_packet = None, None
