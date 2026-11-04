"""Bounded pending turns and conservative text-based speaker echo filtering."""
from difflib import SequenceMatcher
import queue
import threading
import time


def normalized(text):
    return ''.join(char.lower() for char in text if char.isalnum())


class EchoFilter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.reference = ''
        self.active = False
        self.expires = 0

    def observe(self, message):
        with self.lock:
            if message=='新回答开始':
                self.reference='';self.active=False;self.expires=0
            elif message.startswith('助手片段：'):
                self.reference=(self.reference+normalized(message[5:]))[-4000:]
            elif message.startswith('助手：'):
                self.reference = normalized(message[3:])
            elif message.startswith('正在播报'):
                self.active = True

    def finish(self):
        with self.lock:
            self.active = False
            self.expires = self.clock() + 2

    def is_echo(self, text):
        with self.lock:
            reference = self.reference
            recent = self.active or self.clock() < self.expires
        text = normalized(text)
        if not recent or not reference:
            return False
        if len(text) < 4:
            return text in ('好的', '嗯', '是的', '谢谢', '你好', '可以', '对的', '明白', '没问题') and text in reference
        return text in reference or SequenceMatcher(None, text, reference).ratio() >= 0.8


class PendingTurns:
    def __init__(self):
        self.queue = queue.Queue(maxsize=3)

    def offer(self, pcm, text):
        try:
            self.queue.put_nowait((pcm, text, time.monotonic()))
            return True
        except queue.Full:
            return False

    def take(self, timeout=0.1):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
