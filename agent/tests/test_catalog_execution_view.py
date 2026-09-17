from app.catalog_execution_view import phase_fields


def test_prepare_records_user_units_and_real_changes_without_provider_secrets():
    r=dict(facet='容量',mode='require',value='888ml',terms=['888ml'])
    run=dict(message='无糖可乐 888ml',catalogPlan={'action':'search'},catalogBefore={},
        catalogNext={'query':'无糖可乐 888ml','retrievalQuery':'无糖可乐 888ml','requirements':[r]},
        catalogRouteCall={'model':'fixture','durationMs':12,'secret':'must-not-be-public','prompt':'hidden'})
    node=phase_fields(run,'prepare',.1)
    assert node['input']['用户原话']=='无糖可乐 888ml'
    assert node['output']['当前条件'][0]['要求']=='888ml'
    assert node['output']['新增或修改条件'][0]['要求']=='888ml'
    assert 'must-not-be-public' not in str(node) and 'hidden' not in str(node)
    assert node['detail']['executionMode']=='fixed_catalog_workflow'


def test_compare_retrieve_does_not_report_a_new_search():
    run=dict(catalogPlan={'action':'compare'},catalogNext={'query':'可乐','scope':{'groups':[],
        'sources':[{'source':'kuaisearch','hits':[{}]}]}})
    node=phase_fields(run,'retrieve',.1)
    assert node['input']['实际调用']=='无新检索'
    assert node['output']['来源返回']==[]
    assert node['detail']['retrievalExecuted'] is False
