"""Execution mode for validated primitive actions supplied by the trusted upper route."""


class StructuredBackend:
    primitive_only = True
    structured_only = True

    def __init__(self):
        self._closed = False

    @property
    def execution_allowed(self):
        return not self._closed

    def supports_instruction_input(self):
        return not self._closed

    def probe(self):
        return {"ok": not self._closed, "model": "structured", "probe_supported": True,
                "network_called": False, "inference_called": False, "inference_ok": False,
                "error_code": "closed" if self._closed else None}

    def decide(self, snapshot):
        raise ValueError("structured_action_required")

    def cancel(self):
        pass

    def close(self):
        self._closed = True
