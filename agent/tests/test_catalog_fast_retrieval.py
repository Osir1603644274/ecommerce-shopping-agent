"""Exact lexical tie handling and source identity, no model call."""
import sqlite3
import pytest
from app.catalog_fast_retrieval import lexical_topk,resolve_rows


def test_rank_topk_collects_boundary_ties_before_rowid_tie_break():
    db=sqlite3.connect(':memory:');db.execute('CREATE VIRTUAL TABLE words USING fts5(text)')
    for rid in reversed(range(1,1400)):
        db.execute('INSERT INTO words(rowid,text) VALUES(?,?)',(rid,'收纳 透明'))
    actual,_=lexical_topk(db,'words','"收纳"',300)
    expected=db.execute('SELECT rowid,bm25(words) AS score FROM words WHERE words MATCH ? ORDER BY score,rowid LIMIT 300',('"收纳"',)).fetchall()
    assert actual==expected and actual[-1][0]==300
    assert lexical_topk(db,'words','',300)==([],0)


def test_batched_ids_cannot_cross_source_or_silently_drop_a_document():
    db=sqlite3.connect(':memory:');db.execute('CREATE TABLE documents(docid,source)')
    db.executemany('INSERT INTO documents VALUES(?,?)',[('kuaisearch:2','kuaisearch'),('multicpr:2','multicpr')])
    assert resolve_rows(db,'kuaisearch',[1])=={1:'kuaisearch:2'}
    with pytest.raises(ValueError,match='source_mismatch'):resolve_rows(db,'kuaisearch',[1,2])
    with pytest.raises(ValueError,match='missing'):resolve_rows(db,'kuaisearch',[3])
