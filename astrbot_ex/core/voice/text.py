"""Incremental text shaping shared by speech adapters."""
import re


class SentenceBuffer:
    def __init__(self) -> None:
        self.text = ""
        self.emitted = False

    def feed(self, chunk: str) -> list[str]:
        self.text += chunk
        result = []
        while self.text:
            end = next(
                (i + 1 for i, char in enumerate(self.text)
                 if (char in "。！？!?\n" and i >= 2)
                 or (not self.emitted and char in "，,；;" and i >= 11)),
                None,
            )
            if end is None and len(self.text) < 80:
                break
            end = end or 80
            result.append(self.text[:end])
            self.text = self.text[end:]
            self.emitted = True
        return result

    def finish(self) -> list[str]:
        value = self.text.strip()
        self.text = ""
        return [value] if value else []


def spoken_text(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+)", "", text)
    return text.replace("**", "").replace("`", "").strip()
