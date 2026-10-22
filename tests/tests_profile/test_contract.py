import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from profile.contract import ProfileExport, render_for_prompt, display_name
from profile.models import Assessment, Topic
from profile.service import *
from tests.tests_profile.test_profile import FakeStore

FIXTURE = Path(__file__).resolve().parents[1]/'fixtures'/'profiles'/'vectorfood.json'

def exported(**answers):
    p=create_profile(FakeStore(),'U_1')
    for key in QUESTIONS: answer(p,key,answers.get(key,'не знаю'))
    confirm_profile(p)
    p.evidence={'source:1':{'id':'source:1','text':'Ставка','filename':'rate.pdf','page':1,'date_hint':'2026-09-11'}}
    p.assessment=Assessment(topics=[Topic(title='Ставка',rationale='Кредитная нагрузка',
                                          profile_fields=['sensitivity'],source_ids=['source:1'])],limitations=['Исторический материал'])
    confirm_assessment(p)
    return export_profile(p)

class ContractTests(unittest.TestCase):
    def test_export_is_the_contract(self):
        payload=exported(sensitivity='Плавающая ставка')
        export=ProfileExport.model_validate(payload)
        self.assertEqual(export.schema_version,'1')
        self.assertEqual(export.company_facts['CITY'].value,'Москва')
        self.assertEqual(json.loads(json.dumps(payload,ensure_ascii=False)),payload)

    def test_render_puts_user_answers_first_and_keeps_caveats(self):
        text=render_for_prompt(exported(sensitivity='Плавающая ставка'))
        self.assertLess(text.index('Плавающая ставка'),text.index('Поля карточки'))
        self.assertIn('пользователь не знает',text)
        self.assertIn('В наборе отмечены кредитный продукт',text)  # coded value 1.0, not a raw number
        self.assertIn('не называй их поставщиками',text)
        self.assertNotIn('Темы наблюдения',text)  # topics are no longer part of the profile
        self.assertIn('ID обезличены',text)
        self.assertNotIn('U_2',text)  # counterparties are counted, not named

    def test_wrong_version_or_unconfirmed_topics_are_rejected(self):
        payload=exported()
        with self.assertRaises(ValidationError):
            ProfileExport.model_validate(payload|{'schema_version':'2'})
        with self.assertRaises(ValidationError):
            ProfileExport.model_validate({k:v for k,v in payload.items() if k!='monitoring_context'})

    def test_demo_fixture_follows_the_contract(self):
        export=ProfileExport.model_validate_json(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(display_name(export),'ВекторФуд')
        text=render_for_prompt(export)
        self.assertIn('Финансовый директор',text)
        self.assertNotIn('Темы наблюдения',text)

    def test_publish_writes_to_the_shared_folder_only_when_configured(self):
        payload=exported()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'PROFILES_DIR':''}):
            self.assertIsNone(publish(payload))
            path=publish(payload,tmp)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['profile_id'],payload['profile_id'])

class InfrastructureTests(unittest.TestCase):
    def test_database_url_falls_back_to_the_eventgraph_database(self):
        from profile.db.connection import database_url
        with patch.dict(os.environ,{'PROFILE_DATABASE_URL':'','DATABASE_URL':'postgresql+asyncpg://u:p@db:5432/eventgraph'}):
            self.assertEqual(database_url(),'postgresql://u:p@db:5432/eventgraph')
        with patch.dict(os.environ,{'PROFILE_DATABASE_URL':'postgresql://x/y'}):
            self.assertEqual(database_url(),'postgresql://x/y')

    def test_llm_settings_read_the_shared_variables_first(self):
        from utils.config import settings
        env={'LLM_API_KEY':'k','LLM_MODEL':'m','LLM_TIMEOUT':'42','LLM_TIMEOUT_SECONDS':'7',
             'LLM_PARAMS':'{"reasoning_effort":"none"}','LLM_EXTRA_BODY':'{"x":1}'}
        with patch.dict(os.environ,env):
            config=settings()
        self.assertEqual((config.model,config.timeout_seconds,config.extra_body),('m',42.0,{'reasoning_effort':'none'}))
        with patch.dict(os.environ,{'LLM_API_KEY':'k','LLM_TIMEOUT':'','LLM_TIMEOUT_SECONDS':'7','LLM_PARAMS':''}):
            self.assertEqual(settings().timeout_seconds,7.0)

    def test_shared_embedder_splits_text_to_the_model_input(self):
        from profile.db.store import SharedEmbedder
        chunks=SharedEmbedder(chunk_chars=100).split(' '.join(f'слово{i}' for i in range(200)))
        self.assertTrue(all(len(c)<=100 for c in chunks))
        self.assertGreater(len(chunks),10)
        self.assertEqual(chunks[0].split()[0],'слово0')
        self.assertIn('слово199',chunks[-1])
        self.assertEqual(SharedEmbedder(chunk_chars=100).split('   '),[])

    def test_index_built_with_another_model_is_refused(self):
        from profile.db.store import Store
        class Embedder:
            name='local:other-model'
            def encode(self,texts,query=False): return [[0.0]]
        class Connection:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def execute(self,sql,params): return self
            def fetchone(self): return {'value':'local:intfloat/multilingual-e5-large'}
        with patch('profile.db.store.connect',return_value=Connection()):
            with self.assertRaisesRegex(ValueError,'--replace'):
                Store('postgresql://example/test',Embedder()).search('ставка')

class PdfParserTests(unittest.TestCase):
    def test_dates_come_from_the_first_page_then_the_file_name(self):
        from parser.pdf import date_hint
        self.assertEqual(date_hint('x.pdf','Москва, 11 сентября 2026 г.'),'2026-09-11')
        self.assertEqual(date_hint('x.pdf','Report as of September 11, 2026'),'2026-09-11')
        self.assertEqual(date_hint('FI_Strategy_3Q26_22072026.pdf',''),'2026-07-22')
        self.assertEqual(date_hint('Steels_15Apr2021.pdf',''),'2021-04-15')
        self.assertEqual(date_hint('Gold_digest_August26.pdf',''),'2026-08')
        self.assertEqual(date_hint('ALFA-macro_stat1H26.pdf',''),'2026-06')
        self.assertEqual(date_hint('Inflation_tracker_02.pdf',''),'unknown')
        # OCR lost the day; the disclaimer's licence date is not the report's date
        page='Г сентября 2026\nМакростатистика\n...Генеральная лицензия ЦБ РФ № 1326 от 16.01.2015 г.'
        self.assertEqual(date_hint('ALFA-7M26.pdf',page),'2026-09')
        self.assertEqual(date_hint('x.pdf','Лицензия № 1 от 16.01.2015'),'unknown')

    def test_text_pages_are_kept_and_scans_go_to_ocr(self):
        import parser.pdf as pdf
        class Page:
            def __init__(self,text):self.text=text
            def get_textpage(self):
                page=self
                class TextPage:
                    def get_text_bounded(self):return page.text
                    def close(self):pass
                return TextPage()
            def render(self,scale):
                class Bitmap:
                    def to_pil(self):return 'image'
                return Bitmap()
            def close(self):pass
        class Document(list):
            def close(self):pass
        document=Document([Page('Обзор от 05.03.2025'),Page('')])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'doc.pdf';path.write_bytes(b'%PDF')
            with patch.dict(os.environ,{'PARSER_CACHE_DIR':tmp,'PDF_PARSER':'auto'}), \
                 patch('pypdfium2.PdfDocument',return_value=document), \
                 patch.object(pdf,'docling_available',return_value=False), \
                 patch.object(pdf,'ocr_image',return_value='Текст со скана') as ocr:
                result=pdf.parse_document(path)
                again=pdf.parse_document(path)  # cached: no second OCR
        self.assertEqual(result['date_hint'],'2025-03-05')
        self.assertEqual([(p['extraction'],p['text']) for p in result['pages']],
                         [('pdfium-text','Обзор от 05.03.2025'),('tesseract-ocr','Текст со скана')])
        self.assertEqual(ocr.call_count,1)
        self.assertEqual(again,result)

    def test_docling_is_used_when_installed_and_shares_the_source_cache(self):
        import sys, types
        import parser.pdf as pdf
        # Not docling_parser.document: importing the package here would leak into
        # test_pdf_kinds, which checks that nothing loaded it yet.
        ParsedDocument=lambda text,metadata:types.SimpleNamespace(page_content=text,metadata=metadata)
        seen={}
        class Manager:
            def __init__(self,file_path,cache_dir):
                seen['cache']=cache_dir
                self.texts=[ParsedDocument('Стр. 2',{'page':2}),ParsedDocument('11 сентября 2026 г. Ставка',{'page':1})]
            def extract_content(self):seen['parsed']=True
        module=types.ModuleType('docling_parser.file_manager');module.PdfFileManager=Manager
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'doc.pdf';path.write_bytes(b'%PDF')
            with patch.dict(os.environ,{'DATA_DIR':tmp,'PDF_PARSER':'auto','PARSER_CACHE_DIR':tmp}), \
                 patch.dict(sys.modules,{'docling_parser.file_manager':module}), \
                 patch.object(pdf,'docling_available',return_value=True):
                result=pdf.parse_document(path)
        self.assertEqual(seen['cache'],str(Path(tmp)/'pdf_cache'))
        self.assertEqual([(p['page'],p['text'],p['extraction']) for p in result['pages']],
                         [(1,'11 сентября 2026 г. Ставка','docling'),(2,'Стр. 2','docling')])
        self.assertEqual(result['date_hint'],'2026-09-11')

    def test_indexer_accepts_empty_scan_pages_but_not_empty_documents(self):
        from profile.db.prepare import validate_document
        pages=[{'page':1,'text':'','extraction':'tesseract-ocr'},{'page':2,'text':'Текст','extraction':'tesseract-ocr'}]
        self.assertEqual(len(validate_document({'date_hint':'unknown','pages':pages},Path('a.pdf'))[0]),2)
        with self.assertRaises(ValueError):
            validate_document({'date_hint':'unknown','pages':pages[:1]},Path('a.pdf'))

if __name__=='__main__':unittest.main()
