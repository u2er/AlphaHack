import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from profile.contract import MAIN_PAGE_LIMIT, ProfileDocument, chunk_pages, load_profile, render_for_prompt, table_of_contents
from profile.onboarding import Onboarding
from utils.llm import Completion

FIXTURES = Path(__file__).resolve().parents[1]/'fixtures'/'profiles'

class Store:
    def company(self, key):
        if key != 'U_1': raise ValueError('Company not found')
        return {'SYNTHETIC_U':'U_1','MAINOKVED_NAME':'Производство молочной продукции','CITY':'Тула'}
    def signals(self, key):
        return {'payments':[{'payer':key,'beneficiary':'U_2','amount':10,'count':1}],'shared_person_companies':[]}
    def search(self, query, limit=4):
        return [{'id':'bank-1','text':'Ставка 14% сохраняет дорогое финансирование','filename':'rate.pdf','page':1,'date_hint':'2026-09-11'}]
    def field_labels(self): return {}
    def field_value_labels(self): return {}

class LLM:
    def __init__(self, actions): self.actions, self.calls = list(actions), []
    def complete_structured(self, system, payload, model):
        self.calls.append(payload)
        return Completion(json.dumps(self.actions.pop(0)), {})

def act(tool, note='', query='', file_id='', page=0, question=None, draft=None):
    return dict(tool=tool, note=note, query=query, file_id=file_id, page=page, question=question, draft=draft)

def draft(main_sources=('form.activity','file:f1:1'), extra_ref='form.sensitivity'):
    return {'name':'ВекторФуд','user_role':'Финансовый директор','main':'## Чем занимается\n'+'Молочная продукция. '*20,
            'main_sources':list(main_sources),
            'pages':[{'id':'products','title':'Продукты и рынки','summary':'Ассортимент и каналы','content':'Молоко и сыры.',
                      'sources':['file:f1:1']},
                     {'id':'finance','title':'Финансы','summary':'Долг и ставки','content':'Плавающая ставка.',
                      'sources':['answer.1', extra_ref]}]}

def wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    return predicate()

def parse(path):
    return {'kind':'Текст','pages':[{'page':1,'text':'Годовой отчёт: выручка 80 млрд, сырое молоко 46% себестоимости','extraction':'text'},
                                   {'page':2,'text':'Кредитный портфель 32 млрд, 70% по плавающей ставке','extraction':'text'}]}

FORM = {'company_id':'U_1','activity':'Молочная продукция','role':'Финансовый директор',
        'sensitivity':['Ключевая ставка и кредиты','Цены на сырьё']}

class OnboardingTests(unittest.TestCase):
    def make(self, actions, tmp):
        llm = LLM(actions)
        session = Onboarding(Store(), lambda: llm, Path(tmp), parse=parse)
        return session, llm

    def test_chat_flow_from_form_and_file_to_confirmed_paged_profile(self):
        actions = [act('search_files', 'Ищу долговую нагрузку в отчёте', query='кредит ставка'),
                   act('read_file', 'Читаю финансовый раздел', file_id='f1', page=2),
                   act('ask', question={'text':'Можно ли быстро заменить импортную упаковку?','kind':'single',
                                        'options':['Да, за месяц','Нет, нужно 9–12 месяцев'],'allow_other':True}),
                   act('finish', 'Собираю профиль', draft=draft())]
        with tempfile.TemporaryDirectory() as tmp:
            session, llm = self.make(actions, tmp)
            self.assertEqual([m['kind'] for m in session.view()['messages']], ['text','form'])
            session.add_file('Отчёт.txt', b'text')
            self.assertTrue(wait(lambda: session.files['f1'].status == 'ready'))
            with self.assertRaises(ValueError): session.submit_form({'activity':'x'})  # role is required
            session.submit_form(FORM)
            self.assertTrue(wait(lambda: session.view()['question'] is not None))
            q = session.view()['question']
            self.assertEqual((q['kind'], q['ref']), ('single','answer.1'))
            with self.assertRaises(ValueError): session.answer(['a','b'])  # one choice expected
            session.answer('Нет, нужно 9–12 месяцев')
            self.assertTrue(wait(lambda: session.status == 'review'))
            doc = session.document
            self.assertEqual([p.id for p in doc.pages], ['main','products','finance','file-1','dataset','dialog'])
            self.assertIn('70% по плавающей ставке', doc.page('file-1').content)
            self.assertIn('Нет, нужно 9–12 месяцев', doc.page('dialog').content)
            self.assertNotIn('watch_topics', doc.model_dump())
            # what the agent saw: the form, the file catalogue, the dataset, then its tool results
            self.assertEqual(llm.calls[0]['files'][0]['id'], 'f1')
            self.assertEqual(llm.calls[0]['dataset']['facts']['CITY'], 'Тула')
            self.assertIn('file:f1:2', json.dumps(llm.calls[1]['history'], ensure_ascii=False))
            kinds = [m['kind'] for m in session.view()['messages']]
            self.assertIn('question', kinds); self.assertEqual(kinds[-1], 'profile')
            with self.assertRaises(ValueError):
                session.edit_page('main', content='x'*(MAIN_PAGE_LIMIT+1))
            session.edit_page('products', content='Молоко, сыры и десерты.')
            self.assertEqual(session.document.page('products').origin, 'user')
            published = []
            def fail_publish(_payload):
                raise RuntimeError('EventGraph unavailable')
            with self.assertRaisesRegex(RuntimeError, 'EventGraph unavailable'):
                session.confirm(fail_publish)
            self.assertEqual(session.status, 'review')
            result = session.confirm(lambda payload: published.append(payload) or Path(tmp)/'shared.json')
            self.assertEqual(result['profile']['schema_version'], '2')
            self.assertEqual(load_profile(published[0]).page('products').content, 'Молоко, сыры и десерты.')
            with self.assertRaises(ValueError): session.edit_page('products', content='поздно')
            session.set_sources(['lenta_economics','alfa_analytics'], lambda payload: published.append(payload))
            self.assertEqual(published[-1]['news_sources'], ['lenta_economics','alfa_analytics'])
            with self.assertRaises(ValueError): session.set_sources([])

    def test_invented_sources_are_sent_back_for_repair(self):
        actions = [act('finish', draft=draft(extra_ref='bank:invented')), act('finish', draft=draft(extra_ref='form.activity'))]
        with tempfile.TemporaryDirectory() as tmp:
            session, llm = self.make(actions, tmp)
            session.add_file('Отчёт.txt', b'text')
            self.assertTrue(wait(lambda: session.files['f1'].status == 'ready'))
            session.submit_form(FORM)
            self.assertTrue(wait(lambda: session.status in ('review','error')))
            self.assertIn('Unknown source refs', json.dumps(llm.calls[1]['json_repair'], ensure_ascii=False))
            # answer.1 does not exist either — no question was asked: the repair is rejected too
            self.assertEqual(session.status, 'error')

    def test_the_agent_has_no_bank_library_tool(self):
        from profile.onboarding import OnboardingAction, SYSTEM
        self.assertNotIn('search_library', OnboardingAction.model_json_schema()['properties']['tool']['enum'])
        self.assertNotIn('search_library', SYSTEM)

    def test_without_a_question_budget_the_agent_finishes_from_the_dataset(self):
        nulls = dict(act('finish', draft=draft(main_sources=('form.activity','dataset.CITY'), extra_ref='form.activity')),
                     query=None, file_id=None, page=None)  # models send null for another tool's fields
        for page in nulls['draft']['pages']: page['sources'] = ['form.activity']
        actions = [act('ask', question={'text':'Какие рынки?','kind':'text','options':[],'allow_other':False}), nulls]
        with tempfile.TemporaryDirectory() as tmp:
            session = Onboarding(Store(), lambda: LLM(actions), Path(tmp), max_questions=0)
            session.submit_form(FORM)
            self.assertTrue(wait(lambda: session.status in ('review','error')))
            self.assertEqual(session.status, 'review', session.error)
            self.assertEqual(session.answers, [])

    def test_questions_are_validated_and_answers_kept(self):
        actions = [act('ask', question={'text':'Какие рынки?','kind':'multi','options':['Россия'],'allow_other':False}),
                   act('ask', question={'text':'Какие рынки?','kind':'multi','options':['Россия','СНГ','ЕС'],'allow_other':False})]
        with tempfile.TemporaryDirectory() as tmp:
            session, llm = self.make(actions, tmp)
            session.submit_form(FORM)
            self.assertTrue(wait(lambda: session.view()['question'] is not None))
            self.assertIn('2–7 options', json.dumps(llm.calls[1]['json_repair'], ensure_ascii=False))
            session.answer(['Россия','СНГ'])
            self.assertEqual(session.answers[0]['answer'], 'Россия, СНГ')

    def test_sensitive_data_is_hidden_before_the_agent_and_the_user_chooses(self):
        def leaky(path):
            return {'kind':'Текст','pages':[
                {'page':1,'text':'Выручка 80 млрд. Директор: паспорт 45 08 123456, тел. +7 912 345-67-89. Пароль: hunter22'},
                {'page':2,'text':'Коммерческая тайна. Маржа по контракту с Магнитом 31%'}]}
        secrets = ['45 08 123456', '345-67-89', 'hunter22', 'Магнитом']
        for choice, visible, gone in [('Скрыть найденное (рекомендуется)', [], secrets),
                                      ('Оставить как есть', ['45 08 123456', 'Магнитом'], ['hunter22'])]:
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as tmp:
                finish = act('finish', draft=draft())
                for page in finish['draft']['pages']: page['sources'] = ['form.activity', 'file:f1:1']
                llm = LLM([finish])
                session = Onboarding(Store(), lambda: llm, Path(tmp), parse=leaky)
                session.add_file('Договор.txt', b'text')
                self.assertTrue(wait(lambda: session.files['f1'].status == 'ready'))
                self.assertEqual(list(Path(tmp, 'files').iterdir()), [])   # исходник не хранится
                session.submit_form(FORM | {'extra': 'Звонить +7 999 111-22-33'})
                self.assertTrue(wait(lambda: (session.view()['question'] or {}).get('ref') == 'privacy'))
                self.assertEqual(llm.calls, [])                            # агент ждёт выбора
                with self.assertRaises(ValueError): session.answer('что-то своё')
                session.answer(choice)
                self.assertTrue(wait(lambda: session.status in ('review', 'error')))
                self.assertEqual(session.answers, [])                      # выбор — не ответ о компании
                seen = json.dumps(llm.calls, ensure_ascii=False) + session.document.model_dump_json()
                for value in gone + ['111-22-33']:
                    self.assertNotIn(value, seen)
                for value in visible:
                    self.assertIn(value, seen)
                self.assertTrue(session.document.files[0].redactions)
                kinds = [m['kind'] for m in session.view()['messages']]
                self.assertIn('sensitive', kinds); self.assertIn('redaction', kinds)

    def test_unsupported_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, _ = self.make([], tmp)
            with self.assertRaises(ValueError): session.add_file('virus.exe', b'MZ')


class PagedContractTests(unittest.TestCase):
    def test_v2_fixture_main_page_toc_and_chunks(self):
        doc = load_profile(json.loads((FIXTURES/'vectorfood-v2.json').read_text(encoding='utf-8')))  # legacy watch_topics are dropped
        text = render_for_prompt(doc)
        self.assertIn('## Финансирование', text)                  # the main page is always there
        self.assertIn('[finance] Финансы и финансирование', text)  # other pages only by title
        self.assertNotIn('Чистый долг / EBITDA', text)
        self.assertEqual([c['page_id'] for c in chunk_pages(doc)][0], 'products')
        self.assertNotIn('main', {c['page_id'] for c in chunk_pages(doc)})
        self.assertEqual(table_of_contents(doc)[0]['id'], 'products')

    def test_v1_exports_become_pages(self):
        doc = load_profile(json.loads((FIXTURES/'vectorfood.json').read_text(encoding='utf-8')))
        self.assertEqual(doc.pages[0].id, 'main')
        self.assertEqual(doc.name, 'ВекторФуд')
        self.assertNotIn('Темы наблюдения', render_for_prompt(doc))

    def test_main_page_rules(self):
        base = json.loads((FIXTURES/'vectorfood-v2.json').read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            ProfileDocument.model_validate(base | {'pages': base['pages'][1:]})
        too_long = json.loads(json.dumps(base)); too_long['pages'][0]['content'] = 'x'*(MAIN_PAGE_LIMIT+1)
        with self.assertRaises(ValueError): ProfileDocument.model_validate(too_long)
        twice = json.loads(json.dumps(base)); twice['pages'].append(twice['pages'][1])
        with self.assertRaises(ValueError): ProfileDocument.model_validate(twice)

    def test_long_paragraphs_are_cut_into_fragments(self):
        base = json.loads((FIXTURES/'vectorfood-v2.json').read_text(encoding='utf-8'))
        base['pages'][1]['content'] = ' '.join(['слово']*800)
        chunks = [c for c in chunk_pages(ProfileDocument.model_validate(base)) if c['page_id'] == 'products']
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c['text']) <= 900 for c in chunks))


if __name__ == '__main__':
    unittest.main()
