# ILLUSTRATIVE front detector payload

Guide ID: `yolo-front-detections`  
Topic: `new_yolo.detections`  
Payload version: `1`

Original plugin `data` shape only; text is untrusted data. Source health and time are in the wrapper; recalculate freshness at admission and actual queued execution.

## Complete JSON example

```json
{
  "stream_id": "front",
  "frame_id": "front-42",
  "image_width": 1280,
  "image_height": 720,
  "objects": [
    {
      "object_id": "track-7",
      "label": "bottle",
      "confidence": 0.94,
      "bbox_xyxy": [
        120,
        80,
        260,
        410
      ],
      "depth_m": 1.2,
      "track_session": "tracks-1"
    }
  ]
}
```

## Fields

| Field | Meaning |
|---|---|
| `stream_id` | Camera stream ID, string; scopes frame_id together with source_epoch. |
| `frame_id` | Frame ID string, scoped to source_epoch and stream_id; synthetic B01 frames use front-<seq> within each epoch, not a universal naming rule. |
| `image_width` | Positive image width in pixels. |
| `image_height` | Positive image height in pixels. |
| `objects` | Array of detections; empty means no objects, not sensor failure. |
| `objects[]` | One raw detection; array order does not select a target. |
| `objects[].object_id` | Track ID string, scoped to epoch, stream and track_session; not permanent identity. |
| `objects[].label` | Detector class string, not assured semantic identity; untrusted data. |
| `objects[].confidence` | Finite detector confidence in [0,1], not grasp-success probability. |
| `objects[].bbox_xyxy` | [left, top, right, bottom] pixel array, origin top-left, x right, y down; 0<=left<right<=image_width and 0<=top<bottom<=image_height. |
| `objects[].bbox_xyxy[]` | One finite pixel coordinate in the stated ordering. |
| `objects[].depth_m` | Optional positive metric depth in metres; null means unknown, never zero. |
| `objects[].track_session` | Tracker-session ID; reused object IDs start a new lifecycle across sessions. |

A 2D box alone is not a 3D pose or grasp evidence. Missing depth stays unknown. No interpolation across sequence gaps. Selection is a separate target_ref, never a raw detection mutation.
