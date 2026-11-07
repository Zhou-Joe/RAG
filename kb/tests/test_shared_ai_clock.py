import asyncio
import json
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase,override_settings
from langchain_core.messages import HumanMessage,SystemMessage
from fos_rag.ai_clock import clock_context,with_runtime_clock


@override_settings(TIME_ZONE='Asia/Shanghai')
class SharedClockTests(SimpleTestCase):
    def test_request_clock_is_current_local_time_and_does_not_modify_saved_prefix(self):
        history=[{'role':'system','content':'Instructions'},{'role':'user','content':'Document date: 2005-01-01'}]
        with patch('fos_rag.ai_clock.timezone.now',return_value=datetime(2026,11,7,16,1,tzinfo=timezone.utc)):
            request=with_runtime_clock(history)
        self.assertEqual(len(history),2);self.assertEqual(request[:-1],history)
        self.assertIn('2026-11-08',request[-1]['content']);self.assertIn('+08:00',request[-1]['content'])
        self.assertIn('Asia/Shanghai',request[-1]['content']);self.assertIn('2005-01-01',request[1]['content'])

    def test_clock_refresh_does_not_accumulate_messages_or_rewrite_cached_context(self):
        history=[{'role':'user','content':'What time is it?'}]
        with patch('fos_rag.ai_clock.timezone.now',return_value=datetime(2026,11,7,15,59,tzinfo=timezone.utc)):first=with_runtime_clock(history)
        with patch('fos_rag.ai_clock.timezone.now',return_value=datetime(2026,11,7,16,1,tzinfo=timezone.utc)):second=with_runtime_clock(history)
        self.assertEqual(len(first),2);self.assertEqual(len(second),2);self.assertEqual(len(history),1)
        self.assertIn('2026-11-07',first[-1]['content']);self.assertIn('2026-11-08',second[-1]['content'])

    def test_langchain_request_boundary_refreshes_both_system_and_developer_models(self):
        from kb.clocked_chat import ClockedChatOpenAI
        for name,role in [('local-model','system'),('o3-mini','developer')]:
            model=ClockedChatOpenAI(model=name,api_key='fixture',base_url='http://localhost:1234/v1')
            self.addCleanup(model.root_client.close)
            messages=[SystemMessage(content='Known context'),HumanMessage(content='Today?')]
            with patch('fos_rag.ai_clock.timezone.now',return_value=datetime(2026,11,7,12,tzinfo=timezone.utc)):
                payload=model._get_request_payload(messages)
            self.assertEqual(len(messages),2);self.assertEqual(len(payload['messages']),3)
            self.assertEqual(payload['messages'][-1]['role'],role)
            self.assertIn('2026-11-07',payload['messages'][-1]['content'])
            self.assertEqual(payload['messages'][1]['content'],'Today?')

    def test_vehicle_and_operations_isolated_requests_receive_the_same_clock(self):
        from fos_rag.isolated_model import call_model
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'content':'Answer'}))
        history=[{'role':'system','content':'Module instructions'},{'role':'user','content':'Today?'}]
        with patch('fos_rag.ai_clock.timezone.now',return_value=datetime(2026,11,7,12,tzinfo=timezone.utc)),patch('fos_rag.isolated_model.subprocess.run',return_value=response) as worker:
            self.assertEqual(call_model({},history),'Answer')
        sent=json.loads(worker.call_args.kwargs['input'])['messages']
        self.assertEqual(sent[:-1],history);self.assertEqual(len(history),2)
        self.assertIn('2026-11-07',sent[-1]['content']);self.assertIn('Asia/Shanghai',sent[-1]['content'])
