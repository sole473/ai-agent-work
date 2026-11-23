---
name: ace-step-check
description: Diagnose whether the local ACE-Step API is running, whether Music Agent can reach it, and whether a submitted generation task is progressing. Use for ACE-Step generation stalls or requests for a one-command status check.
---

# ACE-Step generation check

For this repository, ACE-Step 1.5 normally runs on the Windows host at port 8001. Music Agent's Docker `worker` calls it at `http://host.docker.internal:8001`; the worker submits `/release_task` and polls `/query_result` every three seconds. The web UI polls Music Agent every three seconds, so successful small browser requests alone do not prove ACE-Step received the job.

When the user asks for a one-click or one-command check, run `scripts/check.ps1` from the repository root in Windows PowerShell. Explain each command in Chinese and say it is run from `B:\music-agent`, following the repository's `AGENTS.md`. This read-only check reports host port and `/health`, Docker availability and Compose service state, recent worker logs, and optionally an ACE-Step task result when given its task ID. It never prints environment files or API keys.

Usage (Windows PowerShell, in `B:\music-agent`):

```powershell
# 运行只读诊断；如有 ACE-Step 返回的 task_id，可传入以查询实时状态。
.\.agents\skills\ace-step-check\scripts\check.ps1 -TaskId '替换为实际task_id'
```

Without a task ID, omit the parameter. Do not invent a task ID. Interpret results in layers: port/health confirms API responsiveness, container status and worker logs show Music Agent delivery attempts, and `/query_result` with a real task ID shows ACE-Step queue/progress/result. A healthy endpoint does not prove model weights are loaded. If no task ID is available, explain that current code does not expose it in the browser UI; do not claim the job reached ACE-Step without worker/API evidence. Do not start, stop, or resubmit services/jobs as part of this diagnostic unless the user separately asks.

If Docker access is denied, report that worker-side delivery could not be verified, but still report host-side API checks. Never ask for or display secrets; authentication is taken only from the current PowerShell process environment when present.
