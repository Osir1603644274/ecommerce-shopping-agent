from trace_metering import meter_requests

def observation(attempts,status,elapsed,wall):
    return {'trace':{'status':status,'attempts':attempts},'elapsedMs':elapsed,'sinceFirstRequestMs':wall}

def test_failed_attempt_is_retained_and_replays_are_not_double_billed():
    failure={'runId':'one','modelReceipt':{'modelCallId':'failed','status':'FAILED','usage':None,'cost':None,'durationMs':25000},'toolReceipts':[{'toolCallId':'t1','durationMs':3}]}
    success={'runId':'two','modelReceipt':{'modelCallId':'success','status':'SUCCEEDED','usage':{'prompt_tokens':10,'completion_tokens':2},'cost':'0.01','durationMs':900},'toolReceipts':[{'toolCallId':'t2','durationMs':4}]}
    result=meter_requests([observation([failure],'FAILED',25010,25010),observation([failure,success],'COMPLETED',920,28000),observation([failure,success],'COMPLETED',15,29000)])
    assert len(result['modelCalls'])==2 and result['modelCalls'][0]['usage'] is None
    assert result['modelMs']==25900 and result['toolMs']==7
    assert result['elapsedMs']==25945 and result['firstContentMs']==28000
    assert result['meteringComplete'] is True

def test_interrupted_unmetered_attempt_does_not_become_zero_cost():
    attempts=[{'runId':'lost','modelReceipt':{'status':'POTENTIALLY_STARTED','usage':None}}, {'runId':'new','modelReceipt':{'modelCallId':'retry','durationMs':10}}]
    result=meter_requests([observation(attempts,'FAILED',100,100)])
    assert result['meteringComplete'] is False and 'firstContentMs' not in result


def test_only_external_verified_rejection_counts_as_useful_response():
    attempt={'runId':'r','modelReceipt':{'modelCallId':'m','status':'SUCCEEDED','durationMs':200},
             'toolReceipts':[{'toolCallId':'t','status':'FAILED','statusCode':409,'durationMs':10}]}
    rejected=observation([attempt],'FAILED',250,250);rejected['trace']['requestId']='known'
    assert 'firstContentMs' not in meter_requests([rejected])
    assert 'firstContentMs' not in meter_requests([rejected],supported_rejections={'other'})
    measured=meter_requests([rejected],supported_rejections={'known'})
    assert measured['firstContentMs']==250 and measured['modelCalls'][0]['modelCallId']=='m'
