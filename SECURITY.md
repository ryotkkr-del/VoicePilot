# Security policy

## Supported state

VoicePilot is an experimental Windows desktop-automation project. Only the
current `main` branch and the latest **owner-signed** release are candidates
for security fixes. An unsigned ZIP, a CI artifact, or an installation using a
legacy bootstrap is not a trusted production distribution.

## Reporting a vulnerability

Do **not** open a public Issue, pull request, discussion, or commit for a
potential vulnerability. Do not include API keys, pairing secrets, passwords,
personal documents, screenshots, or audio in any report.

Use GitHub's private security-advisory reporting page for this repository:

<https://github.com/ryotkkr-del/VoicePilot/security/advisories/new>

If private reporting is not available, contact the repository owner through
their GitHub profile and request a private reporting channel before sending
technical details. A public issue is appropriate only after the maintainer has
confirmed that disclosure is safe.

Please include the affected version or commit, Windows/Chrome/Python versions,
minimal non-sensitive reproduction steps, expected and observed behavior, and
any suggested mitigation. Do not attempt destructive actions, privilege
escalation, or access to other users' data while validating a report.

## Response and disclosure

The project currently has no service-level response-time commitment. The
maintainer will assess reproducible reports, prepare a fix and regression test,
and publish the remediation and affected-version guidance when a safe release
is available. Coordinated disclosure is preferred: do not publish exploit
details until a fix or mitigation is available, unless the maintainer agrees
otherwise.

## Security boundaries

- High-impact operations, authentication screens, payments, deletion, and
  unattended handling of important data are outside the supported automation
  scope.
- Cloud screen/text features require explicit user consent and can still expose
  visible information. Use local mode for sensitive work.
- Updates are trusted only when the installed bootstrap has a pinned developer
  key and verifies the detached Ed25519 signature. Never install an unsigned
  CI artifact as an update.

See `docs/STRATEGY_FREE_PERFORMANCE_SAFETY.md` and
`docs/OWNER_SIGNED_RELEASE.md` for the current safety gates and signed-release
procedure.
