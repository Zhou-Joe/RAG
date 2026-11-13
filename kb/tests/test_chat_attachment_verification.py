"""Attachment answers are checked against the actual private upload content."""
import asyncio
import base64
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from PIL import Image

from kb.chat_attachments import evidence_for_attachments, prepare_uploads, save_attachments
from kb.models import Conversation, Message
from kb.publication import validate_sources
from kb.qa_steps import verify_answer


class AttachmentVerifierTests(SimpleTestCase):
    def snapshot(self, text=''):
        return {'kind': 'chat_attachment', 'attachment_id': 'attachment-1',
                'conversation_id': 'conversation-1', 'digest': 'verified-source-digest',
                'source': 'inspection.png', 'text': text}

    def model(self):
        return SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
            content='{"verdict":"pass","issues":[]}', response_metadata={})))

    def test_actual_image_and_text_blocks_reach_the_verifier(self):
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,REAL_IMAGE'}}
        original = {'type': 'text', 'text': 'Original uploaded text: pressure is 12 bar.'}
        snapshot = self.snapshot('pressure is 12 bar')
        model = self.model()
        with patch('kb.qa_steps.build_llm', return_value=model), patch(
            'kb.chat_attachments.snapshot_current', return_value=True,
        ) as current, patch('kb.chat_attachments.verification_blocks', return_value=[original, image]) as blocks:
            verdict, _ = asyncio.run(verify_answer({'vision': True}, 'Read the gauge',
                'The gauge reads 12 bar.', [snapshot], user_id=17))
        self.assertEqual(verdict['verdict'], 'pass')
        current.assert_called_once_with(snapshot, 17)
        blocks.assert_called_once_with(snapshot, 17)
        content = model.ainvoke.call_args.args[0][0]['content']
        self.assertIn(image, content)
        self.assertIn(original, content)
        self.assertIn('inspection.png', content[0]['text'])

    def test_missing_or_revoked_attachment_never_reaches_the_model(self):
        model = self.model()
        for user_id, accessible in ((None, True), (17, False)):
            with self.subTest(user_id=user_id, accessible=accessible), patch(
                'kb.qa_steps.build_llm', return_value=model,
            ), patch('kb.chat_attachments.snapshot_current', return_value=accessible), patch(
                'kb.chat_attachments.verification_blocks', return_value=[{'type': 'text', 'text': 'secret'}],
            ) as blocks:
                verdict, _ = asyncio.run(verify_answer({'vision': True}, 'q', 'answer',
                    [self.snapshot()], user_id=user_id))
                self.assertIsNone(verdict)
                blocks.assert_not_called()
        model.ainvoke.assert_not_called()

    def test_document_budget_supports_long_uploads_without_relaxing_kb_evidence(self):
        text = 'Original document text\n' * 1500
        snapshot = self.snapshot(text)
        model = self.model()
        with patch('kb.qa_steps.build_llm', return_value=model), patch(
            'kb.chat_attachments.snapshot_current', return_value=True,
        ), patch('kb.chat_attachments.verification_blocks', return_value=[{'type': 'text', 'text': text}]):
            verdict, _ = asyncio.run(verify_answer({}, 'q', 'answer', [snapshot], user_id=17))
        self.assertEqual(verdict['verdict'], 'pass')
        content = model.ainvoke.call_args.args[0][0]['content']
        self.assertEqual(sum(block.get('text') == text for block in content), 1)
        for evidence in ([self.snapshot('x' * 120001)],
                         [snapshot, {'kind': 'text', 'text': 'x' * 24001}]):
            with self.subTest(evidence_lengths=[len(item['text']) for item in evidence]), patch(
                'kb.qa_steps.build_llm', return_value=model,
            ) as build:
                verdict, _ = asyncio.run(verify_answer({}, 'q', 'answer', evidence, user_id=17))
                self.assertIsNone(verdict)
                build.assert_not_called()

    def test_photos_require_vision_even_when_a_snapshot_is_valid(self):
        model = self.model()
        with patch('kb.qa_steps.build_llm', return_value=model), patch(
            'kb.chat_attachments.snapshot_current', return_value=True,
        ), patch('kb.chat_attachments.verification_blocks', return_value=[
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,REAL_IMAGE'}}]):
            verdict, _ = asyncio.run(verify_answer({}, 'q', 'answer', [self.snapshot()], user_id=17))
        self.assertIsNone(verdict)
        model.ainvoke.assert_not_called()

    def test_text_only_verifier_call_keeps_its_existing_shape(self):
        model = self.model()
        with patch('kb.qa_steps.build_llm', return_value=model), patch(
            'kb.chat_attachments.snapshot_current', side_effect=AssertionError('not an attachment'),
        ):
            verdict, _ = asyncio.run(verify_answer({}, 'q', 'answer', [{'kind': 'text', 'text': 'source'}]))
        self.assertEqual(verdict['verdict'], 'pass')
        self.assertIsInstance(model.ainvoke.call_args.args[0][0]['content'], str)


class AttachmentPublicationTests(TestCase):
    def test_publication_delegates_attachment_owner_and_digest_validation(self):
        user = get_user_model().objects.create_user('attachment-verification-owner')
        snapshot = {'kind': 'chat_attachment', 'attachment_id': 'id',
                    'conversation_id': 'conversation', 'digest': 'fingerprint', 'source': 'photo.png', 'text': ''}
        for current in (True, False):
            with self.subTest(current=current), patch(
                'kb.chat_attachments.snapshot_current', return_value=current,
            ) as source_check:
                self.assertEqual(validate_sources([snapshot], user.pk), current)
                source_check.assert_called_once_with(snapshot, user.pk)
        with patch('kb.chat_attachments.snapshot_current') as source_check:
            user.is_active = False
            user.save(update_fields=['is_active'])
            self.assertFalse(validate_sources([snapshot], user.pk))
            source_check.assert_not_called()


class PrivateAttachmentSourceTests(TransactionTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        settings = override_settings(MEDIA_ROOT=root / 'media', CHAT_ATTACHMENT_ROOT=root / 'private')
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_user('private-attachment-owner')
        self.other = get_user_model().objects.create_user('other-attachment-owner')
        self.conversation = Conversation.objects.create(user=self.user, thread_id='private-attachment-review')
        self.message = Message.objects.create(conversation=self.conversation, role='user', content='Read this upload')

    def attach(self, name, payload, *, vision=False):
        return save_attachments(self.message, prepare_uploads([
            SimpleUploadedFile(name, payload),
        ], vision=vision))[0]

    def evidence(self, attachment):
        return evidence_for_attachments([str(attachment.pk)], user_id=self.user.pk,
            conversation_id=str(self.conversation.pk))

    def test_real_owner_conversation_and_file_changes_revoke_source(self):
        attachment = self.attach('inspection.txt', b'pressure: 12 bar')
        evidence = self.evidence(attachment)
        self.assertTrue(validate_sources(evidence, self.user.pk))
        self.assertFalse(validate_sources(evidence, self.other.pk))
        changed_digest = deepcopy(evidence)
        changed_digest[0]['digest'] = 'different-file'
        self.assertFalse(validate_sources(changed_digest, self.user.pk))
        changed_conversation = deepcopy(evidence)
        changed_conversation[0]['conversation_id'] = str(Conversation.objects.create(
            user=self.user, thread_id='other-private-attachment-review').pk)
        self.assertFalse(validate_sources(changed_conversation, self.user.pk))
        Path(attachment.file.path).write_bytes(b'pressure: 99 bar')
        self.assertFalse(validate_sources(evidence, self.user.pk))

    def test_real_photo_bytes_reach_the_verifier(self):
        source = BytesIO()
        Image.new('RGB', (20, 12), 'blue').save(source, format='PNG')
        attachment = self.attach('inspection.png', source.getvalue(), vision=True)
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
            content='{"verdict":"pass","issues":[]}', response_metadata={})))
        with patch('kb.qa_steps.build_llm', return_value=model):
            verdict, _ = asyncio.run(verify_answer({'vision': True}, 'What color is this?',
                'Blue.', self.evidence(attachment), user_id=self.user.pk))
        self.assertEqual(verdict['verdict'], 'pass')
        content = model.ainvoke.call_args.args[0][0]['content']
        images = [block['image_url']['url'] for block in content if block['type'] == 'image_url']
        self.assertEqual(len(images), 1)
        decoded = base64.b64decode(images[0].split(',', 1)[1])
        with Image.open(BytesIO(decoded)) as image:
            self.assertEqual(image.size, (20, 12))
