"""Inference-only module; import in the isolated worker, never the Core Actor."""
import cv2
import numpy as np
from geometry import bbox_record
from state import State

COLORS = ("black", "white", "gray", "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink", "brown")


def dominant_color(image, mask):
    mask = (mask > 0).astype(np.uint8)
    inner = cv2.erode(mask, np.ones((3, 3), np.uint8))
    if inner.sum() >= 16:
        mask = inner
    pixels = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[mask.astype(bool)]
    if not len(pixels):
        return "unknown"
    h, s, v = pixels.T
    labels = np.full(len(pixels), 3, dtype=np.uint8)
    labels[(h >= 10) & (h < 23)] = 4
    labels[(h >= 23) & (h < 35)] = 5
    labels[(h >= 35) & (h < 85)] = 6
    labels[(h >= 85) & (h < 100)] = 7
    labels[(h >= 100) & (h < 130)] = 8
    labels[(h >= 130) & (h < 155)] = 9
    labels[(h >= 155) & (h < 173)] = 10
    labels[(h >= 5) & (h < 25) & (v < 160)] = 11
    labels[s < 45] = 2
    labels[(s < 45) & (v >= 200)] = 1
    labels[v < 45] = 0
    return COLORS[int(np.bincount(labels, minlength=len(COLORS)).argmax())]


def draw_axes(image):
    h, w = image.shape[:2]
    cx, cy = w//2, h//2
    cv2.line(image, (0, cy), (w-1, cy), (220, 220, 220), 1)
    cv2.line(image, (cx, 0), (cx, h-1), (220, 220, 220), 1)
    for text, xy in (("x +1", (4, max(14, cy-5))), ("x -1", (max(0, w-55), max(14, cy-5))),
                     ("y +1", (cx+4, 16)), ("y -1", (cx+4, h-8)), ("(0,0)", (cx+4, cy+16))):
        cv2.putText(image, text, xy, cv2.FONT_HERSHEY_SIMPLEX, .45, (240, 240, 240), 1, cv2.LINE_AA)
    cv2.circle(image, (cx, cy), 3, (255, 255, 255), -1)


class Segmenter:
    def __init__(self, model_path, device="cpu", conf=0.35, imgsz=640, max_det=50, state_path=None):
        from ultralytics import YOLO
        self.model = YOLO(model_path, task="segment")
        if self.model.task != "segment":
            raise ValueError("A YOLO segmentation model (-seg.pt) is required")
        self.options = dict(device=device, conf=conf, imgsz=imgsz, max_det=max_det, verbose=False, retina_masks=True)
        self.state = State(state_path)

    def infer(self, image, metadata):
        result = self.model.predict(image, **self.options)[0]
        boxes = result.boxes.xyxy.cpu().numpy()
        classes = result.boxes.cls.cpu().numpy().astype(int)
        confidence = result.boxes.conf.cpu().numpy()
        if len(boxes) and (result.masks is None or len(result.masks.data) != len(boxes)):
            raise RuntimeError("Segmentation masks missing or mismatched")
        masks = result.masks.data.cpu().numpy() if len(boxes) else []
        h, w = image.shape[:2]
        masks = [cv2.resize(m.astype(np.uint8), (w,h), interpolation=cv2.INTER_NEAREST) for m in masks]
        colors = [dominant_color(image,m) for m in masks]
        hsv = cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        observations=[]
        for box,cls,mask,color in zip(boxes,classes,masks,colors):
            hist=cv2.calcHist([hsv],[0,1],mask,[16,4],[0,180,0,256]).flatten()
            hist=hist/max(float(hist.sum()),1)
            observations.append({'name':str(result.names[int(cls)]),'color':color,'box':box.tolist(),'feature':hist.tolist()})
        ids = self.state.associate(observations)
        revision,states,marked = self.state.snapshot(ids)
        canvas, objects, labels = image.copy(), {}, []
        for box, cls, conf, mask, oid in zip(boxes, classes, confidence, masks, ids):
            mask = cv2.resize(mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
            color = dominant_color(image, mask)
            bbox = bbox_record(box, w, h)
            obj = {"id": oid, "name": str(result.names[int(cls)]), "color": color,
                   "conf": round(float(conf), 6), "bbox": bbox, "act": states[oid]}
            objects[str(oid)] = obj
            seed=int(oid[:6],16)
            tint = (0,255,255) if obj['act'] else ((seed*71)%180+75,(seed*113)%180+75,(seed*43)%180+75)
            selected = mask.astype(bool)
            canvas[selected] = (canvas[selected]*0.75 + np.array(tint)*0.25).astype(np.uint8)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(canvas, contours, -1, tint, 2)
            x1, y1, x2, y2 = bbox["px"]
            cv2.rectangle(canvas, (x1, y1), (min(x2, w-1), min(y2, h-1)), tint, 1)
            centre = (min(w-1, (x1+x2)//2), min(h-1, (y1+y2)//2))
            cv2.drawMarker(canvas, centre, (0, 0, 255), cv2.MARKER_CROSS, 16, 2)
            labels.append((x1, y1, tint, [f"{'MARKED ' if obj['act'] else ''}#{oid[:6]} {obj['name']} {conf:.2f}",
                                        f"{color} c=({bbox['c'][0]:.2f},{bbox['c'][1]:.2f})"]))
        draw_axes(canvas)
        # Draw labels last so later masks do not obscure them; keep text inside frame.
        for x1, y1, tint, lines in labels:
            widths = [cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, .45, 1)[0][0] for line in lines]
            left = max(0, min(x1, w-max(widths)-8))
            top = max(0, min(y1-40, h-42))
            cv2.rectangle(canvas, (left, top), (min(w-1, left+max(widths)+8), top+40), (20, 20, 20), -1)
            for n, line in enumerate(lines):
                cv2.putText(canvas, line, (left+4, top+15+n*18), cv2.FONT_HERSHEY_SIMPLEX, .45, tint, 1, cv2.LINE_AA)
        packet = {"timestamp": metadata["timestamp"], **metadata, "width": w, "height": h, "status": "ok", "mark_rev": revision, "marks": marked, "objects": objects}
        ok, jpeg = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return packet, jpeg
