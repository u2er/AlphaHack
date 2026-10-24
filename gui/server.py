"""Loopback-only HTTP server; no new runtime dependencies."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from .controller import Controller
from .insights import Insights, InsightsError

STATIC = Path(__file__).resolve().parent
MIME = {'index.html':'text/html; charset=utf-8','bank.js':'text/javascript; charset=utf-8',
        'onboarding.js':'text/javascript; charset=utf-8','graph.js':'text/javascript; charset=utf-8',
        'style.css':'text/css; charset=utf-8'}
MAX_UPLOAD = 30*1024*1024

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
            if len(parts) == 3 and parts[:2] == ['api','onboarding']:
                return self.json(200,self.server.controller.get(parts[2]).view())
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
            if len(parts) == 2 and parts[0] == 'summaries':
                summary = insights.summary(parts[1], int(query.get('min_importance',['3'])[0]),
                                           query.get('direction',[''])[0])
                return self.json(200,{'summary': summary})
            if len(parts) == 2 and parts[0] == 'chats':
                return self.json(200,{'chats':insights.chats(parts[1])})
            if len(parts) == 3 and parts[0] == 'chats':
                return self.json(200,insights.chat(parts[1],parts[2]))
            if len(parts) == 4 and parts[0] == 'profiles' and parts[2] == 'pages':
                return self.json(200,insights.page(parts[1],parts[3],int(query.get('offset',['0'])[0])))
            if parts == ['connectors']:
                return self.json(200,{'connectors':insights.connectors()})
            if len(parts) == 3 and parts[0] == 'connectors' and parts[2] == 'runs':
                return self.json(200,{'runs':insights.connector_runs(parts[1])})
            if parts == ['graph']:
                return self.json(200,insights.graph())
            if parts == ['graph-search']:
                return self.json(200,{'results':insights.graph_search(query.get('q',[''])[0][:300])})
            if len(parts) == 3 and parts[0] == 'graph':
                return self.json(200,insights.graph_element(parts[1],parts[2]))
        except InsightsError as exc:
            return self.error_json(502,str(exc))
        return self.error_json(404,'Не найдено')

    def _upload(self, session):
        """A file as the raw request body; the name comes URL-encoded in X-Filename."""
        self._check_origin()
        size = int(self.headers.get('Content-Length','0'))
        if not 0 < size <= MAX_UPLOAD:
            raise ValueError('Файл пустой или больше 30 МБ')
        name = unquote(self.headers.get('X-Filename',''))[:200]
        return session.add_file(name,self.rfile.read(size))

    def _insights_post(self, parts, body):
        insights = self.server.insights
        try:
            if parts == ['connectors']:
                interval = int(body.get('interval_seconds') or 3600)
                return self.json(201,insights.create_connector(str(body.get('kind') or ''),str(body.get('title') or '')[:200],
                                                               str(body.get('target') or '')[:500],interval))
            if len(parts) == 3 and parts[0] == 'connectors':
                return self.json(200,insights.connector_action(parts[1],parts[2]))
            if len(parts) == 3 and parts[0] == 'profiles' and parts[2] == 'sources':
                return self.json(200,insights.set_sources(parts[1],body.get('sources') or []))
            if len(parts) == 2 and parts[0] == 'summaries':
                return self.json(200,insights.create_summary(
                    parts[1], int(body.get('min_importance',3)), str(body.get('direction') or '')))
            if len(parts) == 3 and parts[0] == 'chats':
                return self.json(200,insights.send_chat(parts[1],parts[2],str(body.get('message') or '')))
            if parts == ['graph-chat']:
                return self.json(200,insights.graph_chat(str(body.get('message') or ''), body.get('history') or []))
            if len(parts) == 3 and parts[0] == 'profiles' and parts[2] == 'edit-proposal':
                return self.json(200,insights.profile_edit_proposal(parts[1],str(body.get('instruction') or '')))
            if len(parts) == 3 and parts[0] == 'profiles' and parts[2] == 'save':
                return self.json(200,insights.save_profile(parts[1],body.get('profile') or {}))
        except InsightsError as exc:
            return self.error_json(502,str(exc))
        return self.error_json(404,'Не найдено')

    def _publish_profile(self, payload):
        path = self.server.controller.publish(payload)
        insights = self.server.insights
        if insights.api_key:
            insights.publish_profile(payload)
        return path

    def do_POST(self):
        try:
            parts = urlsplit(self.path).path.strip('/').split('/')
            controller = self.server.controller
            if parts[:2] == ['api','insights']:
                return self._insights_post(parts[2:],self._body())
            if parts[:2] != ['api','onboarding']:
                return self.error_json(404,'Не найдено')
            if len(parts) == 4 and parts[3] == 'files':
                return self.json(201,self._upload(controller.get(parts[2])))
            body = self._body()
            if len(parts) == 2:
                return self.json(201,controller.create().view())
            session = controller.get(parts[2])
            action = parts[3] if len(parts) > 3 else ''
            if action == 'form' and len(parts) == 4: result = session.submit_form(body.get('form') or {})
            elif action == 'answer' and len(parts) == 4: result = session.answer(body.get('value'))
            elif action == 'pages' and len(parts) == 5:
                result = session.edit_page(parts[4],body.get('title'),body.get('summary'),body.get('content'))
            elif action == 'confirm' and len(parts) == 4: result = session.confirm(self._publish_profile)
            elif action == 'sources' and len(parts) == 4:
                known = {c['name']:(c.get('description') or c['name']).split(' — ')[0]
                         for c in self.server.insights.connectors() if c.get('enabled')}
                chosen = [str(name) for name in body.get('sources') or []]
                if any(name not in known for name in chosen):
                    raise ValueError('Нет таких источников: ' + ', '.join(n for n in chosen if n not in known))
                result = session.set_sources(chosen,self._publish_profile,known)
            else: return self.error_json(404,'Не найдено')
            self.json(200,result)
        except KeyError as exc:
            self.error_json(404,str(exc).strip("'"))
        except (ValueError,TypeError) as exc:
            self.error_json(400,str(exc).splitlines()[0][:400])
        except PermissionError as exc:
            self.error_json(403,str(exc))
        except InsightsError as exc:
            self.error_json(502,str(exc))
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
    parser=argparse.ArgumentParser(description='Интерфейс «Альфа-Бизнес»: бизнес-аналитика и чат добавления компании')
    parser.add_argument('--port',type=int,default=8765)
    # 0.0.0.0 only inside a container, where compose publishes the port on 127.0.0.1.
    parser.add_argument('--host',default='127.0.0.1')
    args=parser.parse_args()
    from utils.logging_utils import setup_logging
    setup_logging()  # library loading and onboarding errors reach `docker compose logs`
    server=create_server(host=args.host,port=args.port)
    server.controller.prepare_library()
    print(f'Откройте http://127.0.0.1:{args.port}/',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.close()
        server.server_close()

if __name__ == '__main__':main()
