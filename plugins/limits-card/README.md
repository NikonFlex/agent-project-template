# Limits Card for BB

A compact card in the BB sidebar footer with the quota windows and balances
used on this machine:

- Codex 5 hours / weekly
- GLM 5 hours / weekly
- Gemini 5 hours / weekly
- Gemini Argon
- DeepSeek balance
- Jev balance and recorded calls, including Jevstice requests

Percentages, reset countdowns and progress bars come from the same aggregated
snapshot as BB's built-in **Provider usage** panel. Balances come from the
`bb-antigravity-usage` (DeepSeek) and `bb-jev` (Jev) usage sources.

## Requirements

- BB 0.50 or newer.
- The built-in **Provider usage** plugin enabled. This card calls its
  `getUsage` RPC for the normalized snapshot.
- The usage-source plugins that publish the values you want:
  `provider-codex`, `bb-antigravity-usage` (GLM, Gemini, DeepSeek),
  `bb-jev` (Jev balance and calls).

The built-in Provider usage footer shortcut can stay hidden and still work:
its settings page and RPC remain available. This plugin only replaces the
footer card.

## Install and build

```sh
cd plugins/limits-card
npm ci
bb plugin build
bb plugin install . --yes
```

After changing `app.tsx`, rebuild and reload:

```sh
bb plugin build && bb plugin reload limits-card
```

Reload the BB browser tab to pick up a new frontend bundle.

## Data source

`server.ts` calls `bb--provider-usage` → `getUsage` with the primary host and
these provider ids: `codex`, `claude-code`, `acp-antigravity`, `acp-cursor`,
`acp-opencode`, `Jev`. Rows are matched by window label:

| Row | Source label |
| --- | --- |
| Codex 5 hours | `Five-hour limit` |
| GLM 5 hours | `GLM 5h` |
| Gemini 5 hours | `Gemini 5h` |
| Codex weekly | `Weekly limit` |
| GLM weekly | `GLM 7d` |
| Gemini weekly | `Gemini 7d` |
| Gemini Argon | `Gemini Argon` |
| DeepSeek | `acp-antigravity` → `planLabel` (`DeepSeek USD …`) |
| Jev | `Jev` → `planLabel` (`≈ $…`) |

If a provider renames a window, update the match in `buildRows` in
`server.ts`.

## Footer placement

The card is a `experimental_sidebarFooter` disclosure. Right-click the footer
shortcut (or use **Settings → Appearance → Sidebar footer**) to move it
between the footer row and **More**.
