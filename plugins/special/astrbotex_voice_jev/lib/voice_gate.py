"""Session-aware voice addressing. History is evidence, never speaker identity."""
from collections import deque
import math
import re
import threading
import time

from decision_client import GateError


class ConversationGate:
    def __init__(self, client, threshold=0.85, followup_seconds=25, clock=time.monotonic,
                 forward_threshold=None, quiet_threshold=0.85):
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise ValueError('Gate threshold must be 0 to 1')
        if not math.isfinite(followup_seconds) or not 1 <= followup_seconds <= 120:
            raise ValueError('Follow-up window must be 1 to 120 seconds')
        self.client = client
        self.threshold = threshold
        self.forward_threshold = threshold if forward_threshold is None else forward_threshold
        if isinstance(self.forward_threshold, bool) or not isinstance(self.forward_threshold, (int, float)) or not 0 <= self.forward_threshold <= 1:
            raise ValueError('Forward threshold must be 0 to 1')
        if isinstance(quiet_threshold, bool) or not isinstance(quiet_threshold, (int, float)) or not 0 <= quiet_threshold <= 1:
            raise ValueError('Quiet threshold must be 0 to 1')
        self.quiet_threshold = quiet_threshold
        self.followup_seconds = followup_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self.history = deque(maxlen=6)
        self.overheard = deque(maxlen=6)
        self.last_address = None
        self.quiet = False

    def set_thresholds(self, threshold, forward_threshold=None):
        forward_threshold = threshold if forward_threshold is None else forward_threshold
        for value in (threshold, forward_threshold):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError('对话判定阈值必须在0～1之间。')
        with self.lock:
            self.threshold = threshold
            self.forward_threshold = forward_threshold

    def state(self, text, captured_at=None):
        now = self.clock() if captured_at is None else captured_at
        with self.lock:
            active = self.last_address is not None and 0 <= now-self.last_address <= self.followup_seconds
            recent = [{'user': item['user'], 'assistant': item['assistant']} for item in self.history
                      if 0 <= now-item['at'] <= 180][-3:]
            background = [item[1] for item in self.overheard if 0 <= now-item[0] <= 120][-4:]
            return {'latest_utterance': text,
                    'conversation_mode': 'open_domain_voice_conversation',
                    'recent_assistant_exchange': recent, 'recent_background_speech': background,
                    'active_exchange': active, 'quiet_requested': self.quiet,
                    'speaker_identity_and_gaze': 'unknown; text-only judgment; do not assume same speaker'}

    def reported_quote_only(self, text):
        # A whole reported quotation is not a direct address, regardless of the quoted
        # words. Outer requests before or after it are excluded.
        prefix = re.split(r'[“"「『]', text, maxsplit=1)[0]
        if re.search(r'解释|翻译|分析|说明|怎么理解|帮我|告诉我|请问', prefix):
            return False
        return re.fullmatch(r'[^：:“”"「」『』]{1,24}(?:说|问|说道|问道)[：:]?[“"「『].+[”"」』][。.!！?？]?', text.strip()) is not None

    def local_quiet(self, text, captured_at=None):
        """Only an entire direct silence command; never a mention or quotation."""
        compact = re.sub(r'[\s，,。.!！?？：:]', '', text)
        active = self.state(text, captured_at)['active_exchange']
        return active and re.fullmatch(
            r'(?:请)?(?:你)?(?:现在)?(?:先)?(?:别回答|不要回答|不用回答|别说话|不要说话|别说|别讲|停止播报|暂停播报|停止说话|停止回答|安静一下|安静|保持安静|闭嘴|听着就行|听着就好)(?:了|吧|好吗)?(?:听着就行|听着就好)?', compact) is not None

    def evaluate(self, text, captured_at=None, cancel_event=None, commit=True):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise GateError('Voice decision needs 1 to 4000 characters')
        if cancel_event is not None and cancel_event.is_set():
            raise GateError('Decision cancelled')
        state = self.state(text, captured_at)
        local_quiet = self.local_quiet(text, captured_at)
        reported = self.reported_quote_only(text)
        scores = ({'addressed': 0.0, 'forward': 0.0, 'quiet': 0.0} if reported else
                  ({'addressed': 1.0, 'forward': 0.0, 'quiet': 1.0} if local_quiet else
                   self.client.evaluate(state, cancel_event=cancel_event)))
        if cancel_event is not None and cancel_event.is_set():
            raise GateError('Decision cancelled')
        # Clients must provide all three probabilities, even when supplied by an adapter.
        for name in ('addressed', 'forward', 'quiet'):
            score = scores.get(name)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise GateError('Invalid decision score')
        with self.lock:
            thresholds = {'addressed': self.threshold, 'forward': self.forward_threshold, 'quiet': self.quiet_threshold}
        addressed = scores['addressed'] > 0 and scores['addressed'] >= thresholds['addressed']
        quiet = addressed and scores['quiet'] > 0 and scores['quiet'] >= thresholds['quiet']
        allowed = addressed and scores['forward'] > 0 and scores['forward'] >= thresholds['forward'] and not quiet
        result = {'allowed': allowed, 'quiet': quiet, 'scores': scores,
                  'reason': 'quiet' if quiet else ('addressed' if allowed else 'background'),
                  'thresholds': thresholds,
                  'captured_at': self.clock() if captured_at is None else captured_at}
        if commit:
            self.commit(text, result)
        return result

    def commit(self, text, result):
        now = result['captured_at']
        with self.lock:
            if result['quiet']:
                self.quiet = True
                self.last_address = None
            elif result['allowed']:
                self.quiet = False
                self.last_address = now
                self.history.append({'user': text[:1200], 'assistant': '', 'at': now})
            else:
                self.overheard.append((now, text[:1000]))

    def answered(self, user, answer, captured_at=None, finished=True):
        with self.lock:
            matched = False
            for item in reversed(self.history):
                if item['user'] == user[:1200] and (captured_at is None or item['at'] == captured_at):
                    item['assistant'] = answer[:1600]
                    matched = True
                    break
            # A reply being spoken extends a real exchange, never revives a quiet request.
            if matched and not self.quiet and finished:
                self.last_address = self.clock()

    def reset(self):
        with self.lock:
            self.history.clear()
            self.overheard.clear()
            self.last_address = None
            self.quiet = False
