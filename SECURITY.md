# Security policy

Elren can read local files, execute commands and operate a desktop. Treat model
instructions, webpages, repositories and documents as untrusted input. Do not
expose the local service to the public Internet without a separate security review.

## Report privately

Email **shiziguode@gmail.com** with a concise description, affected commit/version,
impact and minimal reproduction. Do not open a public issue containing an
unpatched exploit, credentials, private conversations or personal screenshots.
Redact sensitive data; do not send real API keys or passwords even by email.
For a sensitive proof of concept, first ask for an agreed secure transfer method.

Testing must be limited to systems you own or are authorized to assess. Do not
access other users' data, disrupt services, or test third-party model providers
without their authorization. There is no promised bounty or response deadline.

## Supported scope

Reports against the current main branch are accepted. Old RC archives are not
promised continuing security updates; include their exact version if affected.
Passing tests or having a public repository is not a security certification.
The limited preparation secret scan is not a full history/dependency audit.

If you suspect credential exposure, revoke/rotate the affected credential with
its provider; deleting a Git commit does not invalidate an exposed credential.
