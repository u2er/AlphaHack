from __future__ import annotations
import json
import os
from pathlib import Path
import threading
from uuid import uuid4
from profile.db.store import Store
from profile.agent import ProfileAgent
from profile.draft import prepare_overview, suggest_edit, apply_edit
from profile.models import EditProposal
from profile.service import (QUESTIONS, LABELS, answer, clear_answer, clear_fact, confirm_assessment, confirm_profile,
                             create_profile, export_profile, publish, missing_questions, profile_issues,
                             save, set_fact, set_sites)
from profile.trace import ProfileTrace, TracedClient, TracedStore
from utils.config import settings
from utils.llm import make_client

# In a container the code directory is read-only for the service user: GUI_RUNS_DIR points at a volume.
RUNS = Path(os.getenv('GUI_RUNS_DIR') or Path(__file__).resolve().parent/'runs')

class Session:
    def __init__(self, session_id, profile, store, trace, run_dir, client_factory, field_labels, field_value_labels):
        self.id,self.profile,self.store,self.trace,self.run_dir = session_id,profile,store,trace,run_dir
        self.client_factory = client_factory
        self.field_labels = field_labels
        self.field_value_labels = field_value_labels
        self.client = None
        self.lock = threading.RLock()
        self.status = 'selected'
        self.error = None
        self.pending_question = None
        self.question_event = threading.Event()
        self.question_answer = None
        self.proposal = None
        self.worker = None
        self.overview_started = False
        self.save()

    def get_client(self):
        if self.client is None:
            self.client = TracedClient(self.client_factory(),self.trace)
        return self.client

    def save(self):
        save(self.profile,self.run_dir/'draft.json')

    def view(self):
        with self.lock:
            return {'id':self.id,'profile':self.profile.model_dump(mode='json'),
                    'field_labels':self.field_labels,
                    'field_value_labels':self.field_value_labels,
                    'manual':self.profile.company_id is None,'overview_started':self.overview_started,
                    'questions':QUESTIONS,'missing_questions':missing_questions(self.profile),
                    'issues':profile_issues(self.profile),'status':self.status,'error':self.error,
                    'pending_question':self.pending_question,'proposal':self.proposal.model_dump() if self.proposal else None,
                    'trace_path':str(self.trace.path)}

    def ensure_editable(self):
        if self.status in {'agent_running','awaiting_answer','overview_running'}:
            raise ValueError('Дождитесь завершения агента или ответьте на его вопрос')

    def overview(self):
        with self.lock:
            self.ensure_editable()
            self.overview_started = True
            prepare_overview(self.profile,self._overview_client(),self._field_metadata())
            self.status = 'draft_ready'
            self.trace.record('overview_prepared',overview=self.profile.overview.model_dump())
            self.save()
            return self.view()

    def _overview_client(self):
        return self.get_client() if self.profile.facts or self.profile.answers or self.profile.signals else None

    def _field_metadata(self):
        return {key:{'label':row['label'],'description':row['description'],
                     'coded_values':self.field_value_labels.get(key,{})}
                for key,row in self.field_labels.items()}

    def start_overview(self):
        with self.lock:
            self.ensure_editable()
            self.overview_started = True
            self.status,self.error = 'overview_running',None
            self.worker = threading.Thread(target=self._overview_worker,daemon=True)
            self.worker.start()
            return self.view()

    def _overview_worker(self):
        try:
            with self.lock:
                prepare_overview(self.profile,self._overview_client(),self._field_metadata())
                self.status = 'draft_ready'
                self.trace.record('overview_prepared',overview=self.profile.overview.model_dump())
                self.save()
        except Exception as exc:
            with self.lock:
                self.status = 'error'
                self.error = str(exc) if type(exc).__name__ in {'LLMError','ValueError'} else type(exc).__name__
                self.trace.record('overview_error',error_type=type(exc).__name__,message=self.error)
                self.save()

    def submit_manual(self, facts):
        with self.lock:
            self.ensure_editable()
            if self.profile.company_id is not None or not isinstance(facts,dict):
                raise ValueError('Ожидались поля ручного профиля')
            allowed = set(LABELS) - {'SYNTHETIC_U','GROUP_ID'}
            if not set(facts) <= allowed:
                raise ValueError('Неизвестное поле ручного профиля')
            for key,value in facts.items():
                if str(value).strip(): set_fact(self.profile,key,str(value).strip())
                elif key in self.profile.facts: clear_fact(self.profile,key)
            self.trace.record('manual_profile_submitted',filled_fields=[k for k,v in facts.items() if str(v).strip()])
            self.save()
            return self.start_overview()

    def edit_key(self, scope, key, value):
        with self.lock:
            self.ensure_editable()
            if scope == 'answers':
                if key not in QUESTIONS: raise ValueError('Неизвестный вопрос')
                if str(value).strip(): answer(self.profile,key,str(value))
                elif key in self.profile.answers: clear_answer(self.profile,key)
                else:
                    self.trace.record('question_skipped',key=key)
                    return self.view()
            elif scope == 'facts':
                if key not in LABELS and key not in self.profile.facts: raise ValueError('Неизвестное поле')
                if str(value).strip(): set_fact(self.profile,key,value)
                elif key in self.profile.facts: clear_fact(self.profile,key)
                else: return self.view()
            else:
                raise ValueError('Укажите facts или answers')
            self.status = 'selected'
            self.trace.record('field_edit',scope=scope,key=key,value=value,method='exact_key')
            self.save()
            return self.view()

    def propose_edit(self, text):
        with self.lock:
            self.ensure_editable()
            self.proposal = suggest_edit(self.profile,self.get_client(),text,self._field_metadata())
            self.trace.record('natural_edit_proposed',instruction=text,proposal=self.proposal.model_dump())
            return self.view()

    def decide_edit(self, accept):
        with self.lock:
            self.ensure_editable()
            if self.proposal is None:
                raise ValueError('Нет предложенной правки')
            proposal = self.proposal
            if accept:
                apply_edit(self.profile,proposal)
                self.status = 'selected'
            self.trace.record('natural_edit_decision',accepted=bool(accept),proposal=proposal.model_dump())
            self.proposal = None
            self.save()
            return self.view()

    def confirm_profile(self):
        with self.lock:
            self.ensure_editable()
            confirm_profile(self.profile)
            self.status = 'profile_confirmed'
            self.trace.record('profile_confirmed',issues=profile_issues(self.profile))
            self.save()
            return self.start_agent()

    def start_agent(self):
        with self.lock:
            if not self.profile.profile_confirmed:
                raise ValueError('Подтвердите профиль перед запуском агента')
            if self.worker and self.worker.is_alive():
                raise ValueError('Агент уже работает')
            self.status,self.error,self.pending_question = 'agent_running',None,None
            self.question_event.clear()
            self.worker = threading.Thread(target=self._agent_worker,daemon=True)
            self.worker.start()
            return self.view()

    def _ask_agent(self, question):
        with self.lock:
            self.pending_question = question
            self.question_answer = None
            self.status = 'awaiting_answer'
            self.trace.record('agent_question',question=question)
            self.question_event.clear()
        if not self.question_event.wait(timeout=600):
            raise TimeoutError('Ответ на вопрос агента не получен за 10 минут')
        with self.lock:
            value = self.question_answer
            self.question_answer = None
            self.pending_question = None
            self.status = 'agent_running'
            return value

    def answer_agent(self, value):
        with self.lock:
            if self.status != 'awaiting_answer' or not str(value).strip():
                raise ValueError('Нет ожидающего вопроса или ответ пуст')
            self.question_answer = str(value).strip()
            self.trace.record('agent_answer',question=self.pending_question,answer=self.question_answer)
            self.question_event.set()
            return self.view()

    def _agent_worker(self):
        try:
            ProfileAgent(self.store,self.get_client()).run(self.profile,self._ask_agent)
            with self.lock:
                self.status = 'assessment_ready'
                self.trace.record('assessment_prepared',assessment=self.profile.assessment.model_dump())
                self.save()
        except Exception as exc:
            with self.lock:
                self.status = 'error'
                self.error = str(exc) if type(exc).__name__ in {'LLMError','ValueError','TimeoutError'} else type(exc).__name__
                self.trace.record('agent_error',error_type=type(exc).__name__,message=self.error)
                self.save()

    def remove_topic(self, index):
        with self.lock:
            self.ensure_editable()
            if self.profile.assessment is None:
                raise ValueError('Темы ещё не подготовлены')
            if not 0 <= index < len(self.profile.assessment.topics):
                raise ValueError('Неизвестный номер темы')
            removed = self.profile.assessment.topics.pop(index)
            self.profile.assessment_confirmed = False
            self.trace.record('topic_removed',index=index,title=removed.title)
            self.save()
            return self.view()

    def confirm_assessment(self):
        with self.lock:
            self.ensure_editable()
            confirm_assessment(self.profile)
            self.status = 'assessment_confirmed'
            self.trace.record('assessment_confirmed',assessment=self.profile.assessment.model_dump())
            self.save()
            return self.view()

    def sites(self, sites, mode='include'):
        with self.lock:
            self.ensure_editable()
            set_sites(self.profile,sites,mode)
            self.trace.record('sites_selected',sites=self.profile.source_preferences)
            self.save()
            return self.view()

    def export(self):
        with self.lock:
            payload = export_profile(self.profile)
            path = self.run_dir/'export.json'
            path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            self.status = 'complete'
            self.trace.record('profile_exported',path=str(path))
            shared = publish(payload)
            if shared:
                self.trace.record('profile_published',path=str(shared))
            return {'profile':payload,'path':str(path),'trace_path':str(self.trace.path),
                    'published':str(shared) if shared else None}

    def trace_events(self, limit=80):
        limit = min(max(int(limit),1),200)
        return [json.loads(line) for line in self.trace.path.read_text(encoding='utf-8').splitlines()[-limit:]]

    def close(self):
        if self.client:
            self.client.close()

class Controller:
    def __init__(self, store=None, client_factory=None, runs_dir=RUNS):
        self.store = store or Store()
        self.field_labels = self.store.field_labels()
        self.field_value_labels = self.store.field_value_labels()
        self.client_factory = client_factory or (lambda:make_client(settings()))
        self.runs_dir = Path(runs_dir)
        self.sessions = {}
        self.lock = threading.RLock()

    def companies(self, query):
        rows = self.store.companies(query,limit=20)
        return [{'id':r.get('SYNTHETIC_U'),'industry':r.get('MAINOKVED_NAME',''),
                 'region':r.get('REGION',''),'revenue':r.get('REVENUE_COMPANY','')} for r in rows]

    def create(self, company_id=None, name=None, manual=False):
        if not company_id and not manual:
            raise ValueError('Выберите компанию или создайте профиль вручную')
        session_id = str(uuid4())
        run_dir = self.runs_dir/session_id
        run_dir.mkdir(parents=True,mode=0o700)
        trace = ProfileTrace(run_dir/'trace.jsonl')
        store = TracedStore(self.store,trace)
        profile = create_profile(store,company_id=company_id,manual={'name':name.strip()} if not company_id and (name or '').strip() else None)
        trace.record('company_selected',company_id=company_id,profile_id=profile.id)
        session = Session(session_id,profile,store,trace,run_dir,self.client_factory,
                          self.field_labels,self.field_value_labels)
        with self.lock:
            self.sessions[session_id] = session
        if company_id:
            session.start_overview()
        return session

    def get(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise KeyError('Сессия не найдена; начните новый проход')
        return session

    def close(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()
