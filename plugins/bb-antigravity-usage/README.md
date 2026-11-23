# Antigravity Usage for BB

Adds weekly Antigravity quota windows to BB's Provider Usage panel.

## Requirements

- BB 0.43 or newer.
- An existing, working Antigravity ACP provider (`acp-antigravity`). This project does **not** install or replace an ACP provider.
- A local Antigravity login at `~/.gemini/antigravity-cli/antigravity-oauth-token`.

## Privacy and credentials

The plugin reads the local login token only to make the quota request. It does not log, display, or persist access or refresh tokens. If the access token is expired, it asks the locally installed `agy` CLI to refresh it by listing available models; this does not start an inference turn. As a fallback, an OAuth client secret may be supplied through `ANTIGRAVITY_OAUTH_CLIENT_SECRET`; the plugin contains no embedded OAuth secret.

## Install

```sh
bb plugin install git:https://github.com/defacid/bb-antigravity-usage.git
```

## Optional local quota collector

Provider Usage reads Gemini and Claude/GPT quota buckets from the Antigravity
CLI status line. The local Python helper starts the configured IPv4 launcher in
a temporary terminal and never submits a prompt. It keeps only quota fields.
If the CLI provides no data, the plugin uses its existing quota-summary request.

The card header also reports the DeepSeek prepaid balance. The host reads the
active DeepSeek credential from OpenCode's database in read-only mode and uses
it only with the official DeepSeek balance endpoint. No inference is sent.

The same card shows Z.ai Personal Coding Plan's five-hour and weekly quota.
It uses the active OpenCode Coding Plan credential and Z.ai's quota endpoint.
A window without a reported reset time leaves that field blank.

Set `BB_AGY_QUOTA_HELPER` to the Python helper in your checkout and
`BB_AGY_LAUNCHER` to the local launcher if it differs from `agy`.
