"""Clearing chat history deletes only the authenticated user's private data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from kb import access
from kb.models import ChatAttachment, Conversation, KnowledgeBase, Message


class ConversationClearTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('clear-owner')
        self.other = get_user_model().objects.create_user('clear-other')
        self.kb = KnowledgeBase.objects.create(
            name='Restricted source', slug='clear-restricted', department='Restricted',
        )
        self.chat = Conversation.objects.create(user=self.owner, thread_id='clear-general')
        self.legacy = Conversation.objects.create(
            user=self.owner, kb=self.kb, thread_id='clear-hidden', agent_context={'messages': ['private']},
        )
        self.other_chat = Conversation.objects.create(user=self.other, thread_id='clear-other-chat')
        self.queued = Message.objects.create(
            conversation=self.chat, role='ai', content='', completion_status='queued',
        )
        self.running = Message.objects.create(
            conversation=self.legacy, role='ai', content='partial', completion_status='running',
        )
        self.other_answer = Message.objects.create(
            conversation=self.other_chat, role='ai', content='other answer', completion_status='running',
        )
        self.client.force_login(self.owner)
        self.url = reverse('kb:conv_clear')

    def test_clears_all_own_chats_including_hidden_history_and_stops_only_own_answers(self):
        self.assertFalse(access.conversation_accessible(self.owner, self.legacy))
        with patch('kb.answer_tasks.stop') as stop:
            response = self.client.post(self.url, {'user_id': self.other.pk})
        self.assertEqual(response.json(), {'ok': True, 'deleted': 2})
        self.assertCountEqual(stop.call_args_list, [call(self.queued.pk), call(self.running.pk)])
        self.assertFalse(Conversation.objects.filter(user=self.owner).exists())
        self.assertFalse(Message.objects.filter(conversation__user=self.owner).exists())
        self.assertTrue(Conversation.objects.filter(pk=self.other_chat.pk).exists())
        self.other_answer.refresh_from_db()
        self.assertEqual(self.other_answer.content, 'other answer')
        self.assertEqual(self.other_answer.completion_status, 'running')
        self.assertTrue(KnowledgeBase.objects.filter(pk=self.kb.pk).exists())

    def test_even_administrators_clear_only_their_own_history(self):
        self.owner.is_staff = True
        self.owner.is_superuser = True
        self.owner.save(update_fields=['is_staff', 'is_superuser'])
        self.assertEqual(self.client.post(self.url).json()['deleted'], 2)
        self.assertTrue(Conversation.objects.filter(pk=self.other_chat.pk).exists())

    def test_repeated_clear_is_successful_with_an_empty_history(self):
        self.assertEqual(self.client.post(self.url).json()['deleted'], 2)
        with patch('kb.answer_tasks.stop') as stop:
            response = self.client.post(self.url)
        self.assertEqual(response.json(), {'ok': True, 'deleted': 0})
        stop.assert_not_called()

    def test_requires_post_and_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(Conversation.objects.filter(user=self.owner).count(), 2)

    def test_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url).status_code, 403)
        self.assertEqual(Conversation.objects.filter(user=self.owner).count(), 2)

    def test_attachment_files_are_removed_after_commit_without_touching_other_users(self):
        with TemporaryDirectory() as directory, override_settings(CHAT_ATTACHMENT_ROOT=directory):
            attachments = []
            for message in (self.queued, self.other_answer):
                attachments.append(ChatAttachment.objects.create(
                    message=message, original_name='note.txt', kind='document',
                    content_type='text/plain', size=4, digest='test',
                    file=SimpleUploadedFile('note.txt', b'note', content_type='text/plain'),
                ))
            mine, theirs = attachments
            own_path, other_path = Path(mine.file.path), Path(theirs.file.path)
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(own_path.exists())
            self.assertFalse(ChatAttachment.objects.filter(pk=mine.pk).exists())
            self.assertTrue(other_path.exists())
            self.assertTrue(ChatAttachment.objects.filter(pk=theirs.pk).exists())

    def test_history_sidebar_exposes_clear_action_with_reversed_endpoint(self):
        response = self.client.get(reverse('kb:ask'), {'embed': '1'})
        self.assertContains(response, 'id="clearChatHistoryBtn"')
        self.assertContains(response, f"fetch('{self.url}'")
