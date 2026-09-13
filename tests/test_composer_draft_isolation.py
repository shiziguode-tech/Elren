"""Conversation-owned drafts, including sends that finish after navigation."""

import re
from pathlib import Path

from test_frontend_async_workflows import PRELUDE, run_js

ROOT = Path(__file__).resolve().parents[1]
DRAFT_PRELUDE = PRELUDE.replace("let composerDraftScope = 'a';", "let composerDraftScope = null;")
DRAFT_PRELUDE = DRAFT_PRELUDE.replace("const clearSubmittedDraft = () => {};", "")
FUNCTIONS = ("switchComposerDraft", "clearSubmittedDraft", "clearSubmittedComposer")


def test_new_and_existing_chats_keep_separate_text_and_attachments():
    run_js("""
      $('#prompt').value='new draft'; pendingAttachments=[{path:'new.png'}];
      switchComposerDraft('a');
      assert.equal($('#prompt').value,''); assert.deepEqual(pendingAttachments,[]);
      $('#prompt').value='draft A'; pendingAttachments=[{path:'a.pdf'}];
      switchComposerDraft('b');
      assert.equal($('#prompt').value,''); assert.deepEqual(pendingAttachments,[]);
      $('#prompt').value='draft B';
      switchComposerDraft('a');
      assert.equal($('#prompt').value,'draft A'); assert.equal(pendingAttachments[0].path,'a.pdf');
      switchComposerDraft(null);
      assert.equal($('#prompt').value,'new draft'); assert.equal(pendingAttachments[0].path,'new.png');
      switchComposerDraft('b'); assert.equal($('#prompt').value,'draft B');
    """, *FUNCTIONS, prelude=DRAFT_PRELUDE)


def test_same_chat_selection_does_not_restore_an_older_snapshot():
    run_js("""
      switchComposerDraft('a'); $('#prompt').value='edited';
      switchComposerDraft('a'); assert.equal($('#prompt').value,'edited');
      switchComposerDraft('b'); switchComposerDraft('a');
      $('#prompt').value=''; pendingAttachments=[];
      switchComposerDraft('b'); switchComposerDraft('a');
      assert.equal($('#prompt').value,''); assert(!composerDrafts.has('a'));
    """, *FUNCTIONS, prelude=DRAFT_PRELUDE)


def test_generated_continuation_marker_does_not_bleed_into_another_chat():
    run_js("""
      switchComposerDraft('a'); $('#prompt').value='continue';
      $('#prompt').dataset.systemDraft='task-continuation';
      switchComposerDraft('b'); assert(!$('#prompt').dataset.systemDraft);
      $('#prompt').value='my draft'; switchComposerDraft('a');
      assert.equal($('#prompt').dataset.systemDraft,'task-continuation');
      switchComposerDraft('b'); assert.equal($('#prompt').value,'my draft');
      assert(!$('#prompt').dataset.systemDraft);
    """, *FUNCTIONS, prelude=DRAFT_PRELUDE)


def test_late_success_only_clears_submitted_parts_of_the_originating_draft():
    run_js("""
      switchComposerDraft('a'); $('#prompt').value='submitted';
      pendingAttachments=[{path:'sent.pdf'},{path:'later.pdf'}];
      switchComposerDraft('b'); $('#prompt').value='B untouched';
      clearSubmittedDraft('a','submitted',[{path:'sent.pdf'}]);
      assert.equal($('#prompt').value,'B untouched');
      switchComposerDraft('a'); assert.equal($('#prompt').value,'');
      assert.deepEqual(pendingAttachments,[{path:'later.pdf'}]);
      $('#prompt').value='new typing'; switchComposerDraft('b');
      clearSubmittedDraft('a','submitted',[]); switchComposerDraft('a');
      assert.equal($('#prompt').value,'new typing');
    """, *FUNCTIONS, prelude=DRAFT_PRELUDE)


def test_successful_followup_after_navigation_does_not_restore_sent_text():
    run_js("""
      switchComposerDraft('a'); $('#prompt').value='message A';
      const request=sendRunningMessage('message A');
      switchComposerDraft('b'); taskId='b'; taskViewGeneration++;
      $('#prompt').value='B draft';
      resolveRequest({id:'a',status:'running'}); await request;
      assert.equal($('#prompt').value,'B draft');
      switchComposerDraft('a'); assert.equal($('#prompt').value,'');
      assert.equal(startRequestPending,false);
    """, *FUNCTIONS, "sendRunningMessage", "isCurrentTaskRequest", prelude=DRAFT_PRELUDE)


def test_failed_followup_preserves_originating_draft():
    run_js("""
      switchComposerDraft('a'); $('#prompt').value='retry me';
      let rejectRequest; api=()=>new Promise((resolve,reject)=>{rejectRequest=reject;});
      const request=sendRunningMessage('retry me');
      switchComposerDraft('b'); taskId='b'; taskViewGeneration++;
      rejectRequest(Error('offline')); await request;
      switchComposerDraft('a'); assert.equal($('#prompt').value,'retry me');
    """, *FUNCTIONS, "sendRunningMessage", "isCurrentTaskRequest", prelude=DRAFT_PRELUDE)


def test_markdown_emphasis_is_inline_and_console_height_is_stable():
    css = (ROOT / 'deepdesk/static/editorial-ui.css').read_text('utf-8')
    emphasis = re.search(r'\.event-card \.markdown strong\s*\{([^}]+)', css).group(1)
    assert 'display: inline;' in emphasis
    assert 'font-size: inherit;' in emphasis
    assert 'margin: 0;' in emphasis
    assert 'height: min(860px, calc(100dvh - 40px));' in css
    toast = re.search(r'\.workspace-dialog \.toast-region\s*\{([^}]+)', css).group(1)
    assert 'bottom: 80px;' in toast
