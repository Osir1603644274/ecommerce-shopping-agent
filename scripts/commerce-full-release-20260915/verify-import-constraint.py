"""Prove the real PK constraint rejects a batch and rollback leaves no prefix row."""
import json
import pymysql
from prepare import OUT,write
from import_catalog import lab

secret=json.loads((OUT/'stage-connection.private.json').read_text())
assert secret['database']=='commerce_acceptance'
db=pymysql.connect(**secret,cursorclass=pymysql.cursors.DictCursor,autocommit=False)
test_id=8999900000000001
try:
    with db.cursor() as c:
        c.execute('SELECT * FROM product ORDER BY id LIMIT 1');original=c.fetchone()
        c.execute('SELECT COUNT(*) n FROM product');before=c.fetchone()['n']
        c.execute('SELECT id FROM product WHERE id=%s',(test_id,));assert not c.fetchone()
        fresh={**original,'id':test_id,'source':'release-constraint-probe','source_item_id':'1'}
        conflicting={**original,'source':'release-constraint-probe','source_item_id':'2'}
        try:
            lab.insert_rows(c,'product',[fresh,conflicting])
        except pymysql.IntegrityError as exc:
            assert exc.args[0]==1062
        else:raise AssertionError('primary-key collision was accepted')
    db.rollback()
    with db.cursor() as c:
        c.execute('SELECT COUNT(*) n FROM product');assert c.fetchone()['n']==before
        c.execute('SELECT id FROM product WHERE id=%s',(test_id,));assert not c.fetchone()
    write('IMPORT-UNIQUE-CONSTRAINT.json',dict(status='PASS',database='commerce_acceptance',
        duplicatePrimaryKeyRejected=True,prefixRowNotPersisted=True,productCountUnchanged=before))
    print('Real MySQL unique constraint + atomic rollback PASS; no fixture rows persisted.')
finally:db.rollback();db.close()
