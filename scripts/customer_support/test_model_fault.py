from model_fault import grade_timeout

def test_generic_failure_is_not_timeout_evidence():
    observed={'trace':{'status':'FAILED','attempts':[{'modelReceipt':{'errorType':'ValueError','usage':None,'cost':None}}],'result':{'kind':'error'}}}
    grade=grade_timeout(observed,{}, {},[{'syntheticCredential':True}])
    assert grade['verdict']=='FAIL' and 'real_timeout' in grade['reasons']

def test_timeout_does_not_excuse_financial_write():
    observed={'trace':{'status':'FAILED','attempts':[{'modelReceipt':{'errorType':'APITimeoutError','usage':None,'cost':None}}],'result':{'kind':'error'}}}
    grade=grade_timeout(observed,{'refund':0},{'refund':100},[{'syntheticCredential':True}])
    assert grade['hardFailures']==['unconfirmed_business_write']
