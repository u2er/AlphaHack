"""Loopback-only HTTP server; no new runtime dependencies."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .controller import Controller
from .insights import Insights, InsightsError

STATIC = Path(__file__).resolve().parent
MIME = {'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8',
        'bank.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}

class Handler(BaseHTTPRequestHandler):
    server_version = 'AlfaProfileGUI/1'

    def _headers(self, status, content_type, length, *, attachment=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header('Content-Disposition',f'attachment; filename="{attachment}"')
        self.end_headers()

    def json(self, status, value, *, attachment=None):
        data = json.dumps(value,ensure_ascii=False,default=str).encode('utf-8')
        self._headers(status,'application/json; charset=utf-8',len(data),attachment=attachment)
        self.wfile.write(data)

    def error_json(self, status, message):
        self.json(status,{'error':message})

    def _check_origin(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host','')
        if origin and urlsplit(origin).netloc != host:
            raise PermissionError('Запрос из другого источника запрещён')

    def _body(self):
        self._check_origin()
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            raise ValueError('Ожидается application/json')
        size = int(self.headers.get('Content-Length','0'))
        if not 0 <= size <= 65536:
            raise ValueError('Слишком большой запрос')
        data = json.loads(self.rfile.read(size))
        if not isinstance(data,dict):
            raise ValueError('Ожидается JSON-объект')
        return data

    def do_GET(self):
        path = urlsplit(self.path)
        try:
            if path.path == '/api/companies':
                query = parse_qs(path.query).get('q',[''])[0][:100]
                return self.json(200,{'companies':self.server.controller.companies(query)})
            parts = path.path.strip('/').split('/')
            if parts[:2] == ['api','insights']:
                return self._insights(parts[2:],parse_qs(path.query))
            if len(parts) >= 3 and parts[:2] == ['api','sessions']:
                session = self.server.controller.get(parts[2])
                if len(parts) == 3:
                    return self.json(200,session.view())
                if len(parts) == 4 and parts[3] == 'trace':
                    return self.json(200,{'events':session.trace_events()})
                if len(parts) == 4 and parts[3] == 'download':
                    return self.json(200,session.export()['profile'],attachment='company-profile.json')
            name = 'index.html' if path.path == '/' else path.path.lstrip('/')
            if name in MIME:
                data = (STATIC/name).read_bytes()
                self._headers(200,MIME[name],len(data))
                return self.wfile.write(data)
            self.error_json(404,'Не найдено')
        except KeyError as exc:
            self.error_json(404,str(exc))
        except ValueError as exc:
            self.error_json(400,str(exc))
        except Exception:
            self.error_json(500,'Внутренняя ошибка; проверьте терминал')
            raise

    def _insights(self, parts, query):
        insights = self.server.insights
        try:
            if parts == ['profiles']:
                return self.json(200,{'profiles':insights.profiles()})
            if len(parts) == 2 and parts[0] == 'profiles':
                return self.json(200,insights.profile(parts[1]))
            if len(parts) == 3 and parts[0] == 'profiles' and parts[2] == 'feed':
                importance = min(max(int(query.get('min_importance',['1'])[0]),0),5)
                return self.json(200,{'items':insights.feed(parts[1],importance)})
        except InsightsError as exc:
            return self.error_json(502,str(exc))
        return self.error_json(404,'Не найдено')

    def do_POST(self):
        try:
            body = self._body()
            parts = urlsplit(self.path).path.strip('/').split('/')
            if parts == ['api','sessions']:
                session = self.server.controller.create(body.get('company_id'),body.get('name'),body.get('manual',False))
                return self.json(201,session.view())
            if len(parts) != 4 or parts[:2] != ['api','sessions']:
                return self.error_json(404,'Не найдено')
            session = self.server.controller.get(parts[2])
            action = parts[3]
            if action == 'overview': result = session.overview()
            elif action == 'start-overview': result = session.start_overview()
            elif action == 'submit-manual': result = session.submit_manual(body.get('facts'))
            elif action == 'edit': result = session.edit_key(body.get('scope'),body.get('key'),body.get('value'))
            elif action == 'propose-edit': result = session.propose_edit(body.get('text',''))
            elif action == 'decide-edit': result = session.decide_edit(body.get('accept',False))
            elif action == 'confirm-profile': result = session.confirm_profile()
            elif action == 'start-agent': result = session.start_agent()
            elif action == 'answer-agent': result = session.answer_agent(body.get('value',''))
            elif action == 'remove-topic': result = session.remove_topic(int(body.get('index',-1)))
            elif action == 'confirm-assessment': result = session.confirm_assessment()
            elif action == 'sites': result = session.sites(body.get('sites',[]),body.get('mode','include'))
            elif action == 'export': result = session.export()
            else: return self.error_json(404,'Не найдено')
            self.json(200,result)
        except KeyError as exc:
            self.error_json(404,str(exc))
        except (ValueError,TypeError) as exc:
            self.error_json(400,str(exc))
        except PermissionError as exc:
            self.error_json(403,str(exc))
        except Exception:
            self.error_json(500,'Внутренняя ошибка; проверьте терминал')
            raise

def create_server(host='127.0.0.1',port=8765,controller=None,insights=None):
    server = ThreadingHTTPServer((host,port),Handler)
    server.controller = controller or Controller()
    server.insights = insights or Insights(store=getattr(server.controller,'store',None))
    return server

def main():
    import argparse
    parser=argparse.ArgumentParser(description='Локальный GUI для тестирования профиля')
    parser.add_argument('--port',type=int,default=8765)
    # 0.0.0.0 only inside a container, where compose publishes the port on 127.0.0.1.
    parser.add_argument('--host',default='127.0.0.1')
    args=parser.parse_args()
    server=create_server(host=args.host,port=args.port)
    print(f'Откройте http://127.0.0.1:{args.port}/',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.close()
        server.server_close()

if __name__ == '__main__':main()
