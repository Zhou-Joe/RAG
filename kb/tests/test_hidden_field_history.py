"""Hidden business fields must not return through old tool results or prose."""
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.test import TestCase
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from kb.history import (
    conversation_history, field_visibility_current, field_visibility_scope, load_context,
)
from kb.models import Conversation, KnowledgeBase, Message


class HiddenFieldHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('hidden-field-history')
        self.other = get_user_model().objects.create_user('hidden-field-other')
        self.kb = KnowledgeBase.objects.create(
            name='Business metrics', slug='business-metrics', category='business',
        )
        self.context = {
            'version': 1,
            'messages': messages_to_dict([
                HumanMessage(content='Describe the business fields.'),
                AIMessage(content='', tool_calls=[{
                    'id': 'preview', 'name': 'tracker_preview', 'args': {'kb_slug': self.kb.slug},
                }]),
                ToolMessage(content='{"fields":["Useful","Unnecessary"],"sample":"HIDDEN_VALUE"}',
                            tool_call_id='preview'),
                AIMessage(content='The unnecessary field contains HIDDEN_VALUE.'),
            ]),
            'citations': [], 'evidence': [], 'rag': {}, 'field_visibility': {},
        }
        self.conv = Conversation.objects.create(
            user=self.user, thread_id='hidden-field-history', agent_context=self.context,
        )
        Message.objects.create(conversation=self.conv, role='user', content='Describe the fields.')
        Message.objects.create(conversation=self.conv, role='ai', content='HIDDEN_VALUE', completion_status='complete')

    def restrict(self):
        self.kb.active_fields = ['Useful']
        self.kb.save(update_fields=['active_fields'])

    def test_preview_only_context_is_invalidated_when_a_field_is_hidden(self):
        self.assertEqual(load_context(self.conv, self.user), self.context)
        self.restrict()
        self.assertEqual(load_context(self.conv, self.user), {})
        self.assertFalse(field_visibility_current(self.context, self.user))
        self.conv.refresh_from_db()
        self.assertIn('HIDDEN_VALUE', repr(self.conv.agent_context))
        self.assertEqual(self.conv.messages.count(), 2)

    def test_policy_change_does_not_reconstruct_stale_prose_as_published_history(self):
        self.restrict()
        context, published = conversation_history(self.conv, self.user)
        self.assertEqual(context, {})
        self.assertEqual(published, [])
        self.assertEqual(self.conv.messages.filter(content='HIDDEN_VALUE').count(), 1)

    def test_unchanged_restricted_policy_keeps_the_valid_transcript(self):
        self.restrict()
        self.context['field_visibility'] = field_visibility_scope(self.user)
        self.conv.agent_context = self.context
        self.assertEqual(load_context(self.conv, self.user), self.context)
        self.assertEqual(conversation_history(self.conv, self.user), (self.context, []))

    def test_legacy_context_requires_fresh_queries_when_any_accessible_business_scope_is_restricted(self):
        legacy = deepcopy(self.context)
        legacy.pop('field_visibility')
        self.conv.agent_context = legacy
        self.assertEqual(load_context(self.conv, self.user), legacy)
        self.restrict()
        self.assertEqual(load_context(self.conv, self.user), {})
        self.assertEqual(conversation_history(self.conv, self.user), ({}, []))

    def test_scope_ignores_nonapplicable_manuals_and_inaccessible_individual_libraries(self):
        KnowledgeBase.objects.create(name='Manual', slug='history-manual', active_fields=['Manual field'])
        KnowledgeBase.objects.create(
            name='Private', slug='history-private', category='business', active_fields=['Private field'],
            visibility='individual', created_by=self.other,
        )
        self.assertEqual(field_visibility_scope(self.user), {})
        table = KnowledgeBase.objects.create(
            name='Table', slug='history-table', table_query=True, active_fields=['Selected'],
        )
        self.assertEqual(field_visibility_scope(self.user), {table.slug: ['Selected']})
        self.assertEqual(field_visibility_scope(self.user.pk), {table.slug: ['Selected']})

    def test_legacy_published_history_remains_available_without_restricted_fields(self):
        self.conv.agent_context = {}
        context, published = conversation_history(self.conv, self.user)
        self.assertEqual(context, {})
        self.assertEqual([item['content'] for item in published], ['Describe the fields.', 'HIDDEN_VALUE'])

    def test_unhiding_and_selection_changes_refresh_previously_stamped_context(self):
        self.restrict()
        self.context['field_visibility'] = field_visibility_scope(self.user)
        self.conv.agent_context = self.context
        self.kb.active_fields = ['Other']
        self.kb.save(update_fields=['active_fields'])
        self.assertEqual(load_context(self.conv, self.user), {})
        self.kb.active_fields = None
        self.kb.save(update_fields=['active_fields'])
        self.assertEqual(load_context(self.conv, self.user), {})
