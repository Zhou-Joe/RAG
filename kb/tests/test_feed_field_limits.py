"""Long metadata survives connector validation and tracker publication intact."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from kb.connector_runtime import build_batch
from kb.feed_protocol import (
    MAX_FIELDS_CHARS, MAX_METADATA_CHARS, SCHEMA, canonical, load_batch, validate_batch,
)
from kb.feeds import claim, execute
from kb.models import KnowledgeBase, KnowledgeFeed, TrackerRow


def payload():
    return {'schema': SCHEMA, 'items': [{'id': 'one', 'title': 'Report', 'content': 'Source'}]}


class FeedFieldBoundaryTests(SimpleTestCase):
    def test_fields_and_metadata_accept_exact_serialized_limit(self):
        for name, limit in (('fields', MAX_FIELDS_CHARS), ('metadata', MAX_METADATA_CHARS)):
            with self.subTest(name=name):
                value = {'正文': '数' * (limit - len(canonical({'正文': ''})))}
                data = payload()
                data['items'][0][name] = value
                self.assertEqual(len(canonical(value)), 20000)
                self.assertEqual(load_batch(canonical(data).encode())['items'][0][name], value)
                value['正文'] += '数'
                with self.assertRaisesMessage(ValueError, '20000'):
                    validate_batch(data)

    def test_fields_remain_scalars_and_metadata_remains_object(self):
        for name, value in (('fields', {'raw': {'nested': 1}}), ('metadata', 'text')):
            data = payload()
            data['items'][0][name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_batch(data)


class LongMetadataPublicationTests(TestCase):
    def test_whole_object_mapping_is_saved_without_truncation(self):
        metadata = {'正文': '完整原文' * 3000, '设备': 'A-1'}
        config = {
            'name': 'Metadata feed', 'url': 'https://api.example.test/data',
            'records_path': 'records', 'id_paths': ['id'],
            'body_fields': [{'label': '完整元数据', 'path': 'metadata'}],
            'metadata_paths': {'raw': 'metadata'},
        }
        data = validate_batch(build_batch(config, {'records': [{'id': 'one', 'metadata': metadata}]}))
        saved_value = data['items'][0]['fields']['完整元数据']
        self.assertGreater(len(saved_value), 8000)
        self.assertEqual(json.loads(saved_value), metadata)

        user = get_user_model().objects.create_user('long-metadata-builder', is_staff=True)
        kb = KnowledgeBase.objects.create(name='Metadata', slug='long-metadata', is_folder=True,
                                          created_by=user)
        feed = KnowledgeFeed.objects.create(kb=kb, name=kb.name, mode='push', created_by=user)
        run, _ = claim(feed.pk, trigger='push', payload=data)
        with patch('kb.feed_reports.after_run'):
            result = execute(run.pk)
        self.assertEqual(result.status, 'completed', result.error)
        self.assertEqual(TrackerRow.objects.get(feed=feed).values['完整元数据'], saved_value)
        self.assertEqual(feed.records.get().metadata['raw'], metadata)
