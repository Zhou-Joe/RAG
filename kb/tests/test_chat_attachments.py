"""Private chat uploads reach the model as real image or document content."""
import base64
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from PIL import Image

from kb.chat_attachments import (
    attachment_info, compact_messages, evidence_for_attachments, hydrate_messages,
    prepare_uploads, save_attachments, snapshot_current, verification_blocks,
    validate_conversation_context,
)
from kb.models import ChatAttachment, Conversation, Message


class ChatAttachmentTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / 'private'
        self.media = self.root / 'media'
        config = override_settings(CHAT_ATTACHMENT_ROOT=self.private, MEDIA_ROOT=self.media)
        config.enable()
        self.addCleanup(config.disable)
        self.owner = get_user_model().objects.create_user('attachment-owner')
        self.other = get_user_model().objects.create_user('attachment-other')
        self.conversation = Conversation.objects.create(user=self.owner, thread_id='private-attachment-chat')
        self.other_conversation = Conversation.objects.create(user=self.other, thread_id='other-attachment-chat')
        self.message = Message.objects.create(conversation=self.conversation, role='user', content='Please inspect this.')

    def image_upload(self, *, name='photo.png', size=(60, 40), format='PNG'):
        stream = BytesIO()
        image = Image.new('RGB', size, '#4499cc')
        image.save(stream, format=format)
        image.close()
        return SimpleUploadedFile(name, stream.getvalue(), content_type='application/octet-stream')

    def pdf_upload(self, *, name='source.pdf', pages=1, include_image=False):
        import pymupdf
        image = self.image_upload().read() if include_image else None
        book = pymupdf.open()
        for index in range(pages):
            page = book.new_page()
            page.insert_text((70, 70), f'Inspection page {index + 1}. Pressure is 42 kPa.')
            if image:
                page.insert_image(pymupdf.Rect(70, 100, 170, 160), stream=image)
        payload = book.tobytes()
        book.close()
        return SimpleUploadedFile(name, payload)

    def save_next_message(self, uploads, *, vision=True):
        message = Message.objects.create(conversation=self.conversation, role='user', content='Next attachment turn.')
        return save_attachments(message, prepare_uploads(uploads, vision=vision))

    def save(self, uploads, *, vision=True):
        prepared = prepare_uploads(uploads, vision=vision)
        return save_attachments(self.message, prepared)

    def human(self, attachments, text='Please inspect this.'):
        return HumanMessage(content=text, additional_kwargs={
            'chat_attachment_ids': [str(item.pk) for item in attachments],
            'custom_provider_value': 'retain',
        })

    def hydrate(self, messages, **overrides):
        return hydrate_messages(messages, user_id=overrides.get('user_id', self.owner.pk),
                                conversation_id=overrides.get('conversation_id', self.conversation.pk),
                                vision=overrides.get('vision', True))

    def test_text_attachment_preserves_unicode_and_is_stored_outside_public_media(self):
        original = '设备 A：检查结果\nCafé — 原始说明\n'.encode('utf-8')
        attachment, = self.save([SimpleUploadedFile('notes.txt', original)], vision=False)
        self.assertEqual(attachment.kind, 'document')
        self.assertEqual(attachment.original_name, 'notes.txt')
        self.assertIn('设备 A：检查结果', attachment.extracted_text)
        self.assertTrue(Path(attachment.file.path).resolve().is_relative_to(self.private.resolve()))
        self.assertFalse(Path(attachment.file.path).resolve().is_relative_to(self.media.resolve()))
        with attachment.file.open('rb') as source:
            self.assertEqual(source.read(), original)
        info = attachment_info(attachment)
        self.assertEqual(info['id'], str(attachment.pk))
        self.assertEqual(info['name'], 'notes.txt')
        self.assertEqual(info['kind'], 'document')
        self.assertIn('url', info)
        self.assertNotIn('/media/', info['url'])

    def test_image_is_decoded_normalized_and_sent_as_real_multimodal_block(self):
        attachment, = self.save([self.image_upload(size=(2400, 1200))])
        self.assertEqual(attachment.kind, 'image')
        human = self.human([attachment], 'Describe this photo.')
        hydrated = self.hydrate([human])
        self.assertEqual(len(hydrated), 1)
        self.assertIsInstance(hydrated[0].content, list)
        images = [block for block in hydrated[0].content if block.get('type') == 'image_url']
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]['image_url']['url'].startswith('data:image/'))
        self.assertIn(';base64,', images[0]['image_url']['url'])
        encoded = images[0]['image_url']['url'].split(',', 1)[1]
        with Image.open(BytesIO(base64.b64decode(encoded))) as image:
            self.assertLessEqual(max(image.size), 1536)
        self.assertEqual(hydrated[0].additional_kwargs['custom_provider_value'], 'retain')
        self.assertEqual(human.content, 'Describe this photo.')

    def test_compact_transcript_keeps_ids_and_text_without_base64_and_rehydrates(self):
        attachment, = self.save([self.image_upload()])
        user = self.human([attachment], 'What is visible here?')
        messages = [SystemMessage(content='System context'), user, AIMessage(content='A blue rectangle.')]
        hydrated = self.hydrate(messages)
        compact = compact_messages(hydrated)
        self.assertEqual(compact[1].content, user.content)
        self.assertEqual(compact[1].additional_kwargs['chat_attachment_ids'], [str(attachment.pk)])
        self.assertNotIn('base64,', str(compact))
        replay = self.hydrate(compact)
        self.assertTrue(any(block.get('type') == 'image_url' for block in replay[1].content))
        self.assertEqual(replay[0].content, messages[0].content)
        self.assertEqual(replay[-1].content, messages[-1].content)

    def test_document_content_reaches_model_and_is_not_only_a_filename(self):
        attachment, = self.save([SimpleUploadedFile('notes.md', 'Valve pressure is 42 kPa.\n'.encode())], vision=False)
        hydrated = self.hydrate([self.human([attachment])], vision=False)
        content = hydrated[0].content
        text = content if isinstance(content, str) else '\n'.join(block.get('text', '') for block in content)
        self.assertIn('Valve pressure is 42 kPa.', text)
        self.assertIn('notes.md', text)

    def test_attachments_are_bound_to_both_owner_and_conversation(self):
        attachment, = self.save([self.image_upload()])
        human = self.human([attachment])
        for overrides in (
            {'user_id': self.other.pk},
            {'conversation_id': self.other_conversation.pk},
            {'user_id': self.other.pk, 'conversation_id': self.other_conversation.pk},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.hydrate([human], **overrides)

    def test_missing_attachment_or_deleted_image_is_not_silently_omitted(self):
        missing = HumanMessage(content='Inspect it.', additional_kwargs={'chat_attachment_ids': [str(uuid4())]})
        with self.assertRaises(ValueError):
            self.hydrate([missing])
        attachment, = self.save([self.image_upload()])
        Path(attachment.file.path).unlink()
        with self.assertRaises(ValueError):
            self.hydrate([self.human([attachment])])

    def test_text_only_models_refuse_images_in_new_and_replayed_turns(self):
        with self.assertRaises(ValueError):
            prepare_uploads([self.image_upload()], vision=False)
        attachment, = self.save([self.image_upload()])
        with self.assertRaises(ValueError):
            self.hydrate([self.human([attachment])], vision=False)

    def test_untrusted_extensions_and_fake_images_fail_without_persisting(self):
        for upload in (
            SimpleUploadedFile('payload.svg', b'<svg><script>alert(1)</script></svg>'),
            SimpleUploadedFile('payload.exe', b'MZ executable'),
            SimpleUploadedFile('photo.png', b'not actually an image', content_type='image/png'),
        ):
            with self.subTest(name=upload.name), self.assertRaises(ValueError):
                prepare_uploads([upload], vision=True)
        self.assertFalse(ChatAttachment.objects.exists())
        self.assertFalse(self.private.exists() and any(self.private.rglob('*')))

    def test_file_count_and_individual_size_limits_fail_before_storage(self):
        with self.assertRaises(ValueError):
            prepare_uploads([SimpleUploadedFile(f'{i}.txt', b'ok') for i in range(6)], vision=True)
        with self.assertRaises(ValueError):
            prepare_uploads([SimpleUploadedFile('large.txt', b'x' * (10 * 1024 * 1024 + 1))], vision=True)
        self.assertFalse(ChatAttachment.objects.exists())

    def test_document_context_limits_reject_instead_of_silently_shortening(self):
        with self.assertRaises(ValueError):
            prepare_uploads([SimpleUploadedFile('too-long.txt', b'x' * 100000)], vision=False)
        with self.assertRaises(ValueError):
            prepare_uploads([SimpleUploadedFile('first.txt', b'x' * 70000),
                             SimpleUploadedFile('second.txt', b'y' * 70000)], vision=False)
        self.assertFalse(ChatAttachment.objects.exists())

    def test_total_upload_size_is_bounded_before_document_parsing(self):
        uploads = [SimpleUploadedFile(f'{index}.txt', b'x' * (9 * 1024 * 1024)) for index in range(3)]
        with self.assertRaisesRegex(ValueError, '25'):
            prepare_uploads(uploads, vision=False)
        self.assertFalse(ChatAttachment.objects.exists())

    def test_spreadsheet_attachment_keeps_actual_cells_and_formula_as_source_text(self):
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = 'Operations'
        sheet.append(['Equipment', 'Pressure', 'Calculation'])
        sheet.append(['Valve A', 42, '=B2*2'])
        stream = BytesIO()
        book.save(stream)
        book.close()
        attachment, = self.save([SimpleUploadedFile('operations.xlsx', stream.getvalue())], vision=False)
        self.assertIn('Valve A', attachment.extracted_text)
        self.assertIn('42', attachment.extracted_text)
        self.assertIn('=B2*2', attachment.extracted_text)

    def test_readable_pdf_text_is_available_without_vision(self):
        import pymupdf
        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_text((70, 70), 'Inspection complete. Pressure 42 kPa.')
        payload = pdf.tobytes()
        pdf.close()
        attachment, = self.save([SimpleUploadedFile('inspection.pdf', payload)], vision=False)
        self.assertEqual(attachment.kind, 'document')
        self.assertIn('Pressure 42 kPa.', attachment.extracted_text)

    def test_scanned_pdf_is_visual_with_vision_and_rejected_without_it(self):
        import pymupdf
        image = self.image_upload().read()
        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_image(page.rect, stream=image)
        payload = pdf.tobytes()
        pdf.close()
        with self.assertRaises(ValueError):
            prepare_uploads([SimpleUploadedFile('scanned.pdf', payload)], vision=False)
        attachment, = self.save([SimpleUploadedFile('scanned.pdf', payload)])
        hydrated = self.hydrate([self.human([attachment])])
        self.assertTrue(any(block.get('type') == 'image_url' for block in hydrated[0].content))

    def test_conversation_deletion_removes_private_files_after_commit(self):
        attachments = self.save([self.image_upload(), SimpleUploadedFile('notes.txt', b'Private notes')])
        paths = [Path(attachment.file.path) for attachment in attachments]
        with self.captureOnCommitCallbacks(execute=True):
            self.conversation.delete()
        self.assertFalse(ChatAttachment.objects.exists())
        self.assertTrue(all(not path.exists() for path in paths))

    def test_rolled_back_deletion_keeps_file_and_attachment(self):
        attachment, = self.save([self.image_upload()])
        identity = attachment.pk
        path = Path(attachment.file.path)
        with transaction.atomic():
            attachment.delete()
            transaction.set_rollback(True)
        self.assertTrue(ChatAttachment.objects.filter(pk=identity).exists())
        self.assertTrue(path.exists())

    def test_failure_saving_later_attachment_cleans_files_and_rows_from_batch(self):
        real_save = ChatAttachment.save
        count = 0

        def failing_save(instance, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('Simulated database failure')
            return real_save(instance, *args, **kwargs)

        prepared = prepare_uploads([self.image_upload(), SimpleUploadedFile('notes.txt', b'Notes')], vision=True)
        with patch.object(ChatAttachment, 'save', failing_save), self.assertRaises(RuntimeError):
            save_attachments(self.message, prepared)
        self.assertFalse(ChatAttachment.objects.exists())
        self.assertFalse(self.private.exists() and any(path.is_file() for path in self.private.rglob('*')))

    def test_attachment_evidence_verifies_owner_file_and_extracted_text(self):
        attachment, = self.save([SimpleUploadedFile('source.txt', b'Pressure is 42 kPa.')], vision=False)
        snapshot, = evidence_for_attachments([str(attachment.pk)], user_id=self.owner.pk,
                                             conversation_id=self.conversation.pk)
        self.assertTrue(snapshot_current(snapshot, self.owner.pk))
        self.assertFalse(snapshot_current(snapshot, self.other.pk))
        ChatAttachment.objects.filter(pk=attachment.pk).update(extracted_text='Pressure is 999 kPa.')
        self.assertFalse(snapshot_current(snapshot, self.owner.pk))
        with self.assertRaises(ValueError):
            verification_blocks(snapshot, self.owner.pk)

    def test_attachment_evidence_rejects_modified_file_and_visual_verification_has_pixels(self):
        attachment, = self.save([self.image_upload()])
        snapshot, = evidence_for_attachments([str(attachment.pk)], user_id=self.owner.pk,
                                             conversation_id=self.conversation.pk)
        self.assertTrue(snapshot_current(snapshot, self.owner.pk))
        blocks = verification_blocks(snapshot, self.owner.pk)
        self.assertTrue(any(block.get('type') == 'image_url' for block in blocks))
        Path(attachment.file.path).write_bytes(b'changed image payload')
        self.assertFalse(snapshot_current(snapshot, self.owner.pk))
        with self.assertRaises(ValueError):
            verification_blocks(snapshot, self.owner.pk)

    def test_six_attachments_across_two_turns_remain_hydratable_and_verifiable(self):
        first = self.save([self.image_upload(name=f'first-{index}.png') for index in range(3)])
        next_prepared = prepare_uploads([self.image_upload(name=f'second-{index}.png') for index in range(3)], vision=True)
        validate_conversation_context(self.conversation, next_prepared, vision=True)
        message = Message.objects.create(conversation=self.conversation, role='user', content='Inspect the second set.')
        second = save_attachments(message, next_prepared)
        hydrated = self.hydrate([self.human(first), self.human(second)])
        self.assertEqual([sum(block.get('type') == 'image_url' for block in item.content)
                          for item in hydrated], [3, 3])
        snapshots = evidence_for_attachments([str(item.pk) for item in first + second],
            user_id=self.owner.pk, conversation_id=self.conversation.pk)
        self.assertEqual(len(snapshots), 6)
        self.assertTrue(all(snapshot_current(item, self.owner.pk) for item in snapshots))

    def test_context_file_limit_rejects_new_upload_without_discarding_twenty_sources(self):
        retained = []
        for turn in range(4):
            retained.extend(self.save_next_message([
                SimpleUploadedFile(f'{turn}-{index}.txt', b'Original data') for index in range(5)
            ], vision=False))
        validate_conversation_context(self.conversation, [], vision=False)
        prepared = prepare_uploads([SimpleUploadedFile('twenty-first.txt', b'Next source')], vision=False)
        with self.assertRaisesRegex(ValueError, '20'):
            validate_conversation_context(self.conversation, prepared, vision=False)
        self.assertEqual(ChatAttachment.objects.count(), 20)
        self.assertTrue(all(Path(item.file.path).exists() for item in retained))

    def test_context_text_limit_counts_previous_turns_and_preserves_old_data(self):
        attachment, = self.save([SimpleUploadedFile('old.txt', b'x' * 70000)], vision=False)
        prepared = prepare_uploads([SimpleUploadedFile('new.txt', b'y' * 60000)], vision=False)
        with self.assertRaisesRegex(ValueError, '120000'):
            validate_conversation_context(self.conversation, prepared, vision=False)
        attachment.refresh_from_db()
        self.assertEqual(attachment.extracted_text, 'x' * 70000)
        self.assertEqual(ChatAttachment.objects.count(), 1)
        self.assertTrue(Path(attachment.file.path).exists())

    def test_existing_visual_attachment_blocks_turn_when_model_becomes_text_only(self):
        attachment, = self.save([self.image_upload()])
        with self.assertRaises(ValueError):
            validate_conversation_context(self.conversation, [], vision=False)
        self.assertTrue(ChatAttachment.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(Path(attachment.file.path).exists())

    def test_visual_limits_count_pdf_pages_and_photos_together(self):
        with self.assertRaisesRegex(ValueError, '8'):
            prepare_uploads([self.pdf_upload(pages=8), self.image_upload()], vision=True)
        retained = self.save([self.pdf_upload(pages=8)])
        second = prepare_uploads([self.pdf_upload(name='second.pdf', pages=8)], vision=True)
        validate_conversation_context(self.conversation, second, vision=True)
        message = Message.objects.create(conversation=self.conversation, role='user', content='Second PDF.')
        retained.extend(save_attachments(message, second))
        prepared = prepare_uploads([self.image_upload()], vision=True)
        with self.assertRaisesRegex(ValueError, '16'):
            validate_conversation_context(self.conversation, prepared, vision=True)
        self.assertEqual(ChatAttachment.objects.count(), 2)
        self.assertTrue(all(Path(item.file.path).exists() for item in retained))

    def test_pdf_with_readable_text_and_embedded_photo_reaches_model_visually(self):
        attachment, = self.save([self.pdf_upload(include_image=True)])
        self.assertIn('Pressure is 42 kPa.', attachment.extracted_text)
        hydrated = self.hydrate([self.human([attachment])])
        images = [block for block in hydrated[0].content if block.get('type') == 'image_url']
        self.assertEqual(len(images), 1)
        with Image.open(BytesIO(base64.b64decode(images[0]['image_url']['url'].split(',', 1)[1]))) as image:
            self.assertGreater(image.width, 100)

    def test_snapshot_source_name_or_text_tampering_invalidates_evidence(self):
        attachment, = self.save([SimpleUploadedFile('source.txt', b'Pressure is 42 kPa.')], vision=False)
        snapshot, = evidence_for_attachments([str(attachment.pk)], user_id=self.owner.pk,
                                             conversation_id=self.conversation.pk)
        for field, value in (('source', 'forged-source.txt'), ('text', 'Pressure is 999 kPa.')):
            changed = {**snapshot, field: value}
            with self.subTest(field=field):
                self.assertFalse(snapshot_current(changed, self.owner.pk))
                with self.assertRaises(ValueError):
                    verification_blocks(changed, self.owner.pk)

    def test_attachment_storage_rejects_public_media_and_static_paths(self):
        from kb.chat_attachments import ChatAttachmentStorage
        static_root = self.root / 'collected-static'
        assets = self.root / 'source-assets'
        prefixed_assets = self.root / 'prefixed-assets'
        cases = (
            ('media', self.media / 'chat'),
            ('static-root', static_root / 'chat'),
            ('static-directory', assets / 'chat'),
            ('prefixed-static-directory', prefixed_assets / 'chat'),
        )
        for name, path in cases:
            with self.subTest(name=name), override_settings(
                CHAT_ATTACHMENT_ROOT=path,
                STATIC_ROOT=static_root,
                STATICFILES_DIRS=[assets, ('theme', prefixed_assets)],
            ):
                with self.assertRaises(ImproperlyConfigured):
                    _ = ChatAttachmentStorage().location
        attachment, = self.save([SimpleUploadedFile('private.txt', b'Private document')], vision=False)
        self.assertTrue(Path(attachment.file.path).exists())
        with self.assertRaises(ValueError):
            _ = attachment.file.url

