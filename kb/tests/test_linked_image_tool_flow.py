"""Exercise chunk-linked images through the actual normal/enhanced chat tools."""
import base64
import io
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from kb.agent import _build_agent, _image_content_block, _text_only_tool_history, run_agent_stream
from kb.models import Document, KnowledgeBase
from kb.pipeline import _md_for_embedding
from kb.publication import validate_sources, visible_citations


PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
       '/x8AAwMCAO+j3ioAAAAASUVORK5CYII=')
IMAGE_BLOCK = {'type': 'image', 'base64': PNG, 'mime_type': 'image/png'}


def result_text(result):
    return json.loads(result[0]['text'] if isinstance(result, list) else result)


class LinkedImageToolFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('linked-image-reader', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Equipment Manual', slug='linked-image-manual')
        self.name = 'a' * 32 + '.png'
        self.markdown = ('## Inspection\n\nThe measured motor load remained within its operating limit.\n\n'
            f'![Motor load trend](images/{self.name})\n\n'
            'The subsequent inspection confirmed the machine was in good condition.')
        self.doc = Document.objects.create(kb=self.kb, original_name='Motor inspection.pdf',
            status=Document.Status.COMPLETED, md_content=self.markdown)
        self.row = {'doc_id': str(self.doc.pk), 'source': self.doc.original_name,
            'kb_slug': self.kb.slug, 'chunk_id': 'motor-inspection-text', 'type': 'text',
            'text': _md_for_embedding(self.markdown), 'page_label': '第 3 页'}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        settings = override_settings(DATA_DIR=self.data_dir)
        settings.enable()
        self.addCleanup(settings.disable)
        self.image_path = self.data_dir / 'doc_images' / str(self.doc.pk) / self.name
        self.image_path.parent.mkdir(parents=True)
        self.image_path.write_bytes(base64.b64decode(PNG))
        metadata = self.data_dir / 'doc_meta' / str(self.doc.pk)
        metadata.mkdir(parents=True)
        (metadata / 'content_list.json').write_text(json.dumps([
            {'type': 'image', 'img_path': f'images/{self.name}', 'page_idx': 2,
             'image_caption': ['Motor load trend']}]), encoding='utf-8')

    def tools(self, enhanced=False, *, vision=True, conversation_context=None):
        citations, evidence, state = [], [], {}
        with patch('langchain.agents.create_agent') as create, \
             patch('kb.agent._get_llm', return_value=object()):
            _build_agent('', 'linked-image-tools', {'qa_enhance': enhanced, 'vision': vision}, 5, None,
                citations=citations, evidence=evidence, retrieval_state=state,
                relationship_user_id=self.user.pk, conversation_context=conversation_context)
        return ({tool.__name__: tool for tool in create.call_args.kwargs['tools']},
                citations, evidence, state)

    def read_text(self, tools):
        with patch('kb.retriever.search', return_value=[dict(self.row)]), \
             patch('kb.provenance.annotate_results'):
            directory = json.loads(tools['kb_search']('motor load', kb_slug=self.kb.slug))
        text_id = directory['candidates'][0]['candidate_id']
        body = result_text(tools['kb_read_chunks']([text_id]))
        return text_id, body

    def test_text_to_image_to_citation_in_normal_and_enhanced_modes(self):
        for enhanced in (False, True):
            with self.subTest(enhanced=enhanced), \
                 patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)) as reader:
                tools, citations, evidence, state = self.tools(enhanced)
                text_id, body = self.read_text(tools)
                self.assertEqual(body['chunks'][0]['text'], self.row['text'])
                self.assertEqual(len(body['linked_images']), 1)
                hint = body['linked_images'][0]
                image_id = hint['candidate_id']
                self.assertEqual(hint['parent_candidate_id'], text_id)
                self.assertEqual(hint['source'], self.doc.original_name)
                self.assertEqual(hint['page'], '第 3 页')
                self.assertEqual(hint['caption'], 'Motor load trend')
                self.assertEqual(hint['relation'], 'embedded_reference')
                reader.assert_not_called()
                self.assertEqual(citations, [])
                self.assertEqual(evidence, [])
                self.assertNotIn(image_id, state['session'].loaded)

                selected = json.loads(tools['kb_select_evidence']([image_id]))
                self.assertIn('error', selected)
                self.assertEqual(citations, [])

                image = tools['kb_read_chunks']([image_id])
                self.assertIsInstance(image, list)
                self.assertEqual(result_text(image)['image_inputs'],
                    [{'candidate_id': image_id, 'status': 'delivered'}])
                self.assertEqual(image[-1], IMAGE_BLOCK)
                # Each pixel block has a source identity immediately before it.
                identity = json.loads(image[-2]['text'])
                self.assertEqual(identity, {'image_candidate_id': image_id,
                    'source': self.doc.original_name, 'page': '第 3 页'})
                reader.assert_called_once_with(str(self.doc.pk), self.name)
                self.assertEqual(citations, [])

                selected = json.loads(tools['kb_select_evidence']([text_id, image_id]))
                self.assertEqual(selected['selected_candidate_ids'], [text_id, image_id])
                self.assertEqual(len(citations), 1)
                self.assertEqual(citations[0]['images'],
                    [f'/kb/doc/{self.doc.pk}/img/{self.name}'])
                self.assertEqual(citations[0]['image_snapshots'][0]['kind'], 'image')
                # Receiving pixels does not fabricate a relevance-judge verdict.
                self.assertFalse(citations[0]['image_snapshots'][0]['visual_confirmed'])
                with patch('kb.keyword_index.get_chunks', return_value=[self.row]):
                    self.assertTrue(validate_sources(evidence, self.user.pk))
                    self.assertTrue(visible_citations(citations, self.user))

    def test_selecting_only_the_text_does_not_publish_its_discovered_images(self):
        with patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)) as reader:
            tools, citations, evidence, _ = self.tools()
            text_id, body = self.read_text(tools)
            self.assertTrue(body['linked_images'])
            tools['kb_select_evidence']([text_id])
        reader.assert_not_called()
        self.assertEqual(citations[0]['images'], [])
        self.assertEqual([item['kind'] for item in evidence], ['text'])

    def test_vision_disabled_returns_caption_status_without_claiming_pixel_delivery(self):
        with patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)) as reader:
            tools, citations, _, state = self.tools(vision=False)
            _, body = self.read_text(tools)
            image_id = body['linked_images'][0]['candidate_id']
            image = result_text(tools['kb_read_chunks']([image_id]))
        reader.assert_not_called()
        self.assertEqual(image['image_inputs'], [{'candidate_id': image_id,
            'status': 'caption_only', 'reason': 'vision_disabled'}])
        self.assertNotIn(image_id, state['session'].image_inputs)
        self.assertEqual(citations, [])

    def test_image_source_changes_invalidate_selected_evidence_before_publication(self):
        with patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)):
            tools, citations, evidence, _ = self.tools()
            text_id, body = self.read_text(tools)
            image_id = body['linked_images'][0]['candidate_id']
            tools['kb_read_chunks']([image_id])
            tools['kb_select_evidence']([text_id, image_id])
        self.image_path.write_bytes(b'changed image source')
        with patch('kb.keyword_index.get_chunks', return_value=[self.row]):
            self.assertFalse(validate_sources(evidence, self.user.pk))
            self.assertFalse(visible_citations(citations, self.user))

    def test_image_search_uses_the_same_labeled_delivery_budget_without_auto_citing(self):
        from langchain_core.messages import ToolMessage

        names = [self.name] + [char * 32 + '.png' for char in 'bcdef']
        for name in names[1:]:
            (self.image_path.parent / name).write_bytes(base64.b64decode(PNG))
        rows = [dict(self.row, type='image', image=name, text=f'Motor image {index}',
            chunk_id=f'img-{self.doc.pk}-{name}', distance=0.4 + (index % 3) * 0.001)
            for index, name in enumerate(names)]
        with patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)), \
             patch('kb.pipeline._embeddings', return_value=Mock(embed_query=Mock(return_value=[0.1]))), \
             patch('kb.retriever._image_lane', side_effect=[rows[:3], rows[3:]]), \
             patch('kb.provenance.annotate_results'), \
             patch('kb.agent._judge_candidate', return_value={'match': True, 'caption': 'Motor image'}), \
             patch('kb.agent._get_llm', return_value=object()):
            tools, citations, _, state = self.tools()
            outputs = [tools['kb_image_search']('motor', kb_slug=self.kb.slug, top=3) for _ in range(2)]
        delivered = []
        limited = []
        for output in outputs:
            self.assertIsInstance(output, list)
            payload = next(json.loads(part['text']) for part in output
                if part.get('type') == 'text' and part['text'].startswith('{')
                and json.loads(part['text']).get('kind') == 'rag_chunks')
            delivered.extend(item for item in payload['image_inputs'] if item['status'] == 'delivered')
            limited.extend(item for item in payload['image_inputs'] if item.get('reason') == 'image_limit')
            for index, part in enumerate(output):
                if part['type'] == 'image':
                    self.assertIn('image_candidate_id', json.loads(output[index - 1]['text']))
            sanitized, = _text_only_tool_history([
                ToolMessage(content=output, tool_call_id='image-search', name='kb_image_search')])
            self.assertFalse(any(part.get('type') in ('image', 'image_url', 'input_image')
                for part in sanitized.content if isinstance(part, dict)))
            caption_result = next(json.loads(part['text']) for part in sanitized.content
                if part.get('type') == 'text' and part['text'].startswith('{')
                and json.loads(part['text']).get('kind') == 'rag_chunks')
            self.assertTrue(all(item['status'] == 'caption_only' for item in caption_result['image_inputs']))
            self.assertEqual(len(caption_result['image_inputs']), len(payload['image_inputs']))
            self.assertEqual(len([item for item in caption_result['image_inputs']
                if item.get('reason') == 'vision_disabled']),
                len([item for item in payload['image_inputs'] if item['status'] == 'delivered']))
        self.assertEqual(len(delivered), 4)
        self.assertEqual(len(limited), 2)
        self.assertEqual(len(state['session'].image_inputs), 4)
        self.assertEqual(citations, [])

    def test_page_one_pixels_are_from_the_first_pdf_page(self):
        import pymupdf
        from PIL import Image

        # Different solid colors make an off-by-one PDF page read observable.
        document = pymupdf.open()
        for color in ((1, 0, 0), (0, 0, 1)):
            page = document.new_page(width=100, height=100)
            page.draw_rect(page.rect, color=color, fill=color)
        document.save(self.data_dir / 'source.pdf')
        document.close()
        self.doc.file = 'source.pdf'
        self.doc.save(update_fields=['file'])
        with override_settings(MEDIA_ROOT=self.data_dir):
            block = _image_content_block(str(self.doc.pk), 'page1')
        self.assertIsNotNone(block)
        with Image.open(io.BytesIO(base64.b64decode(block['base64']))) as image:
            red, green, blue = image.convert('RGB').getpixel((image.width // 2, image.height // 2))
        self.assertGreater(red, 200)
        self.assertLess(green, 40)
        self.assertLess(blue, 40)

    def test_webp_image_payload_uses_its_actual_mime_type(self):
        from PIL import Image

        name = 'f' * 64 + '.webp'
        Image.new('RGB', (2, 2), 'green').save(self.image_path.parent / name, format='WEBP')
        block = _image_content_block(str(self.doc.pk), name)
        self.assertEqual(block['mime_type'], 'image/webp')

    def test_real_stream_keeps_image_tool_transcript_and_publishes_selected_image_citation(self):
        from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

        fixture = self

        class ToolFlowAgent:
            def __init__(self, functions):
                self.tools = {function.__name__: function for function in functions}

            async def astream_events(self, inputs, **options):
                transcript = list(inputs['messages'])
                text_id = image_id = None
                for index, name in enumerate(('kb_search', 'kb_read_chunks', 'kb_read_chunks', 'kb_select_evidence')):
                    arguments = ({'query': 'motor load', 'kb_slug': fixture.kb.slug} if index == 0
                        else {'candidate_ids': [text_id]} if index == 1
                        else {'candidate_ids': [image_id]} if index == 2
                        else {'candidate_ids': [text_id, image_id]})
                    identity = f'image-tool-{index}'
                    yield {'event': 'on_tool_start', 'name': name, 'run_id': identity,
                           'data': {'input': arguments}}
                    output = self.tools[name](**arguments)
                    transcript.extend([AIMessage(content='', tool_calls=[{
                        'name': name, 'args': arguments, 'id': identity}]),
                        ToolMessage(content=output, tool_call_id=identity, name=name)])
                    if index == 0:
                        text_id = result_text(output)['candidates'][0]['candidate_id']
                    elif index == 1:
                        image_id = result_text(output)['linked_images'][0]['candidate_id']
                    yield {'event': 'on_tool_end', 'name': name, 'run_id': identity,
                           'data': {'output': output}}
                answer = AIMessage(content='The chart shows a stable motor load [1].',
                    response_metadata={'finish_reason': 'stop'})
                transcript.append(answer)
                yield {'event': 'on_chat_model_start', 'data': {}}
                yield {'event': 'on_chat_model_stream', 'data': {
                    'chunk': AIMessageChunk(content=answer.content)}}
                yield {'event': 'on_chat_model_end', 'data': {'output': answer}}
                yield {'event': 'on_chain_end', 'parent_ids': [],
                       'data': {'output': {'messages': transcript}}}

        async def immediate_call(function, *args, **kwargs):
            return function(*args, **kwargs)

        def immediate_sync(function, **options):
            return lambda *args, **kwargs: immediate_call(function, *args, **kwargs)

        for enhanced in (False, True):
            config = {'llm': {'qa_enhance': enhanced, 'vision': True}, 'top_k': 5,
                      'user_id': self.user.pk}

            async def collect():
                return [event async for event in run_agent_stream(
                    'Describe the motor load chart.', 'linked-image-stream', '', config)]

            with self.subTest(enhanced=enhanced), \
                 patch('kb.agent.sync_to_async', immediate_sync), \
                 patch('langchain.agents.create_agent', side_effect=lambda **kwargs: ToolFlowAgent(kwargs['tools'])), \
                 patch('kb.agent._get_llm', return_value=object()), \
                 patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)), \
                 patch('kb.retriever.search', return_value=[dict(self.row)]), \
                 patch('kb.provenance.annotate_results'), \
                 patch('kb.keyword_index.get_chunks', return_value=[self.row]), \
                 patch('kb.qa_steps.plan_query', AsyncMock(return_value=('', {'input_tokens': 0, 'output_tokens': 0}))), \
                 patch('kb.qa_steps.verify_answer', AsyncMock(return_value=(
                     {'verdict': 'pass'}, {'input_tokens': 0, 'output_tokens': 0}))):
                # The fake model and sync dependencies complete immediately, so
                # no socket-backed asyncio event loop is needed on managed hosts.
                coroutine = collect()
                try:
                    pending = coroutine.send(None)
                except StopIteration as completed:
                    events = completed.value
                else:
                    coroutine.close()
                    self.fail(f'Unexpected external asynchronous work: {pending!r}')
            self.assertFalse([data for kind, data in events if kind == 'error'], events)
            self.assertIn('stable motor load', ''.join(data['text'] for kind, data in events if kind == 'token'))
            public = [data['citations'] for kind, data in events if kind == 'citations'][-1]
            self.assertEqual(public[0]['images'], [f'/kb/doc/{self.doc.pk}/img/{self.name}'])
            context = config['next_context']
            self.assertEqual(len(context['rag']['image_inputs']), 1)
            image_messages = [item['data']['content'] for item in context['messages']
                if item['type'] == 'tool' and isinstance(item['data']['content'], list)]
            self.assertEqual(len(image_messages), 1)
            self.assertEqual(image_messages[0][-1], IMAGE_BLOCK)
            self.assertIn('image_candidate_id', json.loads(image_messages[0][-2]['text']))
            with patch('kb.keyword_index.get_chunks', return_value=[self.row]):
                self.assertTrue(visible_citations(context['citations'], self.user))

    def test_saved_image_history_switches_to_text_only_and_can_explicitly_upgrade_later(self):
        from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage, messages_to_dict
        from kb.history import field_visibility_scope

        with patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)):
            tools, citations, evidence, state = self.tools()
            text_id, body = self.read_text(tools)
            image_id = body['linked_images'][0]['candidate_id']
            image = tools['kb_read_chunks']([image_id])
            tools['kb_select_evidence']([text_id, image_id])
        # Older stored providers may use any of the accepted image schemas.
        image.extend([{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + PNG}},
                      {'type': 'input_image', 'image_url': 'data:image/png;base64,' + PNG}])
        messages = [HumanMessage(content='Read this chart.'), AIMessage(content='', tool_calls=[{
            'name': 'kb_read_chunks', 'args': {'candidate_ids': [image_id]}, 'id': 'saved-image'}]),
            ToolMessage(content=image, tool_call_id='saved-image', name='kb_read_chunks',
                additional_kwargs={'keep_metadata': 'unchanged'}),
            AIMessage(content='The chart shows motor load [1].')]
        saved = {'version': 1, 'messages': messages_to_dict(messages), 'rag': deepcopy(state['session'].snapshot()),
            'citations': deepcopy(citations), 'evidence': deepcopy(evidence),
            'field_visibility': field_visibility_scope(self.user.pk)}
        original = deepcopy(saved)
        captured = {}

        class TextContinuationAgent:
            def __init__(self, functions):
                self.tools = {function.__name__: function for function in functions}

            async def astream_events(self, inputs, **options):
                captured['messages'] = inputs['messages']
                output = self.tools['kb_read_chunks']([image_id])
                captured['read'] = result_text(output)
                self.tools['kb_select_evidence']([text_id, image_id])
                answer = AIMessage(content='Only the image caption is available [1].',
                    response_metadata={'finish_reason': 'stop'})
                yield {'event': 'on_chat_model_start', 'data': {}}
                yield {'event': 'on_chat_model_stream', 'data': {
                    'chunk': AIMessageChunk(content=answer.content)}}
                yield {'event': 'on_chat_model_end', 'data': {'output': answer}}
                yield {'event': 'on_chain_end', 'parent_ids': [],
                    'data': {'output': {'messages': inputs['messages'] + [answer]}}}

        async def immediate_call(function, *args, **kwargs):
            return function(*args, **kwargs)

        def immediate_sync(function, **options):
            return lambda *args, **kwargs: immediate_call(function, *args, **kwargs)

        config = {'llm': {'vision': False}, 'top_k': 5, 'user_id': self.user.pk,
                  'conversation_context': saved}

        async def collect():
            return [event async for event in run_agent_stream(
                'What can you see now?', 'linked-image-capability-switch', '', config)]

        with patch('kb.agent.sync_to_async', immediate_sync), \
             patch('langchain.agents.create_agent', side_effect=lambda **kwargs: TextContinuationAgent(kwargs['tools'])), \
             patch('kb.agent._get_llm', return_value=object()), \
             patch('kb.agent._image_content_block') as reader, \
             patch('kb.keyword_index.get_chunks', return_value=[self.row]):
            coroutine = collect()
            try:
                pending = coroutine.send(None)
            except StopIteration as completed:
                events = completed.value
            else:
                coroutine.close()
                self.fail(f'Unexpected external asynchronous work: {pending!r}')
        reader.assert_not_called()
        self.assertFalse([data for kind, data in events if kind == 'error'], events)
        tool_message = next(message for message in captured['messages'] if isinstance(message, ToolMessage))
        self.assertEqual(tool_message.tool_call_id, 'saved-image')
        self.assertEqual(tool_message.additional_kwargs['keep_metadata'], 'unchanged')
        self.assertFalse(any(part.get('type') in ('image', 'image_url', 'input_image')
            for part in tool_message.content if isinstance(part, dict)))
        sanitized = result_text(tool_message.content)
        self.assertEqual(sanitized['image_inputs'], [{'candidate_id': image_id,
            'status': 'caption_only', 'reason': 'vision_disabled'}])
        self.assertEqual(sanitized['chunks'][0]['text'], 'Motor load trend')
        self.assertEqual(captured['read']['image_inputs'], [{'candidate_id': image_id,
            'status': 'caption_only', 'reason': 'vision_disabled'}])
        self.assertEqual(config['next_context']['rag']['image_inputs'], [])
        self.assertEqual(saved, original)

        with patch('kb.agent._image_content_block', return_value=dict(IMAGE_BLOCK)) as reader:
            tools, _, _, restored = self.tools(conversation_context=config['next_context'])
            upgraded = tools['kb_read_chunks']([image_id])
        self.assertEqual(result_text(upgraded)['image_inputs'],
            [{'candidate_id': image_id, 'status': 'delivered'}])
        self.assertEqual(upgraded[-1], IMAGE_BLOCK)
        self.assertIn(image_id, restored['session'].image_inputs)
        reader.assert_called_once_with(str(self.doc.pk), self.name)
