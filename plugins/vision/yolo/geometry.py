"""Image-edge coordinates: left/top +1, centre 0, right/bottom -1."""
import math


def point(x, y, width, height):
    if width < 2 or height < 2:
        raise ValueError("Image must be at least 2x2")
    if not all(math.isfinite(float(v)) for v in (x, y)):
        raise ValueError("Non-finite coordinates")
    # Coordinates use continuous image boundaries [0,W] x [0,H].
    return [round(1 - 2 * min(max(float(x), 0), width) / width, 6),
            round(1 - 2 * min(max(float(y), 0), height) / height, 6)]


def bbox_record(box, width, height):
    x1, y1, x2, y2 = [float(v) for v in box]
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        raise ValueError("Non-finite box")
    x1, x2 = sorted((min(max(x1, 0), width), min(max(x2, 0), width)))
    y1, y2 = sorted((min(max(y1, 0), height), min(max(y2, 0), height)))
    return {"c": point((x1+x2)/2, (y1+y2)/2, width, height),
            "rt": point(x2, y1, width, height),
            "lb": point(x1, y2, width, height),
            "px": [int(math.floor(x1)), int(math.floor(y1)),
                   int(math.ceil(x2)), int(math.ceil(y2))]}


def iou(a, b):
    overlap = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - overlap
    return overlap / union if union > 0 else 0.0


class ObjectIds:
    """Conservative class+IoU association; IDs are unique within one stream.

    Does not claim re-identification after occlusion or crossing objects.
    Empty frames expire associations after max_gap frames.
    """
    def __init__(self, threshold=0.3, max_gap=5):
        self.threshold, self.max_gap = threshold, max_gap
        self.tracks, self.next_id, self.frame = {}, 1, 0

    def update(self, boxes, classes):
        self.frame += 1
        self.tracks = {k: v for k, v in self.tracks.items() if self.frame-v[2] <= self.max_gap+1}
        candidates = sorted(((iou(box, old[0]), idx, key)
                             for idx, (box, cls) in enumerate(zip(boxes, classes))
                             for key, old in self.tracks.items() if cls == old[1]), reverse=True)
        assigned, used = {}, set()
        for score, idx, key in candidates:
            if score >= self.threshold and idx not in assigned and key not in used:
                assigned[idx] = key
                used.add(key)
        ids = []
        for idx, (box, cls) in enumerate(zip(boxes, classes)):
            key = assigned.get(idx)
            if key is None:
                key, self.next_id = self.next_id, self.next_id + 1
            self.tracks[key] = (list(box), int(cls), self.frame)
            ids.append(key)
        return ids
