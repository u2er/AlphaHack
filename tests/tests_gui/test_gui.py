import json
import csv
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from gui.controller import Controller
from gui.server import create_server
from profile.db.field_labels import FIELDS, VALUE_LABELS, ensure_field_labels
from utils.llm import Completion

FIXTURES = Path(__file__).resolve().parents[1]/'fixtures'/'profiles'


class FakeStore:
    def field_labels(self): return {}
    def field_value_labels(self): return {}
    def companies(self, query='', limit=20):
        return [{'SYNTHETIC_U':'U_1','MAINOKVED_NAME':'Металлы','REGION':'Томская область'}] if query else []
    def company(self, company_id):
        if company_id != 'U_1': raise ValueError('Unknown company')
        return {'SYNTHETIC_U':'U_1','MAINOKVED_NAME':'Металлы','CITY':'Томск'}
    def signals(self, company_id):
        return {'payments':[], 'shared_person_companies':[]}
    def search(self, query, limit=4):
        return [{'id':'source:1','text':'Стоимость кредита зависит от ставки','filename':'bank.pdf',
                 'page':1,'date_hint':'2026','score':.8}]


def action(tool, **kw):
    return {'tool':tool,'note':kw.get('note',''),'query':kw.get('query',''),'file_id':kw.get('file_id',''),
            'page':kw.get('page',0),'question':kw.get('question'),'draft':kw.get('draft')}


class FakeClient:
    """One scripted onboarding: library search, a free-text question, finish."""
    def __init__(self):
        self.responses = [
            action('ask', question={'text':'Есть ли кредиты с плавающей ставкой?','kind':'text','options':[],'allow_other':False}),
            action('finish', draft={'name':'Металл Сибирь','user_role':'Финансовый директор',
                                    'main':'## Чем занимается\n'+'Сортовой прокат для стройки. '*10,
                                    'main_sources':['form.activity'],
                                    'pages':[{'id':'finance','title':'Финансы','summary':'Кредиты',
                                              'content':'Кредит с плавающей ставкой','sources':['answer.1']}]})]
    def complete_structured(self, system, payload, response_model):
        return Completion(json.dumps(self.responses.pop(0)), {})
    def close(self): pass


def parse(path):
    return {'kind':'Текст','pages':[{'page':1,'text':Path(path).read_text(encoding='utf-8'),'extraction':'text'}]}


class Server:
    def __init__(self, tmp, insights=None):
        self.client = FakeClient()
        self.published = []
        controller = Controller(FakeStore(), lambda: self.client, Path(tmp), embedder=None, parse=parse)
        controller.publish = lambda payload: self.published.append(payload) or Path(tmp)/'shared.json'
        self.server = create_server(port=0, controller=controller, insights=insights)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
    def __enter__(self):
        self.thread.start(); return self
    def __exit__(self, *exc):
        self.server.shutdown(); self.server.server_close()
    def get(self, path):
        with urlopen(self.base+path) as response:
            return json.load(response)
    def post(self, path, body=None, data=None, headers=None):
        request = Request(self.base+path, data=data if data is not None else json.dumps(body or {}).encode(),
                          headers=headers or {'Content-Type':'application/json'}, method='POST')
        with urlopen(request) as response:
            return json.load(response)
    def wait(self, sid, predicate, timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            view = self.get(f'/api/onboarding/{sid}')
            if predicate(view): return view
            time.sleep(.02)
        raise AssertionError(f'timeout, last status {view["status"]}')


class GuiTests(unittest.TestCase):
    def test_onboarding_chat_over_http(self):
        with tempfile.TemporaryDirectory() as tmp, Server(tmp) as s:
            page = urlopen(s.base+'/').read().decode()
            self.assertIn('Добавить компанию', page)
            for gone in ('Счета', 'Платежи', 'Зарплатный проект', 'Кредиты'):
                self.assertNotIn(gone, page)
            self.assertEqual(s.get('/api/companies?q=met')['companies'][0]['id'], 'U_1')
            view = s.post('/api/onboarding')
            sid = view['id']
            self.assertEqual(view['messages'][-1]['kind'], 'form')
            s.post(f'/api/onboarding/{sid}/files', data='Годовой отчёт: прокат и арматура'.encode(),
                   headers={'X-Filename': quote('Отчёт 2025.txt')})
            s.wait(sid, lambda v: v['files'][0]['status'] == 'ready')
            with self.assertRaises(HTTPError) as error:
                s.post(f'/api/onboarding/{sid}/form', {'form':{'activity':'Прокат'}})
            self.assertEqual(error.exception.code, 400)
            s.post(f'/api/onboarding/{sid}/form', {'form':{'company_name':'Металл Сибирь','activity':'Прокат',
                                                            'role':'Финансовый директор','sensitivity':['Ключевая ставка и кредиты']}})
            view = s.wait(sid, lambda v: v['question'] is not None)
            self.assertEqual(view['question']['kind'], 'text')
            s.post(f'/api/onboarding/{sid}/answer', {'value':'Да, 70% портфеля'})
            view = s.wait(sid, lambda v: v['status'] == 'review')
            pages = [p['id'] for p in view['profile']['pages']]
            self.assertEqual(pages, ['main','finance','file-1','dialog'])
            view = s.post(f'/api/onboarding/{sid}/pages/finance', {'content':'Кредит 2 млрд, плавающая ставка'})
            self.assertEqual(view['profile']['pages'][1]['content'], 'Кредит 2 млрд, плавающая ставка')
            result = s.post(f'/api/onboarding/{sid}/confirm')
            self.assertTrue(result['published'])
            self.assertEqual(s.published[0]['name'], 'Металл Сибирь')
            trace = (Path(tmp)/sid/'trace.jsonl').read_text(encoding='utf-8')
            self.assertIn('profile_confirmed', trace)

    def test_insights_proxy_keeps_the_key_server_side_and_reads_pages(self):
        import httpx
        from gui.insights import Insights
        payload = json.loads((FIXTURES/'vectorfood-v2.json').read_text(encoding='utf-8'))
        seen = []
        def handler(request):
            seen.append((request.url.path, request.headers.get('X-Api-Key')))
            if request.url.path == '/profiles':
                return httpx.Response(200, json=[{'id':'p1','name':'Клиент','active':True},{'id':'p0','name':'Старый','active':False}])
            if request.url.path == '/profiles/p1':
                return httpx.Response(200, json={'id':'p1','name':'ВекторФуд','payload':payload})
            if request.url.path == '/profiles/p1/pages/finance':
                return httpx.Response(200, json={'ok':True,'id':'finance','content':'Кредитный портфель 32 млрд'})
            if request.url.path == '/profiles/p1/feed':
                return httpx.Response(200, json=[{'uid':'01A','title':'ЦБ сохранил ставку','analysis':{'importance':4,'impact':'Дорогой кредит'}}])
            return httpx.Response(404, json={})
        insights = Insights(store=FakeStore(), base_url='http://eventgraph', api_key='secret', transport=httpx.MockTransport(handler))
        self.assertEqual([p['id'] for p in insights.profiles()], ['p1'])
        profile = insights.profile('p1')
        self.assertIn('## Финансирование', profile['main'])
        self.assertEqual(profile['toc'][0]['id'], 'products')
        self.assertNotIn('topics', profile)
        self.assertIn('32 млрд', insights.page('p1', 'finance')['content'])
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, insights) as s:
            item = s.get('/api/insights/profiles/p1/feed?min_importance=3')['items'][0]
            self.assertEqual(item['bank'][0]['filename'], 'bank.pdf')
            self.assertEqual(s.get('/api/insights/profiles/p1/pages/finance')['id'], 'finance')
            scripts = urlopen(s.base+'/bank.js').read().decode()+urlopen(s.base+'/onboarding.js').read().decode()
            self.assertNotIn('secret', scripts)
        self.assertIn(('/profiles/p1/feed','secret'), seen)

    def test_graph_chat_proxy_forwards_message_and_history(self):
        import httpx
        from gui.insights import Insights
        seen = []

        def handler(request):
            seen.append((request.url.path, json.loads(request.content), request.headers.get('X-Api-Key')))
            return httpx.Response(200, json={'reply': 'Связи в графе проверены'})

        insights = Insights(base_url='http://eventgraph', api_key='secret', transport=httpx.MockTransport(handler))
        history = [{'role': 'user', 'content': 'Расскажи о событиях'}]

        self.assertEqual(insights.graph_chat('Какие причины?', history), {'reply': 'Связи в графе проверены'})
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, insights) as s:
            result = s.post('/api/insights/graph-chat', {'message': 'Какие причины?', 'history': history})
        self.assertEqual(result, {'reply': 'Связи в графе проверены'})
        expected = ('/ai/graph-chat', {'message': 'Какие причины?', 'history': history}, 'secret')
        self.assertEqual(seen, [expected, expected])

    def test_connectors_sources_and_graph_go_through_the_proxy(self):
        import httpx
        from gui.insights import Insights
        calls = []
        connectors = [{'name':'lenta_economics','kind':'website','description':'Lenta.ru — экономика','enabled':True,
                       'paused':False,'items_count':3,'config':{'secret':'x'}},
                      {'name':'alfa_investor_site','kind':'website','description':'выключен','enabled':False,'paused':False,'items_count':0}]
        def handler(request):
            calls.append((request.method, request.url.path, request.content.decode() if request.content else ''))
            path = request.url.path
            if path == '/connectors': return httpx.Response(200, json=connectors)
            if path == '/connectors/lenta_economics/pause': return httpx.Response(200, json=connectors[0] | {'paused': True})
            if path == '/connectors/lenta_economics/state/reset': return httpx.Response(200, json=connectors[0])
            if path == '/profiles/p1/sources' and request.method == 'PUT':
                return httpx.Response(200, json={'id':'p1','news_sources':json.loads(request.content)['sources']})
            if path == '/profiles' and request.method == 'PUT':
                return httpx.Response(201, json={'id':json.loads(request.content)['profile_id']})
            if path == '/health': return httpx.Response(200, json={'graph':'news'})
            if path == '/graphs/news':
                return httpx.Response(200, json={'graph':'news','nodes':[{'id':'a','name':'ЦБ','meta':{'type':'org'},'item_count':0}],
                                                 'edges':[{'id':'e','name':'about','source_id':'a','target_id':'a'}]})
            return httpx.Response(404, json={})
        insights = Insights(store=FakeStore(), base_url='http://eventgraph', api_key='secret', transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, insights) as s:
            listed = s.get('/api/insights/connectors')['connectors']
            self.assertEqual([c['name'] for c in listed], ['lenta_economics','alfa_investor_site'])
            self.assertNotIn('config', listed[0])  # connector settings stay on the server
            self.assertTrue(s.post('/api/insights/connectors/lenta_economics/pause')['paused'])
            s.post('/api/insights/connectors/lenta_economics/reset')
            with self.assertRaises(HTTPError) as error:
                s.post('/api/insights/connectors/lenta_economics/delete')
            self.assertEqual(error.exception.code, 404)
            saved = s.post('/api/insights/profiles/p1/sources', {'sources':['lenta_economics']})
            self.assertEqual(saved['news_sources'], ['lenta_economics'])
            graph = s.get('/api/insights/graph')
            self.assertEqual((graph['nodes'][0]['name'], graph['edges'][0]['source']), ('ЦБ', 'a'))
            # onboarding: sources after confirmation, only enabled connectors
            sid = s.post('/api/onboarding')['id']
            s.post(f'/api/onboarding/{sid}/form', {'form':{'activity':'Прокат','role':'Финансовый директор'}})
            s.wait(sid, lambda v: v['question'] is not None)
            s.post(f'/api/onboarding/{sid}/answer', {'value':'Да'})
            s.wait(sid, lambda v: v['status'] == 'review')
            s.post(f'/api/onboarding/{sid}/confirm')
            with self.assertRaises(HTTPError):
                s.post(f'/api/onboarding/{sid}/sources', {'sources':['alfa_investor_site']})
            view = s.post(f'/api/onboarding/{sid}/sources', {'sources':['lenta_economics']})
            self.assertEqual(view['profile']['news_sources'], ['lenta_economics'])
            self.assertEqual(s.published[-1]['news_sources'], ['lenta_economics'])
        self.assertIn(('POST', '/connectors/lenta_economics/state/reset', ''), calls)
        self.assertTrue(any(method == 'PUT' and path == '/profiles' for method, path, _ in calls))

    def test_insights_without_eventgraph_report_a_readable_error(self):
        from gui.insights import Insights, InsightsError
        with self.assertRaisesRegex(InsightsError, 'API_KEY'):
            Insights(api_key='').profiles()
        with self.assertRaisesRegex(InsightsError, 'недоступен'):
            Insights(base_url='http://127.0.0.1:9', api_key='k').profiles()

    def test_library_not_ready_is_a_readable_error(self):
        class Empty(FakeStore):
            def field_labels(self): raise RuntimeError('relation "profile_data.field_labels" does not exist')
        controller = Controller(Empty(), FakeClient, tempfile.mkdtemp(), embedder=None)
        with self.assertRaisesRegex(ValueError, 'profile-prepare'):
            controller.companies('U_1')
        controller.library_loading = True  # the GUI loads appendix 1 itself on start
        with self.assertRaisesRegex(ValueError, 'загружается'):
            controller.companies('U_1')

    def test_the_company_library_is_loaded_on_start_only_when_missing(self):
        import profile.db.prepare as prepare
        calls = []
        original = prepare.ensure_companies
        prepare.ensure_companies = lambda dsn=None: calls.append(dsn) or True
        try:
            store = FakeStore(); store.dsn = 'postgresql://x'
            controller = Controller(store, FakeClient, tempfile.mkdtemp(), embedder=None)
            controller.prepare_library().join(5)
        finally:
            prepare.ensure_companies = original
        self.assertEqual(calls, ['postgresql://x'])
        self.assertFalse(controller.library_loading)

    def test_field_catalog_covers_csv_and_updates_without_changing_data(self):
        root=Path(__file__).resolve().parents[2]
        for csv_path in (root/'формулировка и данные хакатона'/'приложение 1').glob('*.csv'):
            with csv_path.open(encoding='utf-8-sig',newline='') as source:
                keys=csv.DictReader(source,delimiter=';').fieldnames
            self.assertTrue(set(keys)<=set(key for key,*_ in FIELDS),csv_path.name)
        class Connection:
            def __init__(self):self.calls=[]
            def cursor(self):return self
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,sql):self.calls.append((sql,None))
            def executemany(self,sql,params):self.calls.append((sql,list(params)))
        connection=Connection()
        ensure_field_labels(connection)
        self.assertTrue(all('profile_data.' in sql or 'CREATE SCHEMA' in sql for sql,_ in connection.calls))
        self.assertEqual(len(connection.calls[2][1]),len(FIELDS))
        self.assertEqual(len(connection.calls[4][1]),len(VALUE_LABELS))

if __name__=='__main__':unittest.main()
