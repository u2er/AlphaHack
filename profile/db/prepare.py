"""Offline import of appendix 1 and E5 indexing of appendix 2 into PostgreSQL."""
from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
import logging
from pathlib import Path

import numpy as np
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from .connection import ROOT, connect, executemany, copy_rows
from .field_labels import ensure_field_labels
from .store import DEFAULT_MODEL, E5
from parser.loader import load_parser
from parser.csv import csv_rows
from utils.logging_utils import setup_logging

log = logging.getLogger('alfa.profile.prepare')
DEFAULT_SOURCE = ROOT / 'формулировка и данные хакатона'

DDL = (
    '''CREATE TABLE profile_data.company_records (
        id text NOT NULL, payload jsonb NOT NULL)''',
    '''CREATE INDEX company_records_id_idx ON profile_data.company_records (id)''',
    '''CREATE TABLE profile_data.companies (
        id text PRIMARY KEY, industry text, region text, payload jsonb NOT NULL)''',
    '''CREATE INDEX companies_industry_idx ON profile_data.companies (industry)''',
    '''CREATE INDEX companies_region_idx ON profile_data.companies (region)''',
    '''CREATE TABLE profile_data.person_links (
        person_id text, company_id text, city text, region text)''',
    '''CREATE INDEX person_company_idx ON profile_data.person_links (company_id)''',
    '''CREATE INDEX person_person_idx ON profile_data.person_links (person_id)''',
    '''CREATE TABLE profile_data.payments (
        payer text, beneficiary text, amount double precision, payment_count bigint)''',
    '''CREATE INDEX payments_payer_idx ON profile_data.payments (payer)''',
    '''CREATE INDEX payments_beneficiary_idx ON profile_data.payments (beneficiary)''',
    '''CREATE TABLE profile_data.documents (
        id text PRIMARY KEY, filename text NOT NULL, sha256 text NOT NULL,
        date_hint text NOT NULL, extraction text NOT NULL)''',
    '''CREATE TABLE profile_data.pages (
        document_id text REFERENCES profile_data.documents(id), page integer,
        text text NOT NULL, PRIMARY KEY(document_id,page))''',
    '''CREATE TABLE profile_data.chunks (
        id text PRIMARY KEY, document_id text REFERENCES profile_data.documents(id),
        page integer, text text NOT NULL, embedding vector(1024) NOT NULL)''',
    '''CREATE INDEX chunks_document_idx ON profile_data.chunks (document_id)''',
    '''CREATE TABLE profile_data.metadata (key text PRIMARY KEY, value text NOT NULL)''',
)


def import_csv(connection, folder: Path) -> None:
    records = defaultdict(list)
    for row in csv_rows(folder / 'df_Uid_data_hack.csv'):
        records[row['SYNTHETIC_U']].append(row)
        connection.execute('INSERT INTO profile_data.company_records VALUES (%s,%s)',
                           (row['SYNTHETIC_U'], Jsonb(row)))
    for key, variants in records.items():
        common = {column: value for column, value in variants[0].items()
                  if all(row[column] == value for row in variants)}
        conflicts = {column: list(dict.fromkeys(row[column] for row in variants))
                     for column in variants[0] if column not in common}
        if conflicts:
            common['_conflicts'] = conflicts
        connection.execute('INSERT INTO profile_data.companies VALUES (%s,%s,%s,%s)',
                           (key, common.get('MAINOKVED_NAME', ''), common.get('REGION', ''), Jsonb(common)))
    copy_rows(connection, 'COPY profile_data.person_links (person_id,company_id,city,region) FROM STDIN',
        ((row['SYNTHETIC_F'], row['SYNTHETIC_U'], row['CITY_NAME'], row['RC_NAME'])
         for row in csv_rows(folder / 'df_Fid_Uid_link_hack.csv')))
    copy_rows(connection, 'COPY profile_data.payments (payer,beneficiary,amount,payment_count) FROM STDIN',
        ((row['PAYER'], row['BENEFICIARY'], float(row['PAY_AMT']), int(row['PAY_CNT']))
         for row in csv_rows(folder / 'df_Uid_payment_hack.csv')))


def validate_document(document, path: Path):
    """Validate the external parser's complete, page-level result."""
    if not isinstance(document, dict) or not isinstance(document.get('date_hint'), str) \
            or not document['date_hint'].strip():
        raise ValueError(f'Parser must return a nonempty date_hint: {path.name}')
    pages = document.get('pages')
    if not isinstance(pages, list) or not pages:
        raise ValueError(f'Parser must return a nonempty pages list: {path.name}')
    for number, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or page.get('page') != number
                or not isinstance(page.get('text'), str) or not page['text'].strip()
                or not isinstance(page.get('extraction'), str) or not page['extraction'].strip()):
            raise ValueError(f'Invalid parser result for page {number}: {path.name}')
    return pages, document['date_hint'].strip()


def prepare(source=DEFAULT_SOURCE, dsn=None, model_path=DEFAULT_MODEL,
            parser_spec=None, replace=False, embedder=None):
    source = Path(source)
    if not parser_spec:
        raise ValueError('Specify a document parser with --parser parser.module:function')
    parser = load_parser(parser_spec)
    embedder = embedder or E5(model_path)
    with connect(dsn) as connection:
        connection.execute('CREATE EXTENSION IF NOT EXISTS vector')
        existing = connection.execute("SELECT to_regclass('profile_data.companies') AS table_name").fetchone()['table_name']
        if existing and not replace:
            raise ValueError('Profile tables already exist; pass --replace to rebuild them')
        if existing:
            connection.execute('DROP SCHEMA profile_data CASCADE')
        connection.execute('CREATE SCHEMA IF NOT EXISTS profile_data')
        for statement in DDL:
            connection.execute(statement)
        ensure_field_labels(connection)
        import_csv(connection, source / 'приложение 1')
        pdfs = sorted((source / 'приложение 2').glob('*.pdf'))
        if not pdfs:
            raise ValueError('No analytical PDF documents found')
        register_vector(connection)
        chunk_count = 0
        for pdf in pdfs:
            log.info('Индексация %s', pdf.name)
            digest = sha256(pdf.read_bytes()).hexdigest()
            pages, date_hint = validate_document(parser(pdf), pdf)
            extraction = ','.join(sorted({page['extraction'] for page in pages}))
            connection.execute('INSERT INTO profile_data.documents VALUES (%s,%s,%s,%s,%s)',
                               (digest, pdf.name, digest, date_hint, extraction))
            for page in pages:
                connection.execute('INSERT INTO profile_data.pages VALUES (%s,%s,%s)',
                                   (digest, page['page'], page['text']))
                chunks = embedder.split(page['text'])
                if not chunks:
                    raise ValueError(f'No searchable text on page {page["page"]}: {pdf.name}')
                vectors = np.asarray(embedder.encode(chunks), dtype=np.float32)
                if vectors.shape != (len(chunks), 1024):
                    raise ValueError(f'E5 must return one 1024-dimensional vector per chunk: {pdf.name}')
                executemany(connection, '''INSERT INTO profile_data.chunks VALUES (%s,%s,%s,%s,%s)''',
                    ((f'{digest}:{page["page"]}:{i}', digest, page['page'], text, vector)
                     for i, (text, vector) in enumerate(zip(chunks, vectors))))
                chunk_count += len(chunks)
        connection.execute('''CREATE INDEX chunks_embedding_hnsw_idx ON profile_data.chunks
            USING hnsw (embedding vector_cosine_ops)''')
        executemany(connection, 'INSERT INTO profile_data.metadata VALUES (%s,%s)',
            [('embedding_model', 'multilingual-e5-large'),
             ('embedding_prefixes', 'query: / passage: '),
             ('parser', parser_spec), ('schema_version', '2')])
        connection.execute('ANALYZE profile_data.chunks')
        log.info('Готово: %d документов, %d фрагментов', len(pdfs), chunk_count)
        return {'documents': len(pdfs), 'chunks': chunk_count}


def main():
    command = argparse.ArgumentParser(description='Индексировать приложения 1 и 2 в PostgreSQL')
    command.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    command.add_argument('--dsn', help='PostgreSQL DSN; defaults to PROFILE_DATABASE_URL')
    command.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    command.add_argument('--parser', required=True, help='parser.module:function')
    command.add_argument('--replace', action='store_true', help='Replace only profile_data in one transaction')
    args = command.parse_args()
    setup_logging()
    print(prepare(args.source, args.dsn, args.model, args.parser, args.replace))


if __name__ == '__main__':
    main()
