import os
from pathlib import Path
import pytest
from experiments.gallery_product_verify import worker_environment


@pytest.mark.parametrize('key',[None,'SystemRoot','SYSTEMROOT'])
def test_parent_environment_supports_normalized_windows_keys(key):
    original={'PYTHONPATH':'sensitive-synthetic','PATH':'old'}
    if key:original[key]='C:/Windows'
    result=worker_environment(original)
    assert result['PATH']==os.pathsep.join([str(Path('C:/Windows/System32')),str(Path('C:/Windows'))])
    assert 'PYTHONPATH' not in result and original['PATH']=='old'
