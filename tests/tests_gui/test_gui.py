import json
import csv
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from gui.controller import Controller
from gui.server import create_server
from profile.db.field_labels import FIELDS, VALUE_LABELS, ensure_field_labels
from utils.llm import Completion

class FakeStore:
    def field_labels(self):
        return {key:{'field_key':key,'label':label,'description':description,
                     'section':section,'sort_order':i}
                for i,(key,label,description,section) in enumerate(FIELDS)}
    def field_value_labels(self):
        result={}
        for key,raw,label in VALUE_LABELS:
            result.setdefault(key,{})[raw]=label
        return result
    def companies(self,query='',limit=20):
        return [{'SYNTHETIC_U':'U_1','MAINOKVED_NAME':'Металлы','REGION':'Томская область'}] if query else []
    def company(self,company_id):
        if company_id!='U_1':raise ValueError('Unknown company')
        return {'SYNTHETIC_U':'U_1','MAINOKVED_NAME':'Металлы','CITY':'Томск','REVENUE_COMPANY':'100000000'}
    def signals(self,company_id):
        return {'payments':[{'payer':'U_1','beneficiary':'U_2','amount':200,'count':2}],
                'shared_person_companies':[]}
    def search(self,query,limit=4):
        return [{'id':'source:1','text':'Стоимость кредита зависит от ставки','filename':'bank.pdf',
                 'page':1,'date_hint':'2026','date_warning':'Проверить дату','score':.8}]

class FakeClient:
    provider='test'
    model='test'
    def __init__(self):
        self.lock=threading.Lock()
        self.responses=[
            {'summary':'По данным набора — металлургия.','claims':[
                {'kind':'fact','text':'Отрасль указана в наборе.','source_keys':['MAINOKVED_NAME']},
                {'kind':'signal','text':'Платежи наблюдаются.','source_keys':['signals.payments']},
                {'kind':'question','text':'Уточните рынки продаж.','source_keys':['MAINOKVED_NAME']}]},
            {'scope':'facts','key':'CITY','value':'Томск','explanation':'Исправление города'},
            {'tool':'search_library','query':'ставка','question':'','assessment':None},
            {'tool':'ask_user','query':'','question':'Есть ли кредит?','assessment':None},
            {'tool':'finish','query':'','question':'','assessment':{
                'topics':[{'title':'Ставка','rationale':'Ответ пользователя о кредитах',
                           'profile_fields':['sensitivity'],'source_ids':['source:1']}],
                'limitations':['Требуется проверка']}}
        ]
    def complete_structured(self,system,payload,response_model):
        with self.lock:
            return Completion(json.dumps(self.responses.pop(0)),{})
    def close(self):pass

class GuiTests(unittest.TestCase):
    def test_controller_full_flow_and_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=FakeClient()
            controller=Controller(FakeStore(),lambda:client,Path(tmp))
            session=controller.create(company_id='U_1')
            self.assertEqual(session.view()['field_labels']['MAINOKVED_NAME']['label'],'Основной вид деятельности')
            self.assertEqual(session.view()['field_value_labels']['CL_TYPE']['CLIENT'],'Клиент банка')
            self.assertEqual(session.view()['profile']['source_facts']['CITY']['value'],'Томск')
            deadline=time.monotonic()+5
            while session.view()['status']=='overview_running' and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(session.view()['profile']['overview']['claims'])
            session.propose_edit('Город регистрации Томск')
            self.assertEqual(session.view()['proposal']['key'],'CITY')
            session.decide_edit(False)
            self.assertEqual(session.view()['profile']['facts']['CITY']['source'],'appendix1:df_Uid_data_hack.csv')
            from profile.service import QUESTIONS
            for key in QUESTIONS:
                session.edit_key('answers',key,'Стоимость кредита' if key=='sensitivity' else 'не знаю')
            session.confirm_profile()
            self.assertTrue(session.view()['profile']['profile_confirmed'])
            deadline=time.monotonic()+5
            while session.view()['status']=='agent_running' and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(session.view()['status'],'awaiting_answer')
            self.assertEqual(session.view()['pending_question'],'Есть ли кредит?')
            session.answer_agent('Да, есть кредит')
            while session.view()['status'] in {'agent_running','awaiting_answer'} and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(session.view()['status'],'assessment_ready')
            session.confirm_assessment()
            session.sites(['example.com'])
            exported=session.export()
            self.assertEqual(exported['profile']['monitoring_context']['topics'][0]['title'],'Ставка')
            self.assertEqual(exported['profile']['source_facts']['CITY']['value'],'Томск')
            kinds={e['event'] for e in session.trace_events(200)}
            self.assertTrue({'llm_request','llm_response','db_result','agent_question','agent_answer','profile_exported'}<=kinds)
            requests=[e for e in session.trace_events(200) if e['event']=='llm_request']
            self.assertTrue(all('field_metadata' in e['payload'] for e in requests))
            self.assertTrue(all('REVENUE_COMPANY' in e['payload']['field_metadata'] for e in requests))
            self.assertEqual(Path(exported['trace_path']).stat().st_mode & 0o777,0o600)
            controller.close()

    def test_manual_empty_fields_and_skipped_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=FakeClient()
            client.responses[0]={'summary':'Указан тип организации.','claims':[
                {'kind':'fact','text':'Тип организации указан пользователем.','source_keys':['ORG_TYPE']},
                {'kind':'question','text':'Уточните деятельность.','source_keys':['ORG_TYPE']},
                {'kind':'question','text':'Уточните географию.','source_keys':['ORG_TYPE']}]}
            controller=Controller(FakeStore(),lambda:client,Path(tmp))
            session=controller.create(manual=True)
            self.assertEqual(session.view()['profile']['facts'],{})
            session.edit_key('answers','geography','')
            self.assertEqual(session.view()['status'],'selected')
            session.submit_manual({'ORG_TYPE':'КОММ_ОРГ','CITY':'','name':''})
            deadline=time.monotonic()+5
            while session.view()['status']=='overview_running' and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(session.view()['status'],'draft_ready')
            self.assertEqual(session.view()['profile']['facts']['ORG_TYPE']['value'],'КОММ_ОРГ')
            session.edit_key('answers','activity','Оптовая торговля')
            session.edit_key('answers','activity','')
            client.responses=[
                {'tool':'search_library','query':'торговля','question':'','assessment':None},
                {'tool':'finish','query':'','question':'','assessment':{
                    'topics':[],'limitations':['Недостаточно подтверждённых данных']}}]
            session.confirm_profile()
            self.assertEqual(session.view()['profile']['answers'],{})
            deadline=time.monotonic()+5
            while session.view()['status']=='agent_running' and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(session.view()['status'],'assessment_ready')
            controller.close()

    def test_empty_manual_profile_can_generate_neutral_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(FakeStore(),lambda:None,Path(tmp))
            session=controller.create(manual=True)
            session.submit_manual({})
            deadline=time.monotonic()+5
            while session.view()['status']=='overview_running' and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(session.view()['status'],'draft_ready')
            self.assertEqual(session.view()['profile']['overview']['claims'],[])
            controller.close()

    def test_http_serves_app_and_selects_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(FakeStore(),FakeClient,Path(tmp))
            server=create_server(port=0,controller=controller)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/') as response:
                    page=response.read()
                    self.assertIn(b'<title>',page)
                    self.assertNotIn(b'id="agentButton"',page)
                with urlopen(base+'/api/companies?q=met') as response:
                    self.assertEqual(json.load(response)['companies'][0]['id'],'U_1')
                request=Request(base+'/api/sessions',data=b'{"company_id":"U_1"}',
                                headers={'Content-Type':'application/json'},method='POST')
                with urlopen(request) as response:
                    session=json.load(response)
                self.assertEqual(session['profile']['company_id'],'U_1')
                self.assertEqual(session['field_labels']['CITY']['label'],'Город регистрации')
                self.assertTrue(Path(session['trace_path']).is_file())
            finally:
                server.shutdown();server.server_close();controller.close();thread.join(timeout=2)

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
