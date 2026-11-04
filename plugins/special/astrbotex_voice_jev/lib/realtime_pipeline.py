"""Incremental text -> bounded TTS producer -> live PCM playback."""
import queue
import re
import threading
import time

from audio_playback import play_pcm_chunks
from mimo_client import stream_pcm


class SentenceBuffer:
    def __init__(self):
        self.text = ''
        self.emitted = False
    def feed(self, chunk):
        self.text+=chunk
        result=[]
        while self.text:
            end=next((i+1 for i,c in enumerate(self.text)
                      if (c in '。！？!?\n' and i>=2) or
                      (not self.emitted and c in '，,；;' and i>=11)),None)
            if end is None and len(self.text)<80:break
            end=end or 80
            result.append(self.text[:end]);self.text=self.text[end:];self.emitted=True
        return result
    def finish(self):
        value=self.text.strip();self.text=''
        return [value] if value else []


def spoken_text(text):
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*]\s+)', '', text)
    return text.replace('**', '').replace('`', '').strip()


def speak_answer(chat, words, settings, root, device, stop_event, notify,
                 stream_tts=stream_pcm, player=play_pcm_chunks):
    if stop_event.is_set():return {}
    text_queue=queue.Queue(maxsize=16)
    audio_queue=queue.Queue(maxsize=48)
    cancel=threading.Event()
    metrics={'started':time.perf_counter()}
    notify('新回答开始')
    def stopped():return stop_event.is_set() or cancel.is_set()
    class Cancel:
        def is_set(self):return stopped()
    token=Cancel()
    def put(target,item):
        while not stopped():
            try:target.put(item,timeout=.1);return
            except queue.Full:pass
    def generate():
        buffer=SentenceBuffer()
        def on_text(text):
            metrics.setdefault('first_text',time.perf_counter())
            for sentence in buffer.feed(text):put(text_queue,('text',sentence))
        try:
            answer=chat.ask(words,on_text=on_text,cancel_event=token)
            for sentence in buffer.finish():put(text_queue,('text',sentence))
            notify('完整回答：'+answer)
            put(text_queue,('done',None))
        except Exception as exc:put(text_queue,('error',exc))
    def synthesize():
        try:
            while not stopped():
                try:kind,item=text_queue.get(timeout=.1)
                except queue.Empty:continue
                if kind=='done':break
                if kind=='error':raise item
                notify('助手片段：'+item)
                spoken = spoken_text(item)
                if not spoken:
                    continue
                for pcm in stream_tts(settings,root,spoken,cancel_event=token):
                    metrics.setdefault('first_pcm',time.perf_counter())
                    for i in range(0,len(pcm),4096):put(audio_queue,('audio',pcm[i:i+4096]))
            put(audio_queue,('done',None))
        except Exception as exc:put(audio_queue,('error',exc))
    threads=[threading.Thread(target=generate,daemon=True),threading.Thread(target=synthesize,daemon=True)]
    for thread in threads:thread.start()
    def chunks():
        while not stopped():
            try:kind,item=audio_queue.get(timeout=.1)
            except queue.Empty:continue
            if kind=='done':return
            if kind=='error':raise item
            yield item
    def first_audio():
        metrics['first_audio']=time.perf_counter()
        notify('正在播报；首段播放等待 {:.2f} 秒'.format(metrics['first_audio']-metrics['started']))
    try:
        player(chunks(),device,token,on_first_audio=first_audio)
        return {key:round(value-metrics['started'],3) for key,value in metrics.items() if key!='started'}
    finally:
        cancel.set()
        for thread in threads:thread.join(timeout=.2)
