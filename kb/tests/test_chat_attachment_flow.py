"""Upload-to-model and private-history integration for EnGenie attachments."""
import asyncio
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image

from kb.models import ChatAttachment, Conversation, Message
from kb.tests.test_conversation_context import ConversationModel


def photo():
    output = BytesIO()
    Image.new('RGB', (20, 10), 'blue').save(output, format='PNG')
    return SimpleUploadedFile('photo.png', output.getvalue(), content_type='image/png')


class ChatAttachmentFlowTests(TransactionTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        configured = override_settings(CHAT_ATTACHMENT_ROOT=self.directory / 'private',
                                       MEDIA_ROOT=self.directory / 'public')
        configured.enable()
        self.addCleanup(configured.disable)
        self.user = get_user_model().objects.create_user('chat-file-owner')
        self.other = get_user_model().objects.create_user('chat-file-other')
        self.client.force_login(self.user)
        self.async_client.cookies = self.client.cookies
        self.model = ConversationModel(responses=[AIMessage(content='The photo is blue.')])

    def ask(self, message='Describe this photo.', *, files=(), thread='chat-attachment-flow', vision=True, enhance=False):
        async def consume():
            payload = {'message': message, 'thread_id': thread}
            if files:
                payload['files'] = list(files)
            response = await self.async_client.post(reverse('kb:stream'), payload)
            if response.status_code != 200:
                return response, response.content.decode()
            body = b''.join([chunk async for chunk in response.streaming_content]).decode()
            return response, body
        with patch('kb.config.llm_settings', return_value={'vision': vision, 'qa_enhance': enhance}), \
                patch('kb.agent._get_llm', return_value=self.model):
            return asyncio.run(consume())

    def test_photo_reaches_multimodal_model_and_compact_context_replays_it(self):
        response, wire = self.ask(files=[photo()])
        self.assertEqual(response.status_code, 200, wire)
        self.assertIn('event: attachments', wire)
        human = next(message for message in self.model.seen[-1] if isinstance(message, HumanMessage))
        self.assertTrue(any(block.get('type') == 'image_url' for block in human.content))
        conversation = Conversation.objects.get(thread_id='chat-attachment-flow')
        serialized = json.dumps(conversation.agent_context)
        self.assertIn('chat_attachment_ids', serialized)
        self.assertNotIn('base64,', serialized)
        self.assertEqual(ChatAttachment.objects.count(), 1)
        response, wire = self.ask('What color was that photo?')
        self.assertEqual(response.status_code, 200, wire)
        self.assertTrue(any(isinstance(message, HumanMessage) and isinstance(message.content, list)
            and any(block.get('type') == 'image_url' for block in message.content)
            for message in self.model.seen[-1]))

    def test_attachment_only_message_and_private_download_survive_reopening(self):
        response, wire = self.ask('', files=[photo()])
        self.assertEqual(response.status_code, 200, wire)
        history = self.client.get(reverse('kb:conv_messages', args=['chat-attachment-flow'])).json()
        attachment = history['messages'][0]['attachments'][0]
        self.assertEqual(attachment['name'], 'photo.png')
        self.assertNotIn('/media/', attachment['url'])
        download = self.client.get(attachment['url'])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Type'], 'image/png')
        self.assertIn('private', download['Cache-Control'])
        download.close()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(attachment['url']).status_code, 404)
        self.assertEqual(self.client.get(reverse('kb:conv_messages', args=['chat-attachment-flow'])).status_code, 404)

    def test_nonvisual_model_rejects_photo_without_persisting_a_turn(self):
        response, _ = self.ask(files=[photo()], vision=False)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChatAttachment.objects.exists())
        self.assertFalse(Conversation.objects.exists())
        self.assertEqual(self.model.seen, [])

    def test_documents_are_context_not_knowledge_base_imports(self):
        document = SimpleUploadedFile('readme.txt', b'The inspection interval is daily.')
        response, wire = self.ask('Summarize this.', files=[document], vision=False)
        self.assertEqual(response.status_code, 200, wire)
        human = next(message for message in self.model.seen[-1] if isinstance(message, HumanMessage))
        self.assertIn('inspection interval is daily', str(human.content))
        self.assertFalse(any(block.get('type') == 'image_url' for block in human.content))
        self.assertEqual(ChatAttachment.objects.get().kind, 'document')

    def test_invalid_later_file_does_not_store_earlier_valid_file(self):
        response, _ = self.ask(files=[photo(), SimpleUploadedFile('script.exe', b'MZ')])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChatAttachment.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertFalse(list((self.directory / 'private').rglob('*')))

    def test_outer_transaction_failure_cleans_already_saved_private_files(self):
        with patch('kb.config.llm_settings', return_value={'vision': True}), \
                patch('kb.config.retrieval_settings', side_effect=RuntimeError('configuration unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('kb:stream'), {
                    'message': 'Describe this.', 'thread_id': 'attachment-rollback', 'files': photo(),
                })
        self.assertFalse(ChatAttachment.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertFalse([path for path in (self.directory / 'private').rglob('*') if path.is_file()])

    def test_failed_answer_can_retry_with_its_attachment_without_reuploading(self):
        async def broken(*args, **kwargs):
            yield 'error', {'message': 'Temporary model failure'}
        with patch('kb.agent.run_agent_stream', side_effect=broken):
            response, wire = self.ask(files=[photo()])
        self.assertEqual(response.status_code, 200, wire)
        self.assertEqual(Conversation.objects.get().messages.get(role='ai').completion_status, 'incomplete')
        response, wire = self.ask('Please try again.')
        self.assertEqual(response.status_code, 200, wire)
        self.assertTrue(any(isinstance(message, HumanMessage) and isinstance(message.content, list)
            and any(block.get('type') == 'image_url' for block in message.content)
            for message in self.model.seen[-1]))

    def test_deleting_conversation_removes_its_private_attachment(self):
        response, wire = self.ask(files=[photo()])
        self.assertEqual(response.status_code, 200, wire)
        path = Path(ChatAttachment.objects.get().file.path)
        response = self.client.post(reverse('kb:conv_delete', args=['chat-attachment-flow']),
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(path.exists())
        self.assertFalse(ChatAttachment.objects.exists())

    def test_enhanced_photo_answer_is_checked_against_pixels_and_published(self):
        verifier = AsyncMock()
        verifier.ainvoke.return_value = AIMessage(content='{"verdict":"pass","issues":[]}')
        with patch('kb.qa_steps.build_llm', return_value=verifier), \
                patch('kb.qa_steps.plan_query', new_callable=AsyncMock) as planner:
            response, wire = self.ask(files=[photo()], enhance=True)
        self.assertEqual(response.status_code, 200, wire)
        self.assertIn('The photo is blue.', wire)
        answer = Message.objects.get(role='ai')
        self.assertEqual(answer.completion_status, 'complete')
        self.assertTrue(answer.verified)
        content = verifier.ainvoke.call_args.args[0][0]['content']
        self.assertTrue(any(block.get('type') == 'image_url' for block in content))
        planner.assert_not_called()

