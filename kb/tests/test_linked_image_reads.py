"""Reading a text chunk offers images without silently inspecting or citing them."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from langchain_core.messages import HumanMessage, messages_to_dict

from kb.history import load_context
from kb.models import Document, KnowledgeBase
from kb.publication import validate_sources, visible_citations
from kb.rag_selection import MAX_DISCOVERY_CHARS, MAX_READ_CHARS, MAX_RESPONSE_CHARS, RagSelection


def payload(response):
    return json.loads(response[0]['text'] if isinstance(response, list) else response)


class LinkedImageReadTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = override_settings(DATA_DIR=Path(self.temp.name))
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_user('linked-image-reader')
        self.kb = KnowledgeBase.objects.create(name='Operations', slug='linked-images')
        self.names = [f'{index:032x}.png' for index in range(1, 7)]
        self.body = f'Measured output increased steadily. ![Daily output](images/{self.names[0]})'
        self.doc = Document.objects.create(kb=self.kb, original_name='Operations.pdf',
                                           status='completed', md_content=self.body)
        self.image_dir = Path(self.temp.name) / 'doc_images' / str(self.doc.pk)
        self.image_dir.mkdir(parents=True)
        for name in self.names:
            (self.image_dir / name).write_bytes(b'image-source-' + name.encode())
        self.row = {'type': 'text', 'text': self.body, 'doc_id': str(self.doc.pk),
                    'source': self.doc.original_name, 'kb_slug': self.kb.slug, 'chunk_id': 'text-output'}
        self.evidence = []
        self.citations = []
        self.reader = Mock(side_effect=lambda doc_id, name: {'type': 'image', 'base64': name, 'mime_type': 'image/png'})
        self.selection = self.new_selection()

    def new_selection(self, *, saved=None):
        return RagSelection(lambda kb: True, self.evidence, self.citations,
                            image_reader=self.reader, saved=saved)

    def discover_text(self):
        result = json.loads(self.selection.discover([self.row], [self.kb.slug]))
        return result['candidates'][0]['candidate_id']

    def discover_images(self, count=1):
        rows = [dict(self.row, type='image', image=name, chunk_id=f'image-{index}', text=f'Image {index}')
                for index, name in enumerate(self.names[:count])]
        return [item['candidate_id'] for item in json.loads(self.selection.discover(rows, [self.kb.slug]))['candidates']]

    def test_text_directory_then_explicit_image_read_and_selection(self):
        text_id = self.discover_text()
        text_response = self.selection.read([text_id])
        self.assertIsInstance(text_response, str)
        hint = payload(text_response)['linked_images'][0]
        image_id = hint['candidate_id']
        self.assertEqual(hint['parent_candidate_id'], text_id)
        self.assertEqual(hint['caption'], 'Daily output')
        self.assertEqual(hint['relation'], 'embedded_reference')
        self.assertEqual(hint['position']['basis'], 'chunk_text')
        self.assertNotIn(image_id, self.selection.loaded)
        self.reader.assert_not_called()
        self.assertEqual(self.evidence, [])
        self.assertEqual(self.citations, [])
        self.assertIn('error', json.loads(self.selection.select([image_id])))

        response = self.selection.read([image_id])
        self.assertEqual(payload(response)['image_inputs'], [{'candidate_id': image_id, 'status': 'delivered'}])
        self.assertEqual(json.loads(response[1]['text']),
                         {'image_candidate_id': image_id, 'source': self.doc.original_name, 'page': ''})
        self.assertEqual(response[2]['type'], 'image')
        self.assertEqual(self.selection.image_inputs, {image_id})
        self.assertFalse(self.selection.candidates[image_id]['snapshot']['visual_confirmed'])
        self.assertEqual(self.citations, [])
        self.selection.select([image_id])
        self.assertEqual(self.citations[0]['images'], [f'/kb/doc/{self.doc.pk}/img/{self.names[0]}'])
        self.assertEqual(len(self.citations[0]['image_snapshots']), 1)
        self.assertTrue(validate_sources(self.evidence, self.user.pk))
        self.assertTrue(visible_citations(self.citations, self.user))

    def test_selecting_only_text_never_auto_cites_offered_image(self):
        text_id = self.discover_text()
        self.selection.read([text_id])
        self.selection.select([text_id])
        self.assertEqual([item['kind'] for item in self.evidence], ['text'])
        self.assertEqual(self.citations[0]['images'], [])
        self.assertNotIn('image_snapshots', self.citations[0])

    def test_already_read_text_offers_directory_for_older_saved_context(self):
        text_id = self.discover_text()
        saved = copy.deepcopy(self.selection.snapshot())
        saved['loaded'] = [text_id]
        saved.pop('image_inputs')
        self.selection = self.new_selection(saved=saved)
        result = payload(self.selection.read([text_id]))
        self.assertEqual(result['already_loaded'], [text_id])
        self.assertEqual(result['chunks'], [])
        self.assertEqual(len(result['linked_images']), 1)
        self.reader.assert_not_called()

    def test_caption_only_read_can_upgrade_and_delivery_survives_snapshot(self):
        image_id = self.discover_images()[0]
        initial = payload(self.selection.read([image_id], include_images=False))
        self.assertEqual(initial['image_inputs'][0]['reason'], 'not_requested')
        self.assertEqual(self.selection.image_inputs, set())
        self.reader.assert_not_called()
        upgraded = self.selection.read([image_id])
        self.assertEqual(payload(upgraded)['already_loaded'], [image_id])
        self.assertEqual(payload(upgraded)['image_inputs'][0]['status'], 'delivered')
        self.selection = self.new_selection(saved=copy.deepcopy(self.selection.snapshot()))
        again = self.selection.read([image_id])
        self.assertIsInstance(again, str)
        self.assertEqual(payload(again)['image_inputs'][0]['status'], 'already_delivered')
        self.reader.assert_called_once()

    def test_legacy_caption_snapshot_can_upgrade_without_delivery_stamp(self):
        image_id = self.discover_images()[0]
        self.selection.read([image_id], include_images=False)
        saved = copy.deepcopy(self.selection.snapshot())
        saved.pop('image_inputs')
        self.selection = self.new_selection(saved=saved)
        self.assertEqual(payload(self.selection.read([image_id]))['image_inputs'][0]['status'], 'delivered')

    def test_disabling_vision_clears_restored_pixel_delivery_stamp(self):
        image_id = self.discover_images()[0]
        self.selection.read([image_id])
        restored = RagSelection(lambda kb: True, [], [], saved=copy.deepcopy(self.selection.snapshot()))
        self.assertEqual(restored.image_inputs, set())
        result = payload(restored.read([image_id]))
        self.assertEqual(result['image_inputs'][0]['status'], 'caption_only')
        self.assertEqual(result['image_inputs'][0]['reason'], 'vision_disabled')
        self.assertEqual(restored.snapshot()['image_inputs'], [])

    def test_four_image_limit_reports_caption_only_and_labels_each_delivered_image(self):
        ids = self.discover_images(5)
        response = self.selection.read(ids)
        statuses = payload(response)['image_inputs']
        self.assertEqual([item['status'] for item in statuses], ['delivered'] * 4 + ['caption_only'])
        self.assertEqual(statuses[-1]['reason'], 'image_limit')
        self.assertEqual(self.selection.image_inputs, set(ids[:4]))
        self.assertEqual(self.selection.images_read, 4)
        for index, candidate_id in enumerate(ids[:4]):
            self.assertEqual(json.loads(response[1 + 2 * index]['text'])['image_candidate_id'], candidate_id)
            self.assertEqual(response[2 + 2 * index]['type'], 'image')
        self.assertEqual(self.reader.call_count, 4)

    def test_reader_failure_and_disabled_vision_never_claim_pixel_delivery(self):
        image_id = self.discover_images()[0]
        self.reader.side_effect = RuntimeError('Image decoder failed')
        result = payload(self.selection.read([image_id]))
        self.assertEqual(result['image_inputs'][0],
                         {'candidate_id': image_id, 'status': 'caption_only', 'reason': 'image_unavailable'})
        self.assertEqual(self.selection.image_inputs, set())
        self.assertEqual(self.selection.images_read, 0)
        self.assertIn(image_id, self.selection.loaded)
        self.selection.image_reader = None
        self.assertEqual(payload(self.selection.read([image_id]))['image_inputs'][0]['reason'], 'vision_disabled')

    def test_missing_source_is_unread_and_changed_source_cannot_be_selected(self):
        image_id = self.discover_images()[0]
        (self.image_dir / self.names[0]).unlink()
        result = payload(self.selection.read([image_id]))
        self.assertEqual(result['not_loaded'], [image_id])
        self.assertEqual(result['image_inputs'][0]['status'], 'not_loaded')
        self.assertNotIn(image_id, self.selection.loaded)
        self.reader.assert_not_called()
        (self.image_dir / self.names[0]).write_bytes(b'restored-image')
        self.selection.read([image_id])
        (self.image_dir / self.names[0]).write_bytes(b'changed-image')
        self.assertIn('error', json.loads(self.selection.select([image_id])))

    def test_permission_or_document_change_rejects_linked_image(self):
        text_id = self.discover_text()
        image_id = payload(self.selection.read([text_id]))['linked_images'][0]['candidate_id']
        self.selection.allowed = lambda kb: False
        self.assertIn('error', payload(self.selection.read([image_id])))
        self.reader.assert_not_called()
        self.selection.allowed = lambda kb: True
        self.doc.md_content = 'Updated document'
        self.doc.save(update_fields=['md_content'])
        self.assertIn('error', payload(self.selection.read([image_id])))

    def test_budget_omits_image_hints_before_dropping_accepted_text(self):
        self.body = 'Measured output is stable. ' * 5 + ' '.join(
            f'![Daily output {index}](images/{name})' for index, name in enumerate(self.names))
        self.row['text'] = self.body
        self.doc.md_content = self.body
        self.doc.save(update_fields=['md_content'])
        text_id = self.discover_text()
        with patch('kb.rag_selection.MAX_RESPONSE_CHARS', 1500):
            response = self.selection.read([text_id])
        result = payload(response)
        self.assertEqual(result['chunks'][0]['text'], self.body)
        self.assertLess(len(result.get('linked_images', [])), len(self.names))
        shown_ids = {hint['candidate_id'] for hint in result.get('linked_images', [])}
        self.assertEqual(self.selection.presented, {text_id} | shown_ids)
        self.assertLessEqual(len(response), 1500)
        self.assertLessEqual(self.selection.read_chars, MAX_READ_CHARS)
        self.assertLessEqual(self.selection.discovery_chars, MAX_DISCOVERY_CHARS)

    def test_exhausted_discovery_budget_does_not_block_text_read(self):
        text_id = self.discover_text()
        self.selection.discovery_chars = MAX_DISCOVERY_CHARS
        response = self.selection.read([text_id])
        self.assertEqual(payload(response)['chunks'][0]['text'], self.body)
        self.assertNotIn('linked_images', payload(response))
        self.assertLessEqual(len(response), MAX_RESPONSE_CHARS)

    def test_history_rejects_delivered_image_when_its_source_changes(self):
        image_id = self.discover_images()[0]
        self.selection.read([image_id])
        self.selection.select([image_id])
        context = {'version': 1, 'field_visibility': {},
                   'messages': messages_to_dict([HumanMessage(content='Inspect the output chart')]),
                   'rag': self.selection.snapshot(), 'evidence': self.evidence, 'citations': self.citations}
        conversation = SimpleNamespace(pk='linked-image-history', agent_context=context)
        self.assertEqual(load_context(conversation, self.user), context)
        (self.image_dir / self.names[0]).write_bytes(b'changed-image')
        self.assertEqual(load_context(conversation, self.user), {})

    def test_changed_image_metadata_invalidates_publication_and_saved_history(self):
        text_id = self.discover_text()
        image_id = payload(self.selection.read([text_id]))['linked_images'][0]['candidate_id']
        self.selection.read([image_id])
        self.selection.select([image_id])
        context = {'version': 1, 'field_visibility': {},
                   'messages': messages_to_dict([HumanMessage(content='Inspect the output chart')]),
                   'rag': self.selection.snapshot(), 'evidence': self.evidence, 'citations': self.citations}
        conversation = SimpleNamespace(pk='linked-image-metadata-history', agent_context=context)
        self.assertTrue(validate_sources(self.evidence, self.user.pk))
        self.assertEqual(load_context(conversation, self.user), context)
        metadata = Path(self.temp.name) / 'doc_meta' / str(self.doc.pk)
        metadata.mkdir(parents=True)
        (metadata / 'content_list.json').write_text(json.dumps([
            {'type': 'image', 'img_path': f'images/{self.names[0]}', 'page_idx': 8,
             'image_caption': ['New image caption']}]), encoding='utf-8')
        self.assertFalse(validate_sources(self.evidence, self.user.pk))
        self.assertFalse(visible_citations(self.citations, self.user))
        self.assertEqual(load_context(conversation, self.user), {})

    def test_metadata_association_is_rechecked_before_read_and_selection(self):
        before = 'Equipment A operated continuously throughout the measurement period.'
        after = 'The subsequent analysis confirmed a stable daily operating trend.'
        self.row['text'] = f'{before}\n\n{after}'
        self.doc.md_content = self.row['text']
        self.doc.save(update_fields=['md_content'])
        metadata = Path(self.temp.name) / 'doc_meta' / str(self.doc.pk)
        metadata.mkdir(parents=True)
        path = metadata / 'content_list.json'
        blocks = [{'type': 'text', 'text': before},
                  {'type': 'image', 'img_path': f'images/{self.names[0]}', 'image_caption': ['Daily trend']},
                  {'type': 'text', 'text': after}]
        path.write_text(json.dumps(blocks), encoding='utf-8')
        text_id = self.discover_text()
        image_id = payload(self.selection.read([text_id]))['linked_images'][0]['candidate_id']
        self.selection.read([image_id])
        path.write_text(json.dumps([blocks[0], blocks[2]]), encoding='utf-8')
        self.assertIn('error', payload(self.selection.read([image_id])))
        self.assertIn('error', json.loads(self.selection.select([image_id])))

    def test_metadata_caption_and_page_changes_require_fresh_image_candidates(self):
        self.row['text'] = f'Measured output is stable. ![](images/{self.names[0]})'
        self.doc.md_content = self.row['text']
        self.doc.save(update_fields=['md_content'])
        metadata = Path(self.temp.name) / 'doc_meta' / str(self.doc.pk)
        metadata.mkdir(parents=True)
        path = metadata / 'content_list.json'
        block = {'type': 'image', 'img_path': f'images/{self.names[0]}',
                 'image_caption': ['Load trend'], 'page_idx': 0}
        path.write_text(json.dumps([block]), encoding='utf-8')
        text_id = self.discover_text()
        first = payload(self.selection.read([text_id]))['linked_images'][0]
        block['image_caption'] = ['New load trend']
        path.write_text(json.dumps([block]), encoding='utf-8')
        self.assertIn('error', payload(self.selection.read([first['candidate_id']])))
        second = payload(self.selection.read([text_id]))['linked_images'][0]
        self.assertNotEqual(second['candidate_id'], first['candidate_id'])
        self.assertEqual(second['caption'], 'New load trend')
        self.selection.read([second['candidate_id']])
        block['page_idx'] = 4
        path.write_text(json.dumps([block]), encoding='utf-8')
        self.assertIn('error', json.loads(self.selection.select([second['candidate_id']])))
        third = payload(self.selection.read([text_id]))['linked_images'][0]
        self.assertNotEqual(third['candidate_id'], second['candidate_id'])
        self.assertNotEqual(third['page'], second['page'])
        self.assertEqual(payload(self.selection.read([third['candidate_id']]))['image_inputs'][0]['status'], 'delivered')

