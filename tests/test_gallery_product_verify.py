import os
from pathlib import Path
import pytest
from experiments.gallery_product_verify import worker_environment, fixed_plan, require_registered_plan


@pytest.mark.parametrize('key',[None,'SystemRoot','SYSTEMROOT'])
def test_parent_environment_supports_normalized_windows_keys(key):
    original={'PYTHONPATH':'sensitive-synthetic','PATH':'old'}
    if key:original[key]='C:/Windows'
    result=worker_environment(original)
    assert result['PATH']==os.pathsep.join([str(Path('C:/Windows/System32')),str(Path('C:/Windows'))])
    assert 'PYTHONPATH' not in result and original['PATH']=='old'


def test_livephoto_mode_has_only_two_fixed_stages_and_sixteen_requests():
    plan,rows=fixed_plan('livephoto')
    assert len(rows)==1 and rows[0][0]=='livephoto'
    assert plan['total_request_limit']==16
    assert plan['stages']==[dict(sample='1257912749013336118',selected=[1,5,9],prepare_limit=11,download_limit=5)]
    require_registered_plan(plan,plan)


def test_default_four_stage_plan_remains_twenty_four_requests():
    plan,rows=fixed_plan('all')
    assert len(rows)==2 and plan['total_request_limit']==24


@pytest.mark.parametrize('change',['sample_mode','stages','total_request_limit'])
def test_registered_plan_cannot_be_widened_or_replaced(change):
    from copy import deepcopy
    plan,_=fixed_plan('livephoto');registration=deepcopy(plan)
    registration[change]=fixed_plan('all')[0][change]
    with pytest.raises(ValueError,match='registered_plan_changed'):
        require_registered_plan(registration,plan)


def test_unknown_sample_is_not_an_arbitrary_network_target():
    with pytest.raises(ValueError,match='unknown_fixed_sample'):
        fixed_plan('https://example.com/sensitive')
