from langchain_openai import ChatOpenAI
from fos_rag.ai_clock import with_runtime_clock


class ClockedChatOpenAI(ChatOpenAI):
    """Refresh time for sync/async, streamed and non-streamed OpenAI-compatible calls."""
    def _get_request_payload(self,input_,*,stop=None,**kwargs):
        payload=super()._get_request_payload(input_,stop=stop,**kwargs)
        if 'messages' in payload:
            role='developer' if any(message.get('role')=='developer' for message in payload['messages']) else 'system'
            payload['messages']=with_runtime_clock(payload['messages'],role=role)
        return payload
