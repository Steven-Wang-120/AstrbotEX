"""Nonblocking DEALER transport for the supplied astrbotex-zmq v1 contract.

The real A.E.B. repository is not available here. Handshake payload and publish
payload compatibility must be confirmed against it before claiming deployment.
No private receiver buffer is implemented here: only A.E.B. owns those buffers.
"""
import json
import time
import uuid


def header(channel, method, payload):
    return {"timestamp": time.time(), "protocol": "astrbotex-zmq", "version": 1,
            "channel": channel, "kind": "request", "id": uuid.uuid4().hex,
            "method": method, "payload": payload}


class Channel:
    def __init__(self, context, name, endpoint, client_id, timeout=3, mark_handler=None):
        import zmq
        self.zmq = zmq
        self.name, self.endpoint, self.client_id = name, endpoint, client_id
        self.context, self.timeout = context, timeout
        self.socket, self.request, self.latest = None, None, None
        self.ready = False
        self.error = None
        self.last_ack = None
        self.mark_handler = mark_handler
        self.connect()

    def connect(self):
        if self.socket is not None:
            self.socket.close(linger=0)
        self.socket = self.context.socket(self.zmq.DEALER)
        self.socket.setsockopt(self.zmq.IDENTITY, (self.client_id+":"+self.name).encode())
        self.socket.setsockopt(self.zmq.LINGER, 0)
        self.socket.setsockopt(self.zmq.IMMEDIATE, 1)
        self.socket.setsockopt(self.zmq.SNDHWM, 2)
        self.socket.setsockopt(self.zmq.RCVHWM, 4)
        self.socket.setsockopt(self.zmq.MAXMSGSIZE, 1048576)
        self.socket.connect(self.endpoint)
        self.ready, self.request = False, None

    def send(self, method, payload, binary=None):
        if self.request:
            return False
        message = header(self.name, method, payload)
        frames = [json.dumps(message, ensure_ascii=False, allow_nan=False).encode()]
        if binary is not None:
            frames.append(binary)
        try:
            self.socket.send_multipart(frames, flags=self.zmq.NOBLOCK)
        except self.zmq.Again:
            return False
        self.request = (message, time.monotonic())
        return True

    def tick(self):
        # Bounded work per Actor/ROS tick even if peer sends unsolicited replies.
        for _ in range(4):
            try:
                parts = self.socket.recv_multipart(flags=self.zmq.NOBLOCK)
            except self.zmq.Again:
                break
            try:
                response = json.loads(parts[0])
                if (isinstance(response,dict) and response.get('kind')=='request'
                    and response.get('protocol')=='astrbotex-zmq' and response.get('version')==1
                    and response.get('channel')==self.name and self.ready):
                    try:
                        if self.name!='text' or response.get('method')!='vision.mark.set' or len(parts)!=1:
                            raise ValueError('unsupported request')
                        result=self.mark_handler(response.get('payload'))
                    except Exception as exc:
                        result={'ok':False,'error':str(exc)[:300]}
                    reply=header(self.name,response.get('method',''),result)
                    reply.update(kind='response',reply_to=response.get('id'))
                    try:self.socket.send_multipart([json.dumps(reply).encode()],flags=self.zmq.NOBLOCK)
                    except self.zmq.Again:pass  # Caller retries same application request_id.
                    continue
                if not isinstance(response, dict) or not self.request:
                    continue
                sent = self.request[0]
                if response.get("reply_to") != sent["id"]:
                    continue
                if response.get("protocol") != "astrbotex-zmq" or response.get("version") != 1 or response.get("channel") != self.name:
                    continue
                payload = response.get("payload", {})
                if response.get("kind") != "response" or response.get("error") or (isinstance(payload, dict) and (payload.get("ok") is False or payload.get("error"))):
                    self.error = f"{sent['method']} rejected: {str(response)[:500]}"
                    # Retry on a new connection after a bounded delay.
                    continue
                self.request = None
                self.error = None
                if sent["method"] == "system.hello":
                    self.ready = True
                else:
                    self.last_ack = sent["payload"].get("frame_id")
            except (ValueError, IndexError, TypeError):
                continue
        if self.request and time.monotonic()-self.request[1] > self.timeout:
            self.error = f"{self.name}: acknowledgement timeout"
            self.connect()
        if not self.ready and not self.request:
            self.send("system.hello", {"client_id": self.client_id, "name": "astrbotex_yolov8_seg"})

    def close(self):
        self.socket.close(linger=0)
        self.socket = None


class AEBForwarder:
    def __init__(self, text_endpoint, vision_endpoint, client_id, max_age=2, mark_handler=None):
        import zmq
        self.context = zmq.Context()
        self.max_age, self.latest = max_age, None
        self.text = Channel(self.context, "text", text_endpoint, client_id, mark_handler=mark_handler)
        self.vision = Channel(self.context, "vision", vision_endpoint, client_id)

    def submit(self, packet, jpeg=b""):
        # Explicit allowlist keeps any embedded/base64 frame out of the text channel.
        allowed = ("timestamp", "stream_id", "frame_id", "source_stamp", "source_frame", "width", "height", "status", "error", "mark_rev", "marks", "objects")
        data = {key: packet[key] for key in allowed if key in packet}
        self.latest = (data, jpeg, time.monotonic(), False)

    def tick(self):
        self.text.tick()
        self.vision.tick()
        if not self.latest:
            return
        packet, jpeg, queued, text_sent = self.latest
        if time.monotonic()-queued > self.max_age:
            self.latest = None
            return
        if not self.text.ready or (jpeg and not self.vision.ready):
            return
        if not text_sent:
            if not self.text.send("vision.json.publish", packet):
                return
            text_sent = True
            self.latest = packet, jpeg, queued, True
        if not jpeg:
            self.latest = None
            return
        metadata = {key: packet[key] for key in ("timestamp", "stream_id", "frame_id", "width", "height") if key in packet}
        if self.vision.send("vision.jpeg.publish", metadata, jpeg):
            self.latest = None

    def close(self):
        self.text.close()
        self.vision.close()
        self.context.term()
