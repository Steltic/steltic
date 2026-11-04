"""Large-model serialization must preserve hashes, bytes and atomic failure."""
import hashlib
import json
from pathlib import Path
import sys
import tracemalloc

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from steel_engine import bim_json as J


@pytest.mark.parametrize('ascii',[True,False])
def test_streamed_bytes_and_hashes_match_existing_encoding(tmp_path,ascii):
    value={'geometry':[[0.,-0.,1e-10,1e30,1.25]]*10000,'description':'Café 鋼材',
           'nested':{'tuple':(True,None,False),'empty':[]}}
    path=tmp_path/'value.json'
    J.atomic_json(path,value,prefix='.test-',ensure_ascii=ascii)
    expected=(json.dumps(value,sort_keys=True,indent=2,allow_nan=False,ensure_ascii=ascii)+'\n').encode()
    assert path.read_bytes()==expected
    assert J.file_digest(path)==hashlib.sha256(expected).hexdigest()
    assert J.fingerprint(value)==hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),object()])
def test_bad_late_value_keeps_previous_file_and_cleans_scratch(tmp_path,invalid):
    path=tmp_path/'value.json';path.write_text('previous')
    with pytest.raises((ValueError,TypeError)):
        J.atomic_json(path,{'many':[0]*100000,'z_invalid':invalid},prefix='.test-')
    assert path.read_text()=='previous'
    assert sorted(p.name for p in tmp_path.iterdir())==['value.json']
    with pytest.raises((ValueError,TypeError)):J.validate([invalid])


def test_streaming_memory_does_not_scale_with_repeated_geometry(tmp_path):
    # 16 MB encoded data, but only one immutable geometry row in the input.
    # Measure incremental serializer memory, excluding the input itself.
    value={'rows':['x'*4096]*4096}
    tracemalloc.start()
    try:
        J.atomic_json(tmp_path/'large.json',value,prefix='.test-')
        J.fingerprint(value)
        _,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert peak<2*1024*1024
