import pytest
from test_frontend_async_workflows import PRELUDE, run_js

HISTORY_PRELUDE = PRELUDE + """
let historyAppendPending = false;
const historyMarkup = tasks => tasks.map(task=>task.id).join(',');
const writes = [];
let disabled = false;
Object.defineProperty($('#historyMore'),'disabled',{
  get:()=>disabled,set:value=>{disabled=value;writes.push(value);},
});
const page = {tasks:[{id:'old',title:'Task',status:'completed',updated_at:'1'}],
              total:100,has_more:true};
historyFirstPageSignature = historyResultSignature(page);
$('#taskHistory').insertAdjacentHTML = (position, markup) => {
  assert.equal(position,'beforeend'); $('#taskHistory').innerHTML += markup;
};
"""


@pytest.mark.parametrize("outcome", ["unchanged", "changed", "failure"])
def test_background_history_refresh_never_toggles_load_more(outcome):
    run_js("""
      let rejectRequest;
      api = () => new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});
      const request = loadTaskHistory({background:true});
      assert.equal($('#historyMore').disabled,false);
      assert.deepEqual(writes,[]);
    """ + {
        "unchanged": "resolveRequest(page);",
        "changed": "resolveRequest({...page,tasks:[{...page.tasks[0],updated_at:'2'}]});",
        "failure": "rejectRequest(Error('temporary offline'));",
    }[outcome] + """
      await request;
      assert.deepEqual(writes,[]);
      assert.equal(historyLoading,false);
    """, "loadTaskHistory", "historyResultSignature", prelude=HISTORY_PRELUDE)


def test_unchanged_background_refresh_preserves_older_pages_and_scroll():
    run_js("""
      historyOffset=100; $('#taskHistory').scrollTop=420;
      const before=$('#taskHistory').innerHTML;
      const request=loadTaskHistory({background:true});
      resolveRequest(page); await request;
      assert.equal(historyOffset,100);
      assert.equal($('#taskHistory').scrollTop,420);
      assert.equal($('#taskHistory').innerHTML,before);
      assert.deepEqual(writes,[]);
    """, "loadTaskHistory", "historyResultSignature", prelude=HISTORY_PRELUDE)


def test_load_more_click_during_background_refresh_is_queued_once():
    run_js("""
      const requests=[];
      api = url=>new Promise(resolve=>requests.push({url,resolve}));
      const refresh=loadTaskHistory({background:true});
      const click=loadTaskHistory({append:true});
      const repeatedClick=loadTaskHistory({append:true});
      assert.equal($('#historyMore').disabled,true);
      assert.equal(requests.length,1);
      requests[0].resolve(page); await refresh;
      await Promise.resolve();
      assert.equal(requests.length,2);
      assert(requests[1].url.includes('offset=50'));
      requests[1].resolve({tasks:[{id:'older',updated_at:'1',status:'completed'}],
                           total:100,has_more:true});
      await Promise.all([click,repeatedClick]);
      assert.equal(requests.length,2);
      assert.equal(historyOffset,51);
      assert.equal($('#historyMore').disabled,false);
      assert.equal(historyAppendPending,false);
      assert($('#taskHistory').innerHTML.endsWith('older'));
    """, "loadTaskHistory", "historyResultSignature", prelude=HISTORY_PRELUDE)


def test_queued_load_more_is_cancelled_when_filter_changes():
    run_js("""
      const requests=[];
      api=url=>new Promise(resolve=>requests.push({url,resolve}));
      const refresh=loadTaskHistory({background:true});
      const click=loadTaskHistory({append:true});
      $('#historySearch').value='new filter';
      requests[0].resolve(page); await refresh;
      await Promise.resolve();
      assert.equal(requests.length,2);
      assert(requests[1].url.includes('query=new+filter'));
      assert(requests[1].url.includes('offset=0'));
      requests[1].resolve({tasks:[{id:'filtered',updated_at:'1',status:'completed'}],
                           total:100,has_more:true});
      await click;
      assert.equal(requests.length,2);
      assert.equal(historyOffset,1);
      assert.equal(historyAppendPending,false);
      assert.equal($('#historyMore').disabled,false);
    """, "loadTaskHistory", "historyResultSignature", prelude=HISTORY_PRELUDE)


@pytest.mark.parametrize("fails", [False, True])
def test_explicit_load_more_disables_until_request_settles(fails):
    run_js("""
      let rejectRequest;
      api=()=>new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});
      const request=loadTaskHistory({append:true});
      assert.equal($('#historyMore').disabled,true);
    """ + ("rejectRequest(Error('offline'));" if fails else
             "resolveRequest({tasks:[],total:50,has_more:false});") + """
      await request;
      assert.deepEqual(writes,[true,false]);
      assert.equal(historyLoading,false);
    """, "loadTaskHistory", "historyResultSignature", prelude=HISTORY_PRELUDE)
