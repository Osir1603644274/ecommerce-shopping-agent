import sqlite3
import pytest
from app.catalog_fast_retrieval_v3 import LexicalReader


def test_parallel_readers_keep_source_order_and_close_owned_connections(tmp_path):
    db=sqlite3.connect(tmp_path/'catalog.sqlite')
    db.execute('CREATE TABLE documents(docid,source)')
    for j,source in enumerate(['kuaisearch','multicpr']):
        directory=tmp_path/'indexes'/source;directory.mkdir(parents=True)
        index=sqlite3.connect(directory/'lexical.sqlite')
        for table in ['words','chars']:index.execute(f'CREATE VIRTUAL TABLE {table} USING fts5(text)')
        for i in range(1,351):
            rid=j*350+i;db.execute('INSERT INTO documents(rowid,docid,source) VALUES(?,?,?)',(rid,f'{source}:{i}',source))
            for table in ['words','chars']:index.execute(f'INSERT INTO {table}(rowid,text) VALUES(?,?)',(rid,'收纳'))
        index.commit();index.close()
    db.commit();db.close()
    reader=LexicalReader(tmp_path)
    try:
        for source in ['kuaisearch','multicpr','kuaisearch']:
            channels,_=reader.collect(reader.submit(source,'收纳'))
            for channel in channels.values():
                assert [r['document_id'] for r in channel]==[f'{source}:{i}' for i in range(1,301)]
        assert len(reader.connections)<=6
        connection=reader.connections[0]
    finally:reader.close()
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):connection.execute('SELECT 1')
