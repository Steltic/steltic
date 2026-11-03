import copy
import hashlib
import json

from steltic.context_history import archive_tool_results


def output(call, content):
    return dict(role="tool", name="run_python", tool_call_id=call, content=content)


def test_archive_preserves_instructions_calls_recent_outputs_and_exact_retrieval(tmp_path):
    old=json.dumps(dict(stdout="numerical output \u03a3\n"*500, returncode=7),ensure_ascii=False)
    messages=[dict(role="user",content="retain the architectural requirements "*500),
              dict(role="assistant",content=None,tool_calls=[dict(id="first",function=dict(name="run_python",arguments='{"code":"print(1)"}'))]),
              output("first",old),output("small","short result"),output("recent","new"*3000)]
    before=copy.deepcopy(messages)
    assert archive_tool_results(messages,tmp_path,keep=1)==1
    assert messages[:2]==before[:2] and messages[3:]==before[3:]
    assert {k:v for k,v in messages[2].items() if k!="content"}=={k:v for k,v in before[2].items() if k!="content"}
    pointer=json.loads(messages[2]["content"])
    restored=(tmp_path/pointer["path"]).read_bytes()
    assert restored==old.encode("utf-8")
    assert hashlib.sha256(restored).hexdigest()==pointer["sha256"]
    assert pointer["returncode"]==7
    saved=copy.deepcopy(messages)
    assert archive_tool_results(messages,tmp_path,keep=1)==0
    assert messages==saved
    assert len(list((tmp_path/"context_tool_results").iterdir()))==1


def test_failed_archive_keeps_the_original_context(tmp_path):
    # A user-created file blocks the archive directory; no output may be lost.
    (tmp_path/"context_tool_results").write_text("existing file")
    messages=[output("old","x"*5000),output("recent","new")]
    before=copy.deepcopy(messages)
    assert archive_tool_results(messages,tmp_path,keep=1)==0
    assert messages==before


def test_changed_archive_is_not_overwritten_or_referenced(tmp_path):
    text="original"*800
    digest=hashlib.sha256(text.encode()).hexdigest()
    folder=tmp_path/"context_tool_results";folder.mkdir()
    path=folder/(digest+".txt");path.write_text("changed by user")
    messages=[output("old",text),output("recent","new")]
    before=copy.deepcopy(messages)
    assert archive_tool_results(messages,tmp_path,keep=1)==0
    assert messages==before and path.read_text()=="changed by user"


def test_existing_rag_pruning_and_nontext_messages_are_left_alone(tmp_path):
    messages=[dict(role="tool",name="search_engineering_standards",tool_call_id="rag",content="reference"*1000),
              output("nontext",{"value":3}),output("last","recent")]
    before=copy.deepcopy(messages)
    assert archive_tool_results(messages,tmp_path,keep=1)==0
    assert messages==before

