"""Per-run JSONL audit of profile inputs, model calls and database reads."""
from __future__ import annotations
import json
import os
from pathlib import Path
from uuid import uuid4
from utils.models import now

class ProfileTrace:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).resolve().parent/'logs'/f'{uuid4()}.jsonl'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # Traces contain profile fields, user answers and retrieved source text.
        descriptor = os.open(self.path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        os.close(descriptor)
        self.record('run_started',trace_path=str(self.path))

    def record(self, event, **data):
        row = {'at':now(),'event':event,**data}
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')

class TracedClient:
    """The LLM adapter receives no credentials in the trace; only its application payload."""
    def __init__(self, client, trace):
        self.client,self.trace = client,trace
        self.provider = getattr(client,'provider','test')
        self.model = getattr(client,'model','test')

    def complete_structured(self, system, payload, response_model, images=()):
        call_id = str(uuid4())
        # Pictures are recorded by size only: the trace never holds their pixels.
        shown = {'images':[f'{len(url)} chars' for url in images]} if images else {}
        self.trace.record('llm_request',call_id=call_id,provider=self.provider,model=self.model,
                          response_schema=response_model.__name__,system=system,payload=payload,**shown)
        try:
            extra = {'images':list(images)} if images else {}
            result = self.client.complete_structured(system,payload,response_model,**extra)
        except Exception as exc:
            self.trace.record('llm_error',call_id=call_id,error_type=type(exc).__name__,
                              error=str(exc) if type(exc).__name__ == 'LLMError' else 'Model call failed')
            raise
        self.trace.record('llm_response',call_id=call_id,response_schema=response_model.__name__,
                          data=result.data,usage=result.usage)
        return result

    def close(self):
        if hasattr(self.client,'close'):
            self.client.close()

class TracedStore:
    def __init__(self, store, trace):
        self.store,self.trace = store,trace

    def _call(self, method, *args, **kwargs):
        call_id = str(uuid4())
        self.trace.record('db_request',call_id=call_id,method=method,arguments={'args':args,'kwargs':kwargs})
        try:
            result = getattr(self.store,method)(*args,**kwargs)
        except Exception as exc:
            self.trace.record('db_error',call_id=call_id,method=method,error_type=type(exc).__name__)
            raise
        self.trace.record('db_result',call_id=call_id,method=method,result=result)
        return result

    def companies(self, query='', limit=20):
        return self._call('companies',query,limit)

    def company(self, company_id):
        return self._call('company',company_id)

    def signals(self, company_id, limit=10):
        return self._call('signals',company_id)

    def search(self, query, limit=5):
        return self._call('search',query,limit=limit)

    def field_labels(self):
        return self._call('field_labels')

    def field_value_labels(self):
        return self._call('field_value_labels')
