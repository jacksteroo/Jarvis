# Pepper — Auth Lifecycle and Reauthorization Plan

## Goal

Define how Pepper should handle credentials across:

- initial setup
- normal restarts
- Docker today
- the future macOS desktop app
- remote usage via Telegram
- local-only security controls like Keychain and Touch ID

The objective is simple:

- Pepper should not require unnecessary reauthorization on restart
- Pepper should remain usable remotely through Telegram
- credentials should be stored more securely than plain JSON files
- high-risk actions should still support step-up authentication

## Problem Statement

Right now Pepper stores provider credentials on disk under `~/.config/pepper/`:

- shared Google OAuth tokens in `google_token*.json`
- Yahoo IMAP credentials in `yahoo_credentials.json`

That works functionally and survives Docker restarts because the host config directory is bind-mounted into the container, but it does not give us:

- macOS-native secret protection
- a clear runtime unlock model
- a clear answer for remote Telegram access when Touch ID is involved
- a consistent "when do I need to reauthorize?" policy

## Current-State Behavior

### Docker Today

Current behavior:

- the Pepper container mounts `${HOME}/.config/pepper` into `/root/.config/pepper`
- provider credentials survive container restart and rebuild
- Google tokens auto-refresh if the refresh token remains valid
- Yahoo credentials are simply reread from disk on startup

Relevant implementation:

- [docker-compose.yml](/Users/jack/Developer/Pepper/docker-compose.yml:33)
- [subsystems/google_auth.py](/Users/jack/Developer/Pepper/subsystems/google_auth.py:87)
- [subsystems/communications/imap_client.py](/Users/jack/Developer/Pepper/subsystems/communications/imap_client.py:44)

Implication:

- normal Docker restart should not require reauth
- reauth is only needed when credentials are missing, revoked, invalid, or upgraded to a new scope model

## Design Principles

### 1. Restart should not imply reauth

If Pepper restarts, the user should not have to go through Google or Yahoo auth again unless something has actually broken.

### 2. Remote Telegram access must remain possible

If Pepper can answer Telegram messages while the user is away from the Mac, it cannot require live Touch ID on every credential read.

### 3. Local presence should protect sensitive transitions, not routine reads

Touch ID is appropriate for:

- connecting a new account
- reconnecting a revoked account
- changing security settings
- approving high-risk actions

Touch ID is not appropriate for:

- every background sync
- every unread-count fetch
- every calendar lookup triggered from Telegram

### 4. Read-only integrations deserve lower-friction runtime access than write actions

Gmail/Calendar/Yahoo reads are lower risk than future actions like:

- sending email
- modifying calendar events
- changing account bindings

### 5. We need explicit auth state, not implicit failure handling

Pepper should expose clear auth states instead of making users infer them from runtime errors.

## Target Runtime Model

## Credential Storage

### Short term: current file-based model remains supported

This keeps Docker and existing setups working.

### Medium term: migrate secrets into macOS Keychain

Store in Keychain:

- Google refresh tokens
- Yahoo app passwords
- API keys entered through the app

Store on disk:

- non-secret account metadata
- labels
- auth status cache
- timestamps and diagnostics

## Runtime Access Model

Split runtime access into three levels.

### Level 1 — Always-Available Background Read

Applies to:

- Gmail read-only access
- Calendar read-only access
- Yahoo read-only access
- scheduled briefs
- normal Telegram queries

Behavior:

- once connected locally, Pepper can read the required Keychain items without prompting every time
- after app restart, backend restart, or normal login session restart, Pepper resumes access automatically

This is the only model that preserves remote Telegram usability.

### Level 2 — Time-Limited Local Unlock

Applies to:

- opening account details
- running diagnostics that reveal provider/account internals
- reconnecting existing accounts

Behavior:

- user unlocks Pepper locally with Touch ID
- unlock window lasts for a bounded time, for example 8 hours
- unlock expiration does not break basic background reads if those are already allowed

This level is optional for v1, but useful for local administrative actions.

### Level 3 — Step-Up Authentication for High-Risk Actions

Applies to future capabilities like:

- send email
- create or modify calendar events
- remove an account
- export secrets or diagnostic bundles containing sensitive auth material

Behavior:

- requires local approval
- preferably Touch ID
- never auto-approved from Telegram alone

## Restart and Reauthorization Policy

This section is the core policy.

### Case A — Restarting Docker or the Pepper backend

Expected behavior:

- no provider reauth
- Pepper reloads credentials from mounted storage or Keychain bridge
- Google tokens refresh silently if possible

### Case B — Restarting the macOS app

Expected behavior:

- no provider reauth
- Pepper relaunches backend and reconnects to Keychain-backed credentials

### Case C — Rebooting the Mac

Expected behavior:

- no provider reauth
- but Pepper may need the user to log into macOS once before the login Keychain is available

This is not "Google/Yahoo reauth." It is local device session availability.

### Case D — Credential revoked or refresh fails

Expected behavior:

- Pepper marks the account as `needs_reauth`
- background reads for that provider stop gracefully
- Telegram responses explain the provider is disconnected
- reconnect must be initiated locally

### Case E — Scope change or storage migration

Expected behavior:

- Pepper marks the account as `upgrade_required`
- user performs a one-time local reauth
- after upgrade, normal restart behavior resumes

## Auth State Machine

Each connected account should expose one explicit state:

- `connected`
- `connected_degraded`
- `needs_local_login`
- `needs_reauth`
- `upgrade_required`
- `missing_credentials`

Definitions:

- `connected`: credentials available and healthy
- `connected_degraded`: credentials exist but provider access is failing intermittently
- `needs_local_login`: the Mac session or Keychain is unavailable
- `needs_reauth`: provider token/app password no longer works
- `upgrade_required`: local migration needed after auth model change
- `missing_credentials`: no stored secret exists

## Telegram Behavior Plan

Telegram is the hardest constraint because it represents remote access without local presence.

### Rule 1

Telegram-originated read-only requests must work without live Touch ID.

Otherwise the feature is unusable whenever the user is away from the machine.

### Rule 2

Telegram-originated privileged actions must not bypass local security.

For example:

- "send this email"
- "connect my account"
- "change security settings"

should require local approval, not just Telegram identity.

### Rule 3

Telegram responses should report auth state clearly.

Example behaviors:

- `connected`: "I checked your inbox."
- `needs_reauth`: "Your Google account needs to be reconnected locally in Pepper."
- `needs_local_login`: "Pepper needs the Mac user session unlocked before it can access this integration."

## Implementation Plan

### Phase 1 — Auth State and Health Model

Deliver:

- a shared auth-state representation for Google and Yahoo
- account health checks on startup
- explicit status mapping instead of generic auth errors

Implementation notes:

- add a small auth status layer above provider clients
- expose account status through API and desktop settings
- make Telegram surface user-friendly status text

Success criteria:

- Pepper can distinguish missing credentials from revoked credentials from local-unlock problems

### Phase 2 — Restart-Safe Auth UX

Deliver:

- documented restart behavior
- startup checks that avoid triggering browser auth unexpectedly
- reconnect flow separated from normal restart flow

Implementation notes:

- do not auto-open browser auth during background startup
- if credentials are broken, surface `needs_reauth` rather than launching consent silently

Success criteria:

- restart never surprises the user with a browser auth prompt

### Phase 3 — Keychain Migration

Deliver:

- import existing file-based Google and Yahoo secrets into Keychain
- continue reading legacy file storage during migration window
- store non-secret metadata separately on disk

Implementation notes:

- one-time migration on first desktop launch
- maintain a rollback-safe export path
- verify imported credentials before deleting or ignoring legacy files

Success criteria:

- existing users can upgrade without re-entering every credential manually

### Phase 4 — macOS Session and Touch ID Policy

Deliver:

- clear distinction between Keychain availability and provider auth validity
- optional Touch ID gate for local admin actions
- no Touch ID requirement for read-only Telegram queries

Implementation notes:

- use local biometric prompts only for setup/reconnect/privileged actions
- do not use Touch ID as a hard dependency for routine background reads

Success criteria:

- Telegram remains useful remotely
- local security posture improves for sensitive actions

### Phase 5 — Auth Status UI

Deliver:

- settings page or native panel showing every connected integration
- per-account states and remediation actions
- last successful refresh / last failure timestamps

Suggested actions per state:

- `connected` → no action needed
- `connected_degraded` → retry / diagnostics
- `needs_local_login` → sign into Mac locally
- `needs_reauth` → reconnect account
- `upgrade_required` → upgrade credentials
- `missing_credentials` → connect account

Success criteria:

- users no longer have to guess why email/calendar stopped working

### Phase 6 — Telegram Step-Up Rules for Future Write Actions

Deliver:

- policy for local approval on high-risk actions
- placeholder enforcement layer before write-capable email/calendar tools are introduced

Success criteria:

- future write actions do not inherit the low-friction read-only model by accident

## Technical Recommendations

### Recommendation 1 — Do not use Google Authenticator as Pepper's runtime gate

Google Authenticator is appropriate as part of provider MFA during Google/Yahoo login, but not as the local runtime control for Pepper.

Reason:

- Pepper needs unattended background access for Telegram and scheduled work
- TOTP prompts are incompatible with that model

### Recommendation 2 — Use Keychain for storage, not per-request user presence

Keychain should protect secrets at rest.
Touch ID should protect privileged local transitions.

### Recommendation 3 — Disable surprise interactive auth during startup

Startup and restart paths should never auto-launch consent windows as a side effect of a background task.

Instead:

- mark account unhealthy
- expose status
- let the user reconnect intentionally

### Recommendation 4 — Keep a migration bridge from file-based storage

We should support:

- old Google token files
- old Yahoo JSON files

during the migration period so existing users are not broken by upgrade.

## Concrete First Milestone

The first milestone should be:

"Add explicit auth states and restart-safe behavior before moving secrets to Keychain."

That gets us:

- better user-facing clarity immediately
- a stable policy for when reauth should happen
- a clean foundation for the desktop Keychain migration

## Deliverables Checklist

- auth lifecycle design documented
- reauth policy documented
- restart policy documented
- Telegram remote-access policy documented
- auth state machine defined
- Keychain migration path defined
- Touch ID step-up policy defined
