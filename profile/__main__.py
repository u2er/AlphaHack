import argparse
import json
from pathlib import Path
from .db.store import Store
from .db.connection import ROOT
from utils.config import settings
from utils.llm import make_client, LLMError
from utils.logging_utils import setup_logging
from .agent import ProfileAgent
from .draft import prepare_overview, suggest_edit, apply_edit
from .trace import ProfileTrace, TracedClient, TracedStore
from .metadata import field_context
from .service import (QUESTIONS, LABELS, create_profile, set_fact, answer, missing_questions, confirm_profile,
                      confirm_assessment, set_sites, export_profile, save, load, profile_issues, restore_source_facts)

def display(value):
    print(json.dumps(value,ensure_ascii=False,indent=2))

def required(prompt, trace=None):
    while True:
        if trace:
            trace.record('user_question',question=prompt)
        value = input(prompt+'\n> ').strip()
        if value:
            if trace:
                trace.record('user_answer',question=prompt,answer=value)
            return value
        print('Введите ответ; можно указать «не знаю» или «не применимо».')

def yes(prompt, trace=None):
    result = input(prompt+' [да/нет] > ').strip().lower() in {'да','yes','y','д'}
    if trace:
        trace.record('user_confirmation',question=prompt,accepted=result)
    return result

def edit(p, client, trace=None, field_metadata=None):
    while True:
        instruction = input('Ключ поля или правка словами (Enter — закончить): ').strip()
        if not instruction:
            return
        if instruction in QUESTIONS:
            value = required(QUESTIONS[instruction],trace)
            answer(p,instruction,value)
            if trace:
                trace.record('field_edit',scope='answers',key=instruction,value=value,method='exact_key')
        elif instruction in p.facts or instruction in LABELS:
            try:
                value = required(LABELS.get(instruction,instruction),trace)
                set_fact(p,instruction,value)
                if trace:
                    trace.record('field_edit',scope='facts',key=instruction,value=value,method='exact_key')
            except ValueError as exc:
                print(exc)
        else:
            if trace:
                trace.record('natural_edit_requested',instruction=instruction)
            proposal = suggest_edit(p,client,instruction,field_metadata)
            if trace:
                trace.record('natural_edit_proposed',proposal=proposal.model_dump())
            if proposal.scope == 'clarify':
                print('Нужно уточнить:',proposal.explanation)
                continue
            before = p.facts.get(proposal.key) if proposal.scope == 'facts' else p.answers.get(proposal.key)
            display({'предложенная_правка':proposal.model_dump(),
                     'старое_значение':before.value if before else None})
            if yes('Применить эту правку?',trace):
                apply_edit(p,proposal)
                if trace:
                    trace.record('natural_edit_applied',proposal=proposal.model_dump())
            else:
                if trace:
                    trace.record('natural_edit_rejected',proposal=proposal.model_dump())
                print('Правка отклонена; данные не изменены.')

def wizard(store, resume=None, output=None):
    trace = ProfileTrace()
    store = TracedStore(store,trace)
    metadata = field_context(store)
    print('Подробный журнал запуска:',trace.path)
    if resume:
        p = load(resume)
        trace.record('draft_loaded',path=str(resume),profile_id=p.id)
        restore_source_facts(p,store)
    else:
        while True:
            query = input('Найти компанию: ID, отрасль или регион (Enter — создать вручную): ').strip()
            if not query:
                p = create_profile(store,manual={'name':required('Название компании',trace)})
                for key,label in LABELS.items():
                    if key == 'name':
                        continue
                    value = input(label+' (Enter — неизвестно): ').strip()
                    if value:
                        set_fact(p,key,value)
                        trace.record('field_edit',scope='facts',key=key,value=value,method='manual_creation')
                break
            rows = store.companies(query)
            for row in rows:
                print(row['SYNTHETIC_U'],row.get('MAINOKVED_NAME','Есть конфликт исходных записей'),row.get('REGION',''))
            company_id = input('Введите ID (Enter — повторить поиск): ').strip()
            if not company_id:
                continue
            try:
                p = create_profile(store,company_id)
                trace.record('company_selected',profile_id=p.id,company_id=company_id)
                break
            except ValueError as exc:
                print(exc)
    draft = ROOT/'profile'/'drafts'/f'{p.id}.json'
    client = None
    try:
        client = TracedClient(make_client(settings()),trace)
        print('\nЧерновик профиля; исходные банковские признаки могут требовать уточнения:')
        display({k:{'label':LABELS.get(k,k),**v.model_dump()} for k,v in p.facts.items()})
        print('\nСигналы связей (тип отношений не установлен):')
        display(p.signals)
        for key,variants in p.signals.get('data_conflicts',{}).items():
            if key not in p.facts or p.facts[key].source != 'user':
                display({'conflicting_field':key,'values':variants})
                set_fact(p,key,required('Уточните значение или укажите «не знаю»',trace))
        if p.overview is None:
            prepare_overview(p,client,metadata)
        print('\nКраткий черновик от модели (исходные поля выше доступны отдельно):')
        display(p.overview.model_dump())
        edit(p,client,trace,metadata)
        for key,question in missing_questions(p).items():
            answer(p,key,required(question,trace))
            save(p,draft)
        while not p.assessment_confirmed:
            if p.overview is None:
                prepare_overview(p,client,metadata)
                print('\nОбновлённый черновик после ваших ответов:')
                display(p.overview.model_dump())
            if p.profile_confirmed and profile_issues(p):
                print('\nВ профиле есть расхождения:')
                display(profile_issues(p))
                if yes('Исправить профиль перед подбором тем?',trace):
                    edit(p,client,trace,metadata)
            while not p.profile_confirmed:
                display({'facts':{k:v.value for k,v in p.facts.items()},'answers':{k:v.value for k,v in p.answers.items()},
                         'issues':profile_issues(p)})
                if yes('Подтвердить профиль, включая отмеченные расхождения?',trace):
                    confirm_profile(p)
                    trace.record('profile_confirmed',profile_id=p.id,issues=profile_issues(p))
                else:
                    edit(p,client,trace,metadata)
            save(p,draft)
            if p.overview is None:
                prepare_overview(p,client,metadata)
                print('\nОбновлённый черновик профиля:')
                display(p.overview.model_dump())
                save(p,draft)
            if p.assessment is None:
                ProfileAgent(store,client).run(p,lambda question: required(question,trace))
                trace.record('assessment_prepared',assessment=p.assessment.model_dump())
                save(p,draft)
            print('\nПредварительные темы наблюдения:')
            display(p.assessment.model_dump())
            print('\nИспользованные источники:')
            display([{k:r[k] for k in ('id','filename','page','date_hint','date_warning')} for r in p.evidence.values()])
            if yes('Подтвердить темы и ограничения?',trace):
                confirm_assessment(p)
                trace.record('assessment_confirmed',assessment=p.assessment.model_dump())
                break
            choice = input('Номер темы для удаления, «п» — исправить профиль и пересобрать темы, Enter — сохранить черновик и выйти: ').strip().lower()
            if not choice:
                return
            if choice in {'п','p'}:
                edit(p,client,trace)
                if p.profile_confirmed:
                    print('Изменений нет; темы сохранены.')
                continue
            try:
                index = int(choice)-1
                if index < 0:
                    raise ValueError()
                p.assessment.topics.pop(index)
                trace.record('topic_removed',index=index)
            except (ValueError,IndexError):
                print('Неизвестный номер темы')
        while True:
            sites = input('Сайты для наблюдения через запятую (необязательно): ').split(',')
            mode = 'only' if any(s.strip() for s in sites) and yes('В будущем искать только на этих сайтах?',trace) else 'include'
            try:
                set_sites(p,sites,mode)
                trace.record('sites_selected',sites=p.source_preferences)
                break
            except ValueError as exc:
                print(exc)
        result = export_profile(p)
        target = Path(output) if output else ROOT/'profile'/'output'/f'{p.id}.json'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        trace.record('profile_exported',path=str(target),profile_id=p.id)
        display(result)
        print('Готовый профиль:',target)
    finally:
        save(p,draft)
        trace.record('draft_saved',path=str(draft),profile_id=p.id)
        print('Черновик:',draft)
        print('Подробный журнал:',trace.path)
        if client and hasattr(client,'close'):
            client.close()

def main():
    parser = argparse.ArgumentParser(description='Пять этапов подготовки профиля компании')
    parser.add_argument('--dsn',help='PostgreSQL DSN; defaults to PROFILE_DATABASE_URL')
    parser.add_argument('--resume',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--list',metavar='QUERY',help='Поиск компаний без запуска анкеты')
    args = parser.parse_args()
    setup_logging()
    try:
        store = Store(args.dsn)
        if args.list is not None:
            display(store.companies(args.list))
        else:
            wizard(store,args.resume,args.output)
    except (ValueError,FileNotFoundError,LLMError) as exc:
        parser.exit(1,f'Ошибка: {exc}\n')
    except (KeyboardInterrupt,EOFError):
        parser.exit(130,'\nВвод прерван. Созданный черновик сохранён.\n')

if __name__ == '__main__':
    main()
