---
name: glm
description: Inspect ZCode Idle availability and submit explicit GLM tasks from BB.
---

Use `bb glm status` to check the ZCode idle queue and the usual OpenCode
off-peak window. From a dedicated BB worktree, run
`bb glm idle submit --prompt-file plan.md --title "Task name"` when explicitly
asked to submit an idle task. Use `--read-only` for ZCode plan mode.
The prompt file must be inside the worktree.

`bb glm offpeak schedule --prompt-file plan.md --at <ISO-8601 time>`
schedules a normal OpenCode turn and consumes Coding Plan quota. Use it when
the user asks for that mode.
