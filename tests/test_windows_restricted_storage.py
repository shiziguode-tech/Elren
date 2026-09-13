"""Real thread-token access checks; not a whole standard-user desktop test."""
from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted-token semantics")
def test_upload_and_chat_storage_with_administrator_sid_disabled(tmp_path):
    import win32api
    import win32con
    import win32security

    from deepdesk.models import AgentTask
    from deepdesk.task_store import TaskStore
    from deepdesk.upload_storage import save_upload

    admin = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_DUPLICATE | win32security.TOKEN_QUERY,
    )
    restricted = None
    impersonated = False
    try:
        restricted = win32security.CreateRestrictedToken(token, 1, [(admin, 0)], [], [])
        # pytest's elevated TEMP parent is admin-owned on this host. Model an
        # ordinary user's writable application-data folder explicitly, granting
        # only this new QA directory to the unchanged current user SID.
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        user_dacl = win32security.ACL()
        user_dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
            win32con.GENERIC_ALL, user,
        )
        win32security.SetNamedSecurityInfo(
            str(tmp_path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, user_dacl, None,
        )
        # New synthetic fixture only: the restricted token must be denied here.
        guarded = tmp_path / "admin-only.txt"
        guarded.write_bytes(b"synthetic guarded file")
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, admin)
        win32security.SetNamedSecurityInfo(
            str(guarded), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None,
        )
        win32security.ImpersonateLoggedOnUser(restricted)
        impersonated = True
        assert not win32security.CheckTokenMembership(None, admin)
        with pytest.raises(PermissionError):
            guarded.read_bytes()
        target = tmp_path / "普通权限 中文 & files" / "attachment.txt"
        save_upload(target, b"complete attachment")
        assert target.read_bytes() == b"complete attachment"
        database = tmp_path / "普通权限 SQLite" / "tasks.db"
        store = TaskStore(database)
        task = AgentTask(prompt="Synthetic restricted-token conversation")
        store.save(task)
        assert TaskStore(database).get(task.id).prompt == task.prompt
    finally:
        if impersonated:
            win32security.RevertToSelf()
        if restricted is not None:
            restricted.Close()
        token.Close()
