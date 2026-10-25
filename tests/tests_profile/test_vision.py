"""Pictures of attached files reach a vision model as images, not only as OCR text."""
import io
import json
from pathlib import Path

import httpx
from PIL import Image
from pydantic import SecretStr

from parser import documents
from profile.onboarding import Onboarding
from tests.tests_profile.test_onboarding import FORM, LLM, Store, act, draft, wait
from utils.config import Settings
from utils.llm import OpenAICompatibleClient


def png(size=(200, 120), color='red'):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


def test_pdf_image_objects_are_found_per_page_and_tiny_ones_skipped(tmp_path):
    path = tmp_path / 'scan.pdf'
    pages = [Image.new('RGB', (300, 200), 'blue'), Image.new('RGB', (40, 40), 'green')]
    pages[0].save(path, save_all=True, append_images=pages[1:])
    found = documents._pdf_images(path)
    assert list(found) == [1]                                  # 40 px — значок, не картинка
    assert Image.open(io.BytesIO(found[1][0])).size == (300, 200)


def test_pdf_pages_get_marks_and_their_pictures(tmp_path, monkeypatch):
    path = tmp_path / 'report.pdf'
    path.write_bytes(b'%PDF')
    import parser.pdf
    monkeypatch.setattr(parser.pdf, 'parse_document', lambda p: {'pages': [{'text': 'Выручка'}, {'text': 'Схема'}]})
    monkeypatch.setattr(documents, '_pdf_images', lambda p: {2: [png(), png(color='blue')]})
    result = documents.parse_upload(path, images=True)
    assert [p['images'] for p in result['pages']][0] == []
    assert result['pages'][1]['text'] == 'Схема\n\n[изображение 1]\n\n[изображение 2]'
    assert len(result['pages'][1]['images']) == 2
    # без зрения — как раньше: ни меток, ни картинок
    plain = documents.parse_upload(path)
    assert plain['pages'][1] | {'extraction': ''} == {'page': 2, 'text': 'Схема', 'extraction': '', 'images': []}


def test_docx_pictures_follow_their_placeholders_across_split_pages(monkeypatch):
    numbering = documents._Numbering()
    text = f"Шаг 1 {numbering.add(png())}\n\n{'x' * 4500}\n\nШаг 2 {numbering.add(png(color='blue'))}"
    pages = documents._attach(documents.split_pages(text), numbering)
    assert [len(p['images']) for p in pages] == [1, 1]
    assert '[изображение 2]' in pages[1]['text']


def test_an_image_only_file_is_accepted_with_vision(tmp_path, monkeypatch):
    path = tmp_path / 'screen.png'
    path.write_bytes(png())
    monkeypatch.setattr(documents, '_docling_pages', lambda p, images: [{'text': '', 'images': []}])
    result = documents.parse_upload(path, images=True)
    assert result['pages'][0]['text'] == '[изображение 1]' and len(result['pages'][0]['images']) == 1


def finish():
    action = act('finish', draft=draft())
    for page in action['draft']['pages']:
        page['sources'] = ['form.activity', 'file:f1:1']   # вопросов не было — answer.1 не существует
    return action


class VisionLLM(LLM):
    def __init__(self, actions):
        super().__init__(actions)
        self.images = []

    def complete_structured(self, system, payload, model, images=()):
        self.images.append(list(images))
        return super().complete_structured(system, payload, model)


def test_read_file_shows_the_page_pictures_to_the_next_call_only(tmp_path):
    calls = []
    def parse(path, images=False):
        calls.append(images)
        return {'kind': 'Word', 'pages': [{'page': 1, 'text': 'Откройте раздел API [изображение 1]', 'images': [png()]},
                                          {'page': 2, 'text': 'Сохраните ключ', 'images': []}]}
    llm = VisionLLM([act('read_file', file_id='f1', page=1), act('read_file', file_id='f1', page=2),
                     finish()])
    session = Onboarding(Store(), lambda: llm, Path(tmp_path), parse=parse, vision=True)
    session.add_file('Инструкция.docx', b'doc')
    assert wait(lambda: session.files['f1'].status == 'ready')
    assert calls == [True] and session.files['f1'].images == 1
    session.submit_form(FORM)
    assert wait(lambda: session.status in ('review', 'error')), session.error
    assert session.status == 'review', session.error
    assert llm.calls[0]['files'][0]['image_pages'] == [1]
    assert [len(i) for i in llm.images] == [0, 1, 0]           # только шаг сразу после read_file страницы 1
    assert llm.images[1][0].startswith('data:image/jpeg;base64,')
    assert 'изобр.' in json.dumps(llm.calls[1]['history'], ensure_ascii=False)
    assert 'изображений: 1' in json.dumps(session.view()['messages'], ensure_ascii=False)


def test_without_vision_files_are_parsed_as_before(tmp_path):
    calls = []
    def parse(path):
        calls.append(path)
        return {'kind': 'Word', 'pages': [{'page': 1, 'text': 'Текст', 'images': [png()]}]}
    llm = VisionLLM([finish()])
    session = Onboarding(Store(), lambda: llm, Path(tmp_path), parse=parse)
    session.add_file('Инструкция.docx', b'doc')
    assert wait(lambda: session.files['f1'].status == 'ready')
    assert session.files['f1'].images == 0
    session.submit_form(FORM)
    assert wait(lambda: session.status in ('review', 'error'))
    assert 'image_pages' not in llm.calls[0]['files'][0] and llm.images == [[]]


def test_client_sends_images_in_the_user_message():
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
    for mode in ('json_schema', 'responses'):
        config = Settings(api_key=SecretStr('k'), base_url='https://llm.example', structured_output=mode)
        client = OpenAICompatibleClient(config, transport=httpx.MockTransport(handler))
        try:
            client.complete_structured('sys', {'a': 1}, Settings, images=['data:image/png;base64,AA=='])
        except Exception:  # noqa: BLE001 — the envelope is not the point here
            pass
    chat, responses = bodies
    assert chat['messages'][1]['content'][1] == {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}
    assert responses['input'][0]['content'][1] == {'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}
