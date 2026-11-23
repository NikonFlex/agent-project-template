# GLM Idle for BB

`bb glm status` and the **GLM Idle** sidebar footer show whether ZCode can
issue a new idle ticket, its next reported availability, and recent task
states. The footer also shows the published OpenCode off-peak window.
Ordinary GLM usage remains visible in the existing Limits card.

Run these commands from a terminal in a dedicated BB worktree.

```sh
bb glm idle submit --prompt-file plan.md --title "Apply the approved plan"
bb glm idle submit --prompt-file review.md --title "Review the change" --read-only
bb glm offpeak schedule --prompt-file tests.md --at 2026-11-24T23:00:00Z --title "Nightly test"
```

Idle tasks run in the invoking worktree on ZCode Desktop. Keep the worktree
available until the task finishes. The command returns the ZCode task ID and
a reference to the invoking BB thread. Use `bb glm status` or ZCode Desktop
to follow execution. For coding, the native ZCode approval mode applies.
`--read-only` selects ZCode plan mode.

An administrator can also approve isolated worktrees by name with
`desktopNamedWorktrees`. Each name maps to an absolute `path` on the BB
server and a `readOnly` boolean. `--workspace <name>` uses the named worktree
for both the prompt file and the task. The worktree must be accessible to
the ZCode service user and have write permission for coding tasks.

Scheduled OpenCode tasks consume ordinary Coding Plan quota and get a new
BB worktree from the project's default branch. The command checks the
requested dispatch time against the published window. Repeated jobs use
`bb automation create` with `--provider acp-opencode`,
`--model zai-coding-plan/glm-5.3-flash`, and a cron schedule in a named time
zone. Delayed starts and ongoing execution can cross a rate boundary.

## Managed ZCode Desktop connection

Build with `npm ci`, `npm run check`, and `bb plugin build .`.
Install with `bb plugin install --yes .`.

The [Linux service](deploy/bb-glm-idle-desktop.service) runs ZCode Desktop 4.0
under the dedicated `bb-glm-idle` user with Chromium sandboxing. It requires
Node.js, Xvfb, and ZCode under `/opt/bb-glm-idle/`. Prepare an authorized Desktop
profile under `/var/lib/bb-glm-idle/profile` with mode `0700` and service-user
ownership. Keep the profile and its credential store local to the service.

The [bridge](deploy/desktop-bridge.mjs) uses Chromium's inherited debug pipes
and a mode `0600` Unix socket under `/run/bb-glm-idle/`. Configure
`desktopSocket` to that path and `desktopHostId` to its BB host ID.
The native renderer handles OAuth. A change in ZCode's internal renderer API
can make the queue unavailable.

To approve a BB worktree, copy the [mount example](deploy/worktree.conf.example)
to the service's systemd drop-in directory and use the exact worktree path.
Set `desktopWorktreeId` to its BB environment ID and `desktopWorktreePath`
to the path inside the service. With `desktopWorktreeReadOnly` enabled,
submissions require `--read-only`. A writable mount requires separate write
permissions for the service user on that worktree. The managed service does
not need a TCP debugger, so leave `desktopDebugPort` unset.

Check `bb glm status` after `systemctl restart bb-glm-idle-desktop.service`.
To stop the deployment and disable start on boot, run
`systemctl disable --now bb-glm-idle-desktop.service`.

If ZCode doesn't confirm a submission, inspect the queue before retrying.
When idle tickets are unavailable, the idle command exits with the
provider's reported reason and next availability, when supplied.
