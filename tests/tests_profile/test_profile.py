import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from profile.db.prepare import import_csv
from profile.db.field_labels import FIELDS, VALUE_LABELS
from profile.db.store import Store
from profile.agent import ProfileAgent
from profile.draft import prepare_overview, suggest_edit, apply_edit
from profile.trace import ProfileTrace, TracedClient, TracedStore
from profile.models import Assessment, Topic
from profile.service import *
from utils.llm import Completion, LLMError

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
    def company(self, key):
        if key != 'U_1': raise ValueError('Not found')
        return {'SYNTHETIC_U':key,'MAINOKVED_NAME':'Металлургия','CITY':'Москва','CL_PRODUCT_LOA_MARK':'1.0'}
    def signals(self,key):
        return {'payments':[{'payer':key,'beneficiary':'U_2','amount':100,'count':1}]}
    def search(self,query,limit=4):
        return [{'id':'source:1','text':'Исторический обзор ставки','filename':'test.pdf','page':1}]

class FakeLLM:
    def __init__(self,actions):self.actions=iter(actions);self.calls=[]
    def complete_structured(self,*args):
        self.calls.append(args)
        return Completion(json.dumps(next(self.actions)),{})

def action(tool,query='',question='',assessment=None):
    return dict(tool=tool,query=query,question=question,assessment=assessment)

def ready():
    p=create_profile(FakeStore(),'U_1')
    for key in QUESTIONS: answer(p,key,'не знаю' if key != 'sensitivity' else 'Стоимость кредита')
    confirm_profile(p)
    return p

class ProfileTests(unittest.TestCase):
    def test_prefill_no_invented_roles(self):
        p=create_profile(FakeStore(),'U_1')
        self.assertFalse(p.facts['CITY'].confirmed)
        self.assertEqual(p.source_facts['CITY'].value,'Москва')
        self.assertNotIn('supplier',str(p.signals))
        self.assertEqual(len(missing_questions(p)),6)
    def test_manual_and_optional_questions(self):
        p=create_profile(FakeStore(),manual={'name':'Тест'})
        with self.assertRaises(ValueError): answer(p,'activity',' ')
        answer(p,'activity','Торговля')
        clear_answer(p,'activity')
        confirm_profile(p)
        self.assertTrue(p.profile_confirmed)
        self.assertEqual(p.answers,{})
    def test_empty_manual_profile_has_safe_overview(self):
        p=create_profile(FakeStore())
        overview=prepare_overview(p,None)
        self.assertEqual(overview.claims,[])
        confirm_profile(p)
    def test_revision_invalidates_assessment(self):
        p=ready();p.assessment=Assessment(topics=[],limitations=[]);confirm_assessment(p)
        set_fact(p,'CITY','Томск')
        self.assertIsNone(p.assessment)
        self.assertFalse(p.profile_confirmed)
        self.assertEqual(p.trace[-1]['previous']['value'],'Москва')
        self.assertEqual(p.source_facts['CITY'].value,'Москва')
        with self.assertRaises(ValueError):export_profile(p)
    def test_overview_cites_existing_raw_fields_and_signals(self):
        p=create_profile(FakeStore(),'U_1')
        overview={'summary':'По данным набора указан металлургический ОКВЭД.',
                  'claims':[{'kind':'fact','text':'Отрасль указана в наборе.',
                             'source_keys':['MAINOKVED_NAME']},
                            {'kind':'signal','text':'Есть наблюдаемый платёж.',
                             'source_keys':['signals.payments']},
                            {'kind':'question','text':'Где продаётся продукция?',
                             'source_keys':['MAINOKVED_NAME']}]}
        prepare_overview(p,FakeLLM([overview]))
        self.assertEqual(p.overview.claims[1].kind,'signal')
        self.assertIn('source_facts',p.model_dump())
        set_fact(p,'CITY','Томск')
        self.assertIsNone(p.overview)
        self.assertEqual(p.source_facts['CITY'].value,'Москва')
    def test_overview_rejects_invented_source(self):
        p=create_profile(FakeStore(),'U_1')
        bad={'summary':'x','claims':[{'kind':'fact','text':'x','source_keys':['invented']}]*3}
        with self.assertRaises(LLMError):prepare_overview(p,FakeLLM([bad]*3))
    def test_natural_edit_maps_to_key_then_requires_application(self):
        p=create_profile(FakeStore(),'U_1')
        proposal={'scope':'facts','key':'CITY','value':'Томск','explanation':'Исправлен город регистрации'}
        suggestion=suggest_edit(p,FakeLLM([proposal]),'город регистрации теперь Томск')
        self.assertEqual(p.facts['CITY'].value,'Москва')
        apply_edit(p,suggestion)
        self.assertEqual(p.facts['CITY'].value,'Томск')
        self.assertEqual(p.source_facts['CITY'].value,'Москва')
    def test_natural_edit_clarifies_ambiguous_change(self):
        p=create_profile(FakeStore(),'U_1')
        suggestion=suggest_edit(p,FakeLLM([{'scope':'clarify','key':'','value':'',
                                             'explanation':'Вы хотите изменить регистрацию или рынок продаж?'}]),
                                'город теперь Томск')
        self.assertEqual(suggestion.scope,'clarify')
        with self.assertRaises(ValueError):apply_edit(p,suggestion)
    def test_cli_natural_edit_requires_user_confirmation(self):
        import contextlib
        import io
        from unittest.mock import patch
        from profile.__main__ import edit
        p=create_profile(FakeStore(),'U_1')
        proposal={'scope':'facts','key':'CITY','value':'Томск','explanation':'Исправлен город регистрации'}
        with patch('builtins.input',side_effect=['Город регистрации теперь Томск','нет','']), contextlib.redirect_stdout(io.StringIO()):
            edit(p,FakeLLM([proposal]))
        self.assertEqual(p.facts['CITY'].value,'Москва')
        with patch('builtins.input',side_effect=['Город регистрации теперь Томск','да','']), contextlib.redirect_stdout(io.StringIO()):
            edit(p,FakeLLM([proposal]))
        self.assertEqual(p.facts['CITY'].value,'Томск')
    def test_unknown_answers_are_saved_not_inferred(self):
        p=ready();self.assertEqual(p.answers['geography'].value,'не знаю')
    def test_undated_source_values_are_not_treated_as_conflicts(self):
        p=ready()
        p.facts['REVENUE_COMPANY']=Fact(value='59603929469.0',source='csv')
        p.facts['FTS_COUNTRY_NAME']=Fact(value='ИРАК, ЛИВАН, ГРУЗИЯ',source='csv')
        answer(p,'scale','100000000 рублей в год - выручка')
        answer(p,'geography','Россия, Китай')
        issues=profile_issues(p)
        self.assertEqual(issues,[])
    def test_export_confirmation_and_persistence(self):
        p=ready()
        with self.assertRaises(ValueError):export_profile(p)
        p.assessment=Assessment(topics=[],limitations=['Нет подходящих материалов'])
        confirm_assessment(p);set_sites(p,['example.com','https://example.com/'])
        self.assertEqual(export_profile(p)['source_preferences']['sites'],['https://example.com'])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'p.json';save(p,path)
            self.assertEqual(load(path),p)
    def test_invalid_urls(self):
        for value in ['file:///tmp/x','http://user:password@example.com','http://localhost','http://127.0.0.1','http://example.com:bad','bad host.ru']:
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_site(value)
    def test_agent_tools_and_grounding(self):
        p=ready()
        assessment={'topics':[{'title':'Ставка','rationale':'Связь с указанной стоимостью кредита','profile_fields':['sensitivity'],'source_ids':['source:1']}], 'limitations':['Исторический материал']}
        client=FakeLLM([action('search_library',query='ставка'),action('ask_user',question='Плавающая ставка?'),action('finish',assessment=assessment)])
        ProfileAgent(FakeStore(),client).run(p,lambda q:'Не знаю')
        metadata=client.calls[0][1]['field_metadata']
        self.assertEqual(set(metadata),{key for key,*_ in FIELDS})
        self.assertIn('годовая выручка',metadata['REVENUE_COMPANY']['description'])
        self.assertIn('Клиент банка',metadata['CL_TYPE']['coded_values']['CLIENT'])
        self.assertFalse(p.assessment_confirmed)
        self.assertIn('source:1',p.evidence)
        self.assertEqual(p.answers['agent_question_1'].value['answer'],'Не знаю')
    def test_agent_rejects_fabricated_citations(self):
        p=ready();bad={'topics':[{'title':'x','rationale':'x','profile_fields':['sensitivity'],'source_ids':['invented']}],'limitations':[]}
        client=FakeLLM([action('search_library',query='x')]+[action('finish',assessment=bad)]*3)
        with self.assertRaises(LLMError):ProfileAgent(FakeStore(),client).run(p,lambda q:'x')
        self.assertIsNone(p.assessment)
    def test_agent_cannot_skip_search(self):
        client=FakeLLM([action('finish',assessment={'topics':[],'limitations':[]})]*3)
        with self.assertRaises(LLMError):ProfileAgent(FakeStore(),client).run(ready(),lambda q:'x')
    def test_conflict_requires_resolution(self):
        p=ready();p.signals['data_conflicts']={'CITY':['Москва','Омск']}
        p.profile_confirmed=False
        self.assertIn('Город регистрации',profile_issues(p)[0])
        with self.assertRaises(ValueError):confirm_profile(p)
        set_fact(p,'CITY','не знаю');confirm_profile(p)
        self.assertEqual(profile_issues(p),[])

class RetrievalTests(unittest.TestCase):
    def test_postgres_vector_search_uses_pgvector_and_keeps_provenance(self):
        from unittest.mock import patch
        class Embedder:
            def encode(self,texts,query=False):
                assert query
                return np.zeros((1,1024),dtype='<f4')
        class Connection:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,sql,params):
                self.sql,self.params=sql,params
                return self
            def fetchall(self):
                return [{'id':'chunk:1','text':'Обзор','page':2,'filename':'report.pdf',
                         'sha256':'hash','date_hint':'2021','extraction':'ocr','score':0.75}]
        connection=Connection()
        with patch('profile.db.store.connect',return_value=connection):
            store=Store('postgresql://example/test',Embedder())
            result=store.search('ставка',1)[0]
        self.assertIn('<=>',connection.sql)
        self.assertEqual(connection.params[-1],1)
        self.assertEqual(result['id'],'chunk:1')
        self.assertIn('текущую',result['date_warning'])

class TraceTests(unittest.TestCase):
    def test_trace_records_model_payload_validation_and_database_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace=ProfileTrace(Path(tmp)/'run.jsonl')
            store=TracedStore(FakeStore(),trace)
            p=create_profile(store,'U_1')
            overview={'summary':'Компания из набора.', 'claims':[
                {'kind':'fact','text':'Указана отрасль.','source_keys':['MAINOKVED_NAME']},
                {'kind':'signal','text':'Есть наблюдаемый платёж.','source_keys':['signals.payments']},
                {'kind':'question','text':'Уточните рынки.','source_keys':['MAINOKVED_NAME']}]}
            class Client(FakeLLM):
                provider='test_provider'
                model='test_model'
                api_key='DO_NOT_LOG_THIS_KEY'
            prepare_overview(p,TracedClient(Client([overview]),trace))
            events=[json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual(trace.path.stat().st_mode & 0o777,0o600)
            self.assertTrue(any(e['event']=='db_result' and e['method']=='signals'
                                and e['result']['payments'] for e in events))
            self.assertTrue(any(e['event']=='llm_request' and 'facts' in e['payload'] for e in events))
            self.assertTrue(any(e['event']=='llm_response' and
                                json.loads(e['data'])['summary']=='Компания из набора.' for e in events
                                if e['event']=='llm_response'))
            self.assertTrue(any(e['event']=='llm_validation' and e['valid'] for e in events))
            self.assertNotIn('DO_NOT_LOG_THIS_KEY',trace.path.read_text())


class PreparationTests(unittest.TestCase):
    def test_alternative_parser_contract_is_validated(self):
        from parser.loader import load_parser
        from profile.db.prepare import validate_document
        self.assertTrue(callable(load_parser('parser.csv:csv_rows')))
        with self.assertRaises(ValueError):load_parser('os:system')
        document={'date_hint':'unknown','pages':[{'page':1,'text':'Документ','extraction':'custom'}]}
        self.assertEqual(validate_document(document,Path('sample.pdf'))[1],'unknown')
        with self.assertRaises(ValueError):
            validate_document({'date_hint':'unknown','pages':[{'page':2,'text':'Документ','extraction':'custom'}]},Path('sample.pdf'))
        with self.assertRaises(ValueError):
            validate_document({'pages':document['pages']},Path('sample.pdf'))

    def test_duplicate_import_preserves_conflicting_records(self):
        import csv
        class Connection:
            def __init__(self):self.calls=[]
            def cursor(self):return self
            def copy(self,sql):self.copy_sql=sql;return self
            def write_row(self,row):self.calls.append((self.copy_sql,row))
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,sql,params):self.calls.append((sql,params))
            def executemany(self,sql,params):self.calls.extend((sql,p) for p in params)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            columns=['SYNTHETIC_U','MAINOKVED_NAME','REGION']
            with (folder/'df_Uid_data_hack.csv').open('w') as f:
                writer=csv.writer(f,delimiter=';');writer.writerow(columns)
                writer.writerows([['U_1','сталь','Москва'],['U_1','торговля','Москва']])
            (folder/'df_Fid_Uid_link_hack.csv').write_text('SYNTHETIC_F;SYNTHETIC_U;CITY_NAME;RC_NAME\n')
            (folder/'df_Uid_payment_hack.csv').write_text('PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\n')
            c=Connection();import_csv(c,folder)
            records=[params for sql,params in c.calls if 'company_records' in sql]
            self.assertEqual(len(records),2)
            company=[params[-1].obj for sql,params in c.calls if 'profile_data.companies' in sql][0]
            self.assertNotIn('MAINOKVED_NAME',company)
            self.assertEqual(company['_conflicts']['MAINOKVED_NAME'],['сталь','торговля'])

class CLITests(unittest.TestCase):
    def test_resume_through_export(self):
        import contextlib
        import io
        from unittest.mock import patch
        from profile.__main__ import wizard
        p=ready()
        assessment={'topics':[{'title':'Ставка','rationale':'Стоимость кредита','profile_fields':['sensitivity'],'source_ids':['source:1']}],'limitations':['Исторический материал']}
        class CLIStore(FakeStore):
            def search(self,query,limit=4):
                return [super().search(query)[0] | {'date_hint':'2021','date_warning':'Исторический материал'}]
        overview={'summary':'Металлургическая компания.','claims':[
            {'kind':'fact','text':'Отрасль указана в наборе.','source_keys':['MAINOKVED_NAME']},
            {'kind':'signal','text':'Есть наблюдаемый платёж.','source_keys':['signals.payments']},
            {'kind':'question','text':'Где продаётся продукция?','source_keys':['geography']}]}
        client=FakeLLM([overview,action('search_library',query='ставка'),action('finish',assessment=assessment)])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);draft=root/'draft.json';out=root/'out.json';save(p,draft)
            with patch('profile.__main__.ROOT',root), patch('profile.__main__.make_client',return_value=client), patch('profile.__main__.settings',return_value=None), patch('builtins.input',side_effect=['','да','']), contextlib.redirect_stdout(io.StringIO()):
                wizard(CLIStore(),resume=draft,output=out)
            result=json.loads(out.read_text())
            self.assertEqual(result['monitoring_context']['topics'][0]['title'],'Ставка')
            self.assertEqual(result['source_preferences']['sites'],[])

if __name__=='__main__':unittest.main()
