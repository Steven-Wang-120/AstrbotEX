"""Device-independent voice decisions and bounded conversation state.

Capture, playback, local models and operating-system credential stores belong
in the calling device adapter. These modules never open an audio device.
"""
from astrbot_ex.core.voice.decision import DecisionClient, GateError
from astrbot_ex.core.voice.gate import ConversationGate
from astrbot_ex.core.voice.memory import ConversationMemory

__all__ = ["DecisionClient", "GateError", "ConversationGate", "ConversationMemory"]
