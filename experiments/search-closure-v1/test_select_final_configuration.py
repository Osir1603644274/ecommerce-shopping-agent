from select_final_configuration import choose

def test_lower_bound_winner_before_latency():
    assert choose({'a':{'equal_source_mean_lower':.8},'b':{'equal_source_mean_lower':.9}},['a','b'],{'a':1,'b':10})['method']=='b'

def test_tie_needs_actual_latency_then_earlier_checkpoint():
    scores={m:{'equal_source_mean_lower':.8} for m in ['w111/epoch3','w111/epoch1']}
    assert choose(scores,list(scores))['status']=='NEEDS_ACTUAL_LATENCY_TIE_BREAK'
    assert choose(scores,list(scores),{'w111/epoch3':1,'w111/epoch1':2})['method']=='w111/epoch3'
    assert choose(scores,list(scores),{m:1 for m in scores})['method']=='w111/epoch1'

def test_undefined_denominator_does_not_pick_surviving_method():
    assert choose({'a':{'equal_source_mean_lower':None},'b':{'equal_source_mean_lower':.9}},['a','b'])['status']=='NO_VALID_SELECTION'
