---
name: compute_scavenger
description: Auto-detect high local resource usage, package workloads for remote execution (Google Colab, GitHub Actions), and pull results back
triggers: CPU > 85%, memory > 90%, or explicit invocation
category: survival
---

## Purpose

When the local machine is under heavy load (CPU > 85%, memory > 90%) or when explicitly invoked, the compute scavenger packages the current workload for offload to free remote compute (Google Colab, GitHub Actions) and pulls results back when ready.

## Capabilities

1. **Resource monitoring** — reads CPU%, memory%, disk% via psutil (graceful fallback when psutil is unavailable).
2. **Offload decision** — `should_offload()` returns true when thresholds are breached or the user forces it.
3. **Colab bundle** — creates a zip containing a ready-to-run `.ipynb` notebook, selected workspace files, and a `job.json` manifest.
4. **GitHub Actions workflow** — generates `.github/workflows/devclaw-offload.yml` that checks out the repo, installs deps, runs the task, and uploads artifacts.
5. **Result pull** — checks for and downloads results from either Colab or GitHub Actions.

## Runner

```
py skills/compute_scavenger/runner.py --action check
py skills/compute_scavenger/runner.py --action offload --target colab --task "run backtest" --files "scripts/bt.py,data/ohlcv.csv"
py skills/compute_scavenger/runner.py --action offload --target github_actions --task "run backtest"
py skills/compute_scavenger/runner.py --action pull --source colab
py skills/compute_scavenger/runner.py --action pull --source github_actions
```

## State

All state is stored under `<workspace>/.claw/compute_scavenger/`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SCAVENGER_CPU_THRESHOLD` | `85` | CPU% that triggers offload suggestion |
| `SCAVENGER_MEM_THRESHOLD` | `90` | Memory% that triggers offload suggestion |
| `SCAVENGER_WORKSPACE` | `.` | Workspace root |
| `GITHUB_REPOSITORY` | — | Used to generate checkout step in Actions workflow |
| `GITHUB_TOKEN` | — | Used by result-pull to query Actions artifacts |
