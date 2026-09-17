import hashlib
import json
import pytest
from app.catalog_fast_selection import validated_selection


@pytest.fixture
def fixture(tmp_path):
    def write(name,value):
        path=tmp_path/name
        path.write_text(json.dumps(value),encoding='utf-8')
        return path
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    main=write('catalog_fast_retrieval_v2.py','v2')
    parent=write('catalog_fast_retrieval.py','v1')
    report=write('RESULTS.json',{'status':'DEV_GATE_PASS','parameters':{'kuaisearch':{'nprobe':512}}})
    config={'status':'VERIFIED_DEV_CONFIGURATION','runtimeVersion':2,'evaluation':'RESULTS.json',
        'evaluationSha256':sha(report),'parameters':{'kuaisearch':{'nprobe':512}},
        'fastCodeSha256':sha(main),'parentFastCodeSha256':sha(parent),'modelPath':str(tmp_path/'model')}
    write('selected-v2.json',config)
    return tmp_path,config,write


def test_verified_code_bound_selection(fixture):
    directory,config,_=fixture
    assert validated_selection(directory,2,config['modelPath'],code_directory=directory)==config


@pytest.mark.parametrize('target',['RESULTS.json','catalog_fast_retrieval_v2.py','catalog_fast_retrieval.py'])
def test_changed_report_or_runtime_rejected(fixture,target):
    directory,config,_=fixture
    with (directory/target).open('a') as f:f.write(' ')
    with pytest.raises(ValueError,match='fast_selection_gate_failed'):
        validated_selection(directory,2,config['modelPath'],code_directory=directory)


@pytest.mark.parametrize('field,value',[('parameters',{}),('runtimeVersion',1),('status','PENDING'),('modelPath','wrong')])
def test_unapproved_configuration_rejected(fixture,field,value):
    directory,config,write=fixture
    changed=dict(config);changed[field]=value;write('selected-v2.json',changed)
    with pytest.raises(ValueError,match='fast_selection_gate_failed'):
        validated_selection(directory,2,config['modelPath'],code_directory=directory)


def test_evidence_outside_index_directory_rejected(fixture):
    directory,config,write=fixture
    changed=dict(config,evaluation='../outside.json');write('selected-v2.json',changed)
    with pytest.raises(ValueError,match='fast_selection_path'):
        validated_selection(directory,2,config['modelPath'],code_directory=directory)


def test_v3_binds_both_inherited_implementations(fixture):
    directory,config,write=fixture
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    runtime=write('catalog_fast_retrieval_v3.py','v3')
    config=dict(config,runtimeVersion=3,fastCodeSha256=sha(runtime),
        indexRuntimeCodeSha256=sha(directory/'catalog_fast_retrieval_v2.py'))
    write('selected-v3.json',config)
    assert validated_selection(directory,3,config['modelPath'],code_directory=directory)==config
    write('catalog_fast_retrieval_v2.py','changed index implementation')
    with pytest.raises(ValueError,match='fast_selection_gate_failed'):
        validated_selection(directory,3,config['modelPath'],code_directory=directory)
