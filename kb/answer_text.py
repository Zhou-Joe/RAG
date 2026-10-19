"""Never publish a model's serialized tool instructions as an answer."""
import re

class AnswerText:
    def __init__(self):
        self.pending = ''

    def feed(self, text, final=False):
        self.pending += text
        if re.search(r'<\s*(?:tool_call|function\s*[=>]|parameter\s*[=>])', self.pending, re.I):
            self.pending = ''
            raise ValueError('模型返回了工具调用标记，未将其作为答案发布。请重试。')
        if final:
            ready, self.pending = self.pending, ''
            return ready
        boundary = self.pending.rfind('\n\n')
        if boundary < 0:
            return ''
        ready, self.pending = self.pending[:boundary+2], self.pending[boundary+2:]
        return ready
