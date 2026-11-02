import copy,json
from pathlib import Path
import pytest
from steel_engine.bim_contract import validate


def split_package():
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    original=p['physical']['members'][0]; aid='analysis-beam'
    a,b=original['axis']; middle=[(x+y)/2 for x,y in zip(a,b)]
    p['analysis']={'nodes':[{'id':'a','point':a},{'id':'b','point':b}],'elements':[{'id':aid,'node_ids':['a','b']}]}
    left=copy.deepcopy(original);right=copy.deepcopy(original)
    left.update(id='left',axis=[a,middle],analysis_ids=[],analysis_segments=[{'analysis_id':aid,'start':0,'end':.5}])
    right.update(id='right',axis=[middle,b],analysis_ids=[],analysis_segments=[{'analysis_id':aid,'start':.5,'end':1}])
    p['physical']['members']=[left,right]
    for j in p['physical']['joints']:
        for e in j['member_ends']: e['member_id']='left' if e['end']=='start' else 'right'
    p['physical']['joints'].append({'id':'splice','point':middle,'member_ends':[{'member_id':'left','end':'end'},{'member_id':'right','end':'start'}]})
    p['physical']['joint_dispositions'].append({'joint_id':'splice','reason':'Synthetic continuous test joint'})
    return p


def test_disjoint_analysis_intervals(): assert not validate(split_package())


@pytest.mark.parametrize('value,code',[(.4,'analysis_overlap'),(.6,'analysis_mapping')])
def test_analysis_interval_overlap_and_gap(value,code):
    p=split_package();p['physical']['members'][1]['analysis_segments'][0]['start']=value
    assert any(f['code']==code for f in validate(p))


def test_interior_support_attachment_and_bad_station():
    p=split_package();m=p['physical']['members'][0];a,b=m['axis'];point=[(x+y)/2 for x,y in zip(a,b)]
    p['physical']['joints'].append({'id':'attachment','point':point,'member_ends':[],'attachments':[{'member_id':'left','station':.5}]})
    p['physical']['joint_dispositions'].append({'joint_id':'attachment','reason':'Synthetic test attachment'})
    assert not validate(p)
    p['physical']['joints'][-1]['attachments'][0]['station']=.6
    assert any(f['code']=='joint_attachment' for f in validate(p))
