"""Apply the chosen UI language without rewriting prompts or stored history."""
from langchain_openai import ChatOpenAI

from .interface_language import response_language_instruction


class InterfaceChatOpenAI(ChatOpenAI):
    interface_language: str = "zh-hans"

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "messages" in payload:
            payload["messages"] = [
                {"role": "system", "content": response_language_instruction(self.interface_language)},
                *payload["messages"],
            ]
        return payload
