"""Bounded file-mailbox inference process; no ROS initialization or threads."""
import argparse
import json
import os
from pathlib import Path
import time
import numpy as np


def atomic_npz(path, **arrays):
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as stream:
        np.savez(stream, **arrays)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--conf", type=float, default=.35)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--max-det", type=int, default=50)
    args = parser.parse_args()
    from segmentation import Segmenter
    segmenter = Segmenter(args.model, args.device, args.conf, args.imgsz, args.max_det, args.state)
    mailbox = Path(args.mailbox)
    (mailbox/"ready").touch()
    while True:
        try:
            # Rename before reading so the sender may replace the next request.
            os.replace(mailbox/"request.npz", mailbox/"processing.npz")
        except FileNotFoundError:
            time.sleep(.01)
            continue
        with np.load(mailbox/"processing.npz", allow_pickle=False) as data:
            image = data["image"].copy()
            metadata = json.loads(str(data["metadata"].item()))
        (mailbox/"processing.npz").unlink(missing_ok=True)
        try:
            if image.ndim == 1:
                import cv2
                image = cv2.imdecode(image, cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError('Invalid compressed image')
            packet, jpeg = segmenter.infer(image, metadata)
        except Exception as exc:
            shape = image.shape if image is not None and image.ndim >= 2 else (0, 0)
            packet = {"timestamp": metadata["timestamp"], **metadata, "width": shape[1], "height": shape[0],
                      "status": "error", "error": str(exc)[:500], "objects": {}}
            jpeg = np.empty(0, dtype=np.uint8)
        atomic_npz(mailbox/"result.npz", packet=json.dumps(packet, ensure_ascii=False, allow_nan=False), jpeg=jpeg)


if __name__ == "__main__":
    main()
