from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from kb.models import Conversation,Message,KnowledgeBase

class ConversationBadgeTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('badge-reader')
        self.client.force_login(self.user)
        kb=KnowledgeBase.objects.create(name='badge',slug='badge')
        self.conv=Conversation.objects.create(user=self.user,kb=kb,thread_id='badge')

    def state(self):
        return self.client.get(reverse('kb:conv_states')).json()['conversations'][0]

    def test_running_completion_and_click(self):
        answer=Message.objects.create(conversation=self.conv,role='ai',content='',completion_status='running')
        self.assertTrue(self.state()['running'])
        self.assertFalse(self.state()['unread'])
        answer.content='done';answer.completion_status='complete';answer.save()
        self.assertTrue(self.state()['unread'])
        self.assertFalse(self.state()['running'])
        self.client.post(reverse('kb:conv_read',args=['badge']),{'answer_id':answer.pk})
        self.assertFalse(self.state()['unread'])

    def test_stale_click_does_not_clear_newer_answer(self):
        a=Message.objects.create(conversation=self.conv,role='ai',content='one')
        b=Message.objects.create(conversation=self.conv,role='ai',content='two')
        self.client.post(reverse('kb:conv_read',args=['badge']),{'answer_id':a.pk})
        self.assertTrue(self.state()['unread'])
        self.client.post(reverse('kb:conv_read',args=['badge']),{'answer_id':b.pk})
        self.client.post(reverse('kb:conv_read',args=['badge']),{'answer_id':a.pk})
        self.assertFalse(self.state()['unread'])

    def test_failure_is_not_unread_and_running_cannot_be_acknowledged(self):
        a=Message.objects.create(conversation=self.conv,role='ai',content='partial',completion_status='incomplete')
        self.assertFalse(self.state()['unread'])
        self.assertEqual(self.client.post(reverse('kb:conv_read',args=['badge']),{'answer_id':a.pk}).status_code,400)

    def test_other_user_cannot_read_or_acknowledge(self):
        other=get_user_model().objects.create_user('other-badge')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('kb:conv_states')).json()['conversations'],[])
        self.assertEqual(self.client.post(reverse('kb:conv_read',args=['badge']),{'answer_id':1}).status_code,404)
