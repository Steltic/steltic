"""Bounded-memory JSON writing and hashing for large physical models.

The JSON encodings and hashes are byte-for-byte compatible with the previous
whole-string implementation. Geometry, quantities and provenance are untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def chunks(value, **options):
    """Coalesce encoder fragments without holding the complete JSON string."""
    pending=[];size=0
    for chunk in json.JSONEncoder(**options).iterencode(value):
        pending.append(chunk);size+=len(chunk)
        if size>=65536:
            yield ''.join(pending)
            pending=[];size=0
    if pending:yield ''.join(pending)


def validate(value):
    for _ in chunks(value,allow_nan=False):pass


def fingerprint(value):
    digest=hashlib.sha256()
    for chunk in chunks(value,sort_keys=True,separators=(',',':'),allow_nan=False):
        digest.update(chunk.encode())
    return digest.hexdigest()


def file_digest(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path,value,*,prefix,ensure_ascii=True):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=prefix,suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            for chunk in chunks(value,ensure_ascii=ensure_ascii,sort_keys=True,indent=2,allow_nan=False):
                stream.write(chunk)
            stream.write('\n')
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
