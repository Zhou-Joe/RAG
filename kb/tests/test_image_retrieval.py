import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from kb.models import Document, KnowledgeBase
from kb.retriever import _image_lane, search_folder


def collection_result(items):
    return {'documents':[[item.get('text','image caption') for item in items]],
        'metadatas':[[{'source':'photo.jpg','type':'image','image':item['image']} for item in items]],
        'distances':[[item['distance'] for item in items]],
        'ids':[[item.get('id',item['image']) for item in items]]}


class ImageLaneTests(TestCase):
    def setUp(self):
        self.kbs = [KnowledgeBase.objects.create(name=f'Gallery {i}',slug=f'gallery-{i}') for i in range(2)]
        self.docs = [Document.objects.create(kb=kb,original_name='photo.jpg',status='completed') for kb in self.kbs]

    def run_lane(self, groups, **kwargs):
        stores = {kb.slug:SimpleNamespace(_collection=MagicMock()) for kb in self.kbs}
        for kb,items in zip(self.kbs,groups):
            stores[kb.slug]._collection.query.return_value = collection_result(items)
        with patch('kb.retriever.get_kb_vectorstore',side_effect=lambda slug:stores[slug]):
            return _image_lane([kb.slug for kb in self.kbs],[.1,.2],**kwargs)

    def test_missing_invalid_and_unrelated_distances_are_filtered_and_zero_ranks_first(self):
        results = self.run_lane([
            [{'image':'zero.jpg','distance':0.}, {'image':'normal.jpg','distance':.3},
             {'image':'missing.jpg','distance':None}, {'image':'nan.jpg','distance':math.nan},
             {'image':'infinite.jpg','distance':math.inf}, {'image':'negative.jpg','distance':-.2},
             {'image':'unrelated.jpg','distance':1.7}, {'image':'boolean.jpg','distance':False}],
            [{'image':'other.jpg','distance':.2}],
        ])
        self.assertEqual([r['image'] for r in results],['zero.jpg','other.jpg','normal.jpg'])
        self.assertEqual([r['distance'] for r in results],[0.,.2,.3])
        # Identical file names in different libraries keep the correct source.
        self.assertEqual(results[0]['doc_id'],str(self.docs[0].pk))
        self.assertEqual(results[1]['doc_id'],str(self.docs[1].pk))

    def test_images_are_deduplicated_before_the_global_eight_result_cap(self):
        results = self.run_lane([
            [{'image':'duplicate.jpg','distance':.05},{'image':'duplicate.jpg','id':'old-copy','distance':.06}]
            + [{'image':f'a-{i}.jpg','distance':.3+i*.05} for i in range(6)],
            [{'image':f'b-{i}.jpg','distance':.1+i*.05} for i in range(8)],
        ])
        self.assertEqual(len(results),8)
        self.assertEqual(sum(r['image']=='duplicate.jpg' for r in results),1)
        self.assertEqual([r['distance'] for r in results],sorted(r['distance'] for r in results))

    def test_no_query_embedding_returns_no_images(self):
        with patch('kb.retriever.get_kb_vectorstore') as store:
            self.assertEqual(_image_lane(['gallery-0'],None),[])
        store.assert_not_called()


class ImageFusionTests(SimpleTestCase):
    def test_image_panel_uses_only_scored_lane_results_and_reranks_text_only(self):
        text={'text':'maintenance instructions','source':'manual.pdf','kb_slug':'manual','score':.01}
        unscored={'text':'photo caption','source':'photo.jpg','type':'image','image':'same.jpg','score':1.}
        scored={**unscored,'distance':.4,'via':'img'}
        with patch('kb.retriever._without_generated_feed_documents',side_effect=lambda slugs:slugs), patch('kb.retriever._embeddings') as embeddings, patch('kb.retriever.search',return_value=[unscored,text]), patch(
            'kb.retriever._merge_graph',
        ), patch('kb.rerank.rerank_settings',return_value={'enabled':True}), patch('kb.rerank.rerank') as rerank, patch(
            'kb.retriever._image_lane',return_value=[scored],
        ):
            embeddings.return_value.embed_query.return_value=[.1,.2]
            result=search_folder(['manual'],'query',k=8,include_images=True)
        self.assertEqual(result,[text,scored])
        rerank.assert_not_called()  # one text result; images do not enter reranking
        self.assertEqual([r for r in result if r.get('type')=='image'],[scored])

    def test_normal_agent_candidate_search_keeps_its_existing_image_candidates(self):
        image={'text':'caption','source':'photo.jpg','type':'image','image':'a.jpg','score':.1}
        with patch('kb.retriever._without_generated_feed_documents',side_effect=lambda slugs:slugs),patch('kb.retriever._embeddings'),patch('kb.retriever.search',return_value=[image]),patch(
            'kb.retriever._merge_graph',
        ),patch('kb.rerank.rerank_settings',return_value={'enabled':False}),patch('kb.retriever._image_lane') as lane:
            self.assertEqual(search_folder(['manual'],'query',k=8,include_images=False),[image])
        lane.assert_not_called()


class ImageSearchPageTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('image-search-reader')
        self.kb=KnowledgeBase.objects.create(name='Search gallery',slug='search-gallery',chunk_count=10)
        self.doc=Document.objects.create(kb=self.kb,original_name='photo.jpg',status='completed')
        self.client.force_login(self.user)

    def image(self, name, distance=None, **extra):
        return {'doc_id':str(self.doc.pk),'source':'photo.jpg','type':'image','image':name,
                'text':'photo caption','via':'img','distance':distance,**extra}

    def request(self, hits):
        with patch('kb.retriever.search_folder',return_value=hits),patch('kb.provenance.annotate_results'):
            return self.client.get(reverse('kb:search'),{'q':'image query'})

    def test_unscored_and_out_of_range_images_never_render_even_with_a_rerank_score(self):
        response=self.request([self.image('valid.jpg',.4),self.image('missing.jpg',rerank_score=.99),
            self.image('distant.jpg',1.8,rerank_score=1.),self.image('nan.jpg',math.nan)])
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.context['image_matches']),1)
        self.assertEqual(response.context['image_matches'][0]['score'],.8)
        self.assertContains(response,'/img/valid.jpg')
        for name in ('missing.jpg','distant.jpg','nan.jpg'):
            self.assertNotContains(response,'/img/'+name)
        self.assertContains(response,'class="rel-badge img-rel"',count=1)
        self.assertContains(response,'已找到「image query」的综合结果')

    def test_page_sorts_images_by_vector_score_and_caps_the_panel(self):
        response=self.request([self.image(f'image-{i}.jpg',i/10,rerank_score=1.) for i in reversed(range(12))])
        scores=[r['score'] for r in response.context['image_matches']]
        self.assertEqual(len(scores),8)
        self.assertEqual(scores,sorted(scores,reverse=True))
        self.assertEqual(scores[0],1.)
        self.assertContains(response,'class="rel-badge img-rel"',count=8)

    def test_zero_text_score_still_has_a_bar(self):
        response=self.request([{'doc_id':str(self.doc.pk),'source':'manual.pdf','text':'instructions',
                               'via':'kw+rr','rerank_score':0.}])
        self.assertContains(response,'<i>0%</i>')
