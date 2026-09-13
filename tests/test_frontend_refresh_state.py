"""Late list loads and schedule submissions must not replace newer UI state."""
import pytest
from test_frontend_async_workflows import PRELUDE, run_js


@pytest.mark.parametrize("old_fails", [False, True])
def test_old_artifact_response_cannot_replace_new_list_or_start_refresh(old_fails):
    run_js("""
      const requests = [];
      api = () => new Promise((resolve,reject) => requests.push({resolve,reject}));
      const old = loadArtifacts();
      const fresh = loadArtifacts();
      requests[1].resolve({artifacts:[{name:'new.html'}]}); await fresh;
    """ + ("requests[0].reject(Error('old offline'));" if old_fails else
             "requests[0].resolve({artifacts:[{name:'old.html'}],summary_pending:true});") + """
      await old;
      assert.equal($('#artifactList').innerHTML,'new.html');
      assert.equal(artifactRefreshTimer,null);
    """, "loadArtifacts", prelude=PRELUDE + """
      let latestArtifacts = [], artifactRefreshTimer = null, artifactRequestGeneration = 0;
      const renderArtifacts = () => {$('#artifactList').innerHTML=latestArtifacts[0].name;};
    """)


@pytest.mark.parametrize("old_fails", [False, True])
def test_old_schedule_response_cannot_resurrect_deleted_schedule(old_fails):
    run_js("""
      const requests = [];
      api = () => new Promise((resolve,reject) => requests.push({resolve,reject}));
      const old = loadSchedules();
      const fresh = loadSchedules();
      requests[1].resolve({schedules:[]}); await fresh;
      const empty = $('#scheduleList').innerHTML;
    """ + ("requests[0].reject(Error('old offline'));" if old_fails else
             "requests[0].resolve({schedules:[{id:'deleted',name:'Deleted task'}]});") + """
      await old;
      assert.equal($('#scheduleList').innerHTML,empty);
      assert(empty.includes('No scheduled tasks yet.'));
    """, "loadSchedules", prelude=PRELUDE + """
      let scheduleRequestGeneration = 0;
      const scheduleDescription = () => 'Daily';
    """)


def test_schedule_save_preserves_next_draft():
    run_js("""
      $('#scheduleKind').value = 'at';
      $('#scheduleStartAt').value = '2027-01-01T09:00';
      $('#scheduleName').value = 'submitted';
      const save = createSchedule({preventDefault(){}});
      $('#scheduleName').value = 'next draft';
      resolveRequest({ok:true}); await save;
      assert.equal($('#scheduleName').value,'next draft');
      assert.equal(scheduleCreatePending,false);
    """, "createSchedule", "scheduleFormSignature", "parseScheduleDateTime", prelude=PRELUDE + """
      let scheduleCreatePending = false;
      const resetScheduleForm = () => {throw Error('Cleared the next draft');};
      const loadSchedules = async() => {};
    """)
