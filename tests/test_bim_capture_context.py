"""Accepted interfaces must reach capture before its source-scope checks."""
import copy
import pytest
from steel_engine import bim_export as B


@pytest.mark.parametrize('module',('HR','CFS'))
def test_native_capture_receives_saved_and_accepted_interfaces_without_mutation(module):
    cfg={'bim':{'required':True,'panels':[{'id':'saved-panel'}],
                'interfaces':[{'id':'saved-interface','scope':'delegated'}]}}
    selected={'module':module,'layout':{},'declarations':{'panels':[{'id':'entered-panel'}],
        'interfaces':[{'id':'existing-frame','scope':'existing','responsible_party':'Existing-building engineer',
                       'member_ids':['column-1'],'requirements':{'survey':'required'}}]}}
    originals=copy.deepcopy((cfg,selected))
    actual=B.capture_configuration(cfg,selected)
    assert [i['id'] for i in actual['bim']['interfaces']]==['saved-interface','existing-frame']
    assert [i['id'] for i in actual['bim']['panels']]==['saved-panel','entered-panel']
    assert actual['bim']['required'] is True
    actual['bim']['interfaces'][1]['requirements']['survey']='mutated only in capture copy'
    assert (cfg,selected)==originals


def test_no_physical_selection_does_not_invent_existing_scope():
    cfg={'bim':{'interfaces':[]}}
    actual=B.capture_configuration(cfg,None)
    assert not any(i.get('scope')=='existing' for i in actual['bim']['interfaces'])


