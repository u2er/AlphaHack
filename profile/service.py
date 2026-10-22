from __future__ import annotations
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from .models import Fact, Profile
from utils.models import now

# Unknown/not applicable is an explicit acceptable answer; silence is not.
QUESTIONS = {
 'activity': 'Чем компания занимается фактически? Назовите основные продукты/услуги и дополнительные направления.',
 'scale': 'Каков масштаб и положение компании на рынке? Уточните выручку/размер, роль или долю рынка и группу компаний, если известно.',
 'geography': 'Где компания работает и продаёт? Какие страны участвуют в закупках и поставках?',
 'counterparties': 'Кто ваши клиенты, ключевые покупатели, поставщики, партнёры и конкуренты? От кого и насколько зависит бизнес?',
 'sensitivity': 'Какие проблемы и факторы сейчас существенны: кредиты и ставка, валюты, сырьё, логистика, спрос, регулирование?',
 'user_role': 'Какова ваша роль и какие решения вы принимаете?'
}
LABELS = {
 'name':'Название компании', 'ORG_TYPE':'Тип организации', 'OKFS_NAME':'Форма собственности',
 'COMPANY_AGE_YEARS_GRADATION':'Возраст компании', 'ECO_MARK_LABEL':'Стадия развития бизнеса',
 'NEGATIVEMARK':'Признак ключевого собственника', 'CL_TYPE':'Тип клиента банка',
 'MAINOKVED_CODE':'Основной ОКВЭД', 'MAINOKVED_NAME':'Отрасль / деятельность',
 'REVENUE_COMPANY':'Годовая выручка (единица и год в источнике не указаны)',
 'AMOUNT_WORKERS_FNS_GRADATION':'Численность ФНС', 'AMOUNT_WORKERS_SPARK_GRADATION':'Численность СПАРК',
 'CITY':'Город регистрации', 'REGION':'Регион регистрации', 'GROUP_ID':'Группа компаний',
 'CL_PRODUCT_RKO_MARK':'РКО', 'CL_PRODUCT_GAR_MARK':'Гарантии', 'CL_PRODUCT_LOA_MARK':'Кредиты',
 'CL_PRODUCT_LEA_MARK':'Лизинг', 'CL_PRODUCT_DEP_MARK':'Депозиты', 'CL_PRODUCT_ZPP_MARK':'Зарплатный проект',
 'FACTORING_MARK':'Факторинг', 'FTS_FLAG':'Наличие сведений ФТС', 'FTS_COUNTRY_NAME':'Страна из данных ФТС'
}

def create_profile(store, company_id=None, manual=None):
    p = Profile(id=str(uuid4()),company_id=company_id)
    if company_id:
        row = store.company(company_id)
        p.facts = {k:Fact(value=v,source='appendix1:df_Uid_data_hack.csv') for k,v in row.items() if v != '' and k != '_conflicts'}
        p.source_facts = {k:fact.model_copy(deep=True) for k,fact in p.facts.items()}
        p.signals = store.signals(company_id)
        p.signals['data_conflicts'] = row.get('_conflicts', {})
        if p.signals['data_conflicts']:
            p.signals.setdefault('limitations', []).append('ID имеет конфликтующие карточки: принадлежность связей также неоднозначна.')
    if manual:
        for key,value in manual.items():
            set_fact(p,key,value)
    return p

def invalidate(p):
    p.profile_confirmed = False
    p.assessment_confirmed = False
    p.assessment = None
    p.overview = None
    p.evidence = {}
    p.updated_at = now()

def set_fact(p, key, value):
    if key in {'SYNTHETIC_U','GROUP_ID'}:
        raise ValueError('Synthetic identity cannot be edited')
    if key not in LABELS and key not in p.facts and key not in p.signals.get('data_conflicts', {}):
        raise ValueError('Unknown editable field')
    if key in p.facts:
        p.trace.append({'event':'fact_replaced','field':key,'previous':p.facts[key].model_dump(),'at':now()})
    p.facts[key] = Fact(value=value,source='user',confirmed=True)
    invalidate(p)

def clear_fact(p, key):
    if key in {'SYNTHETIC_U','GROUP_ID'}:
        raise ValueError('Synthetic identity cannot be edited')
    if key not in LABELS and key not in p.facts:
        raise ValueError('Unknown editable field')
    p.facts.pop(key,None)
    invalidate(p)

def answer(p,key,value):
    if key not in QUESTIONS or not value.strip():
        raise ValueError('Answer a known question; use «не знаю» if unknown')
    if key in p.answers:
        p.trace.append({'event':'answer_replaced','field':key,'previous':p.answers[key].model_dump(),'at':now()})
    p.answers[key] = Fact(value=value.strip(),source='user',confirmed=True)
    invalidate(p)

def clear_answer(p,key):
    if key not in QUESTIONS:
        raise ValueError('Unknown question')
    p.answers.pop(key,None)
    invalidate(p)

def restore_source_facts(p, store):
    """Upgrade drafts created before source_facts was introduced."""
    if p.company_id and not p.source_facts:
        row = store.company(p.company_id)
        p.source_facts = {k:Fact(value=v,source='appendix1:df_Uid_data_hack.csv')
                          for k,v in row.items() if v != '' and k != '_conflicts'}

def missing_questions(p):
    # CSV registration, OKVED and product flags do not establish operational geography,
    # actual product range, market position, economic dependencies or the user's role.
    return {k:v for k,v in QUESTIONS.items() if k not in p.answers}

def profile_issues(p):
    """Comparable, evidenced inconsistencies only; source gaps are not conflicts."""
    return [f'В исходных карточках расходится поле «{LABELS.get(key, key)}»: {values}. Выберите значение.'
            for key, values in p.signals.get('data_conflicts', {}).items()
            if key not in p.facts or p.facts[key].source != 'user']

def confirm_profile(p):
    unresolved = [k for k in p.signals.get('data_conflicts', {}) if k not in p.facts or p.facts[k].source != 'user']
    if unresolved:
        raise ValueError('Resolve conflicting fields: ' + ', '.join(unresolved))
    for fact in p.facts.values():
        fact.confirmed = True
    p.profile_confirmed = True
    p.updated_at = now()

def normalize_site(value):
    value = value.strip()
    if '://' not in value:
        value = 'https://' + value
    parts = urlsplit(value)
    if parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('Use a public HTTP(S) URL without credentials')
    host = parts.hostname.encode('idna').decode('ascii').lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host or any(not x or x.startswith('-') or x.endswith('-') or
            not all(ch.isalnum() or ch == '-' for ch in x) for x in host.split('.')):
            raise ValueError('Invalid public hostname')
    else:
        if not ip.is_global:
            raise ValueError('Use a public site address')
        if ip.version == 6:
            host = '['+host+']'
    port = parts.port
    return urlunsplit((parts.scheme,host+(f':{port}' if port else ''),parts.path.rstrip('/'),parts.query,''))

def set_sites(p, sites, mode='include'):
    if mode not in {'include','only'}:
        raise ValueError('Unknown source mode')
    p.source_preferences = {'sites':list(dict.fromkeys(normalize_site(s) for s in sites if s.strip())), 'mode':mode}
    p.updated_at = now()

def confirm_assessment(p):
    if not p.profile_confirmed or p.assessment is None:
        raise ValueError('Prepare and confirm the profile first')
    p.assessment_confirmed = True
    p.updated_at = now()

def export_profile(p):
    if not p.profile_confirmed or not p.assessment_confirmed:
        raise ValueError('Both profile and suggested topics require confirmation')
    return {'schema_version':'1','profile_id':p.id,'company_id':p.company_id,
            'company_facts':{k:v.model_dump() for k,v in p.facts.items()},
            'source_facts':{k:v.model_dump() for k,v in p.source_facts.items()},
            'draft_overview':p.overview.model_dump() if p.overview else None,
            'user_context':{k:v.model_dump() for k,v in p.answers.items()},
            'relationship_signals':p.signals,'monitoring_context':p.assessment.model_dump(),
            'evidence':p.evidence,'source_preferences':p.source_preferences,'updated_at':p.updated_at}

def save(p, path):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(p.model_dump_json(indent=2),encoding='utf-8')
    tmp.replace(path)

def load(path):
    return Profile.model_validate_json(Path(path).read_text(encoding='utf-8'))
