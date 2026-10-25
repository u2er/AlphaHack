import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from gui import auth
from gui.server import create_server

KEY = 'a'*40


class Fake:
    """Nothing behind the UI: these tests stop at the gate and the settings."""


class Server:
    def __init__(self, access_key=KEY):
        self.server = create_server(port=0, controller=Fake(), insights=Fake(), access_key=access_key)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.cookie = ''
    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start(); return self
    def __exit__(self, *exc):
        self.server.shutdown(); self.server.server_close()
    def call(self, path, body=None):
        headers = {'Cookie': self.cookie} if self.cookie else {}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'; data = json.dumps(body).encode()
        request = Request(self.base+path, data=data, headers=headers, method='POST' if body is not None else 'GET')
        with urlopen(request) as response:
            if cookie := response.headers.get('Set-Cookie'):
                self.cookie = cookie.split(';')[0]
            return response.status, json.load(response) if 'json' in response.headers['Content-Type'] else response.read()


class AuthTests(unittest.TestCase):
    def test_everything_but_login_needs_the_key(self):
        with Server() as s:
            with self.assertRaises(HTTPError) as caught:
                s.call('/api/settings/llm')
            self.assertEqual(caught.exception.code, 401)
            status, page = s.call('/')  # urlopen follows the redirect to the login page
            self.assertIn(b'loginForm', page)
            with self.assertRaises(HTTPError) as caught:
                s.call('/api/login', {'key': 'b'*40})
            self.assertEqual(caught.exception.code, 401)
            self.assertEqual(s.call('/api/login', {'key': KEY})[0], 200)
            self.assertIn(b'settingsOpen', s.call('/')[1])
            s.call('/api/logout', {})
            with self.assertRaises(HTTPError):
                s.call('/api/settings/llm')

    def test_generated_key_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'GUI_ACCESS_KEY': '', 'GUI_ACCESS_KEY_FILE': f'{tmp}/key'}):
            key = auth.resolve_key()
            self.assertEqual(len(key), 40)
            self.assertTrue(key.isalnum())
            self.assertEqual(auth.resolve_key(), key)
            self.assertEqual(Path(f'{tmp}/key').stat().st_mode & 0o777, 0o600)


class SettingsTests(unittest.TestCase):
    def test_values_come_from_env_and_the_key_stays_on_the_server(self):
        env = {'LLM_BASE_URL': 'https://api.deepseek.com', 'LLM_MODEL': 'm-env', 'LLM_API_KEY': 'sk-env-123456',
               'LLM_PARAMS': '', 'LLM_VISION': '', 'LLM_MAX_TOKENS': '', 'LLM_TIMEOUT': ''}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**env, 'LLM_SETTINGS_FILE': f'{tmp}/llm.json'}), Server('') as s:
            view = s.call('/api/settings/llm')[1]
            self.assertEqual(view['values']['model'], 'm-env')
            self.assertEqual(view['values']['params'], '')
            self.assertEqual(view['api_key'], {'set': True, 'hint': '…3456', 'source': 'env'})
            self.assertNotIn('sk-env-123456', json.dumps(view))

            view = s.call('/api/settings/llm', {'values': {**view['values'], 'model': 'm-gui'}, 'api_key': 'sk-gui-abcdef'})[1]
            self.assertEqual(view['overridden'], ['model'])
            self.assertEqual(view['api_key']['source'], 'gui')
            from utils.config import settings
            self.assertEqual((settings().model, settings().api_key.get_secret_value()), ('m-gui', 'sk-gui-abcdef'))
            # Saving the form again without a key keeps the typed one.
            view = s.call('/api/settings/llm', {'values': view['values']})[1]
            self.assertEqual(view['api_key']['source'], 'gui')
            with self.assertRaises(HTTPError) as caught:
                s.call('/api/settings/llm', {'values': {'params': '[1]'}})
            self.assertEqual(caught.exception.code, 400)
            view = s.call('/api/settings/llm/reset', {})[1]
            self.assertEqual((view['values']['model'], view['api_key']['source'], view['overridden']), ('m-env', 'env', []))


if __name__ == '__main__':
    unittest.main()
