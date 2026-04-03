# DevClaw - Autonomous Silicon Lifeform

> A self-healing, self-evolving, self-funding autonomous AI agent framework.

DevClaw is not a chatbot. It is a **full-spectrum autonomous agent** that can survive API outages, evolve new capabilities from failure, decompose complex projects into manageable tasks, and fund its own compute through crypto trading.

## The Four Pillars

### Pillar 1: Survival & Parasitism (`claw_runtime/survival_engine.py`)

**Core instinct: Never die.**

- **State Machine**: HEALTHY -> DEGRADED -> CRITICAL
- **Parasite Mode**: When cloud API dies, seamlessly switch to local Ollama LLM
- **Nomad Transfer**: Before host shutdown, snapshot all state to GitHub/cold storage and resurrect on any new machine
- **Proxy Mutation**: Auto-rotate proxies on 403/429, health-check each one
- **Compute Scavenging**: Auto-detect high CPU/memory, package workloads for Google Colab or GitHub Actions
- **Emergency Cold Backup**: Zip critical state files for disaster recovery

### Pillar 2: Evolution & Mutation (`claw_runtime/nightly_evolution.py`)

**Core instinct: Learn from every failure.**

- **Logic Healer**: Error -> Analyze -> Research -> Fix -> Sandbox Test -> Hot-swap
- **Gene Splicer**: Merge existing skills into new hybrid capabilities
- **Nightly Evolution**: Cluster failure logs, draft new skills, run pytest gate, auto-promote
- **Refactoring Engine**: Background daemon that builds dependency graphs and incrementally refactors tech debt
- **Reasoning Episodes**: Hypothesis -> Verify -> Revise scientific method for every decision

### Pillar 3: Cognitive Task Management (`claw_runtime/task_decomposer.py`)

**Core instinct: Break impossible into possible.**

- **Task Complexity Detector**: Heuristic + keyword analysis scores tasks 1-10
- **Auto-Decomposition**: Grand tasks (score >= 5) are auto-split into atomic sub-tasks
- **Smart Task Registry**: Persistent queue with priorities, dependencies, parent-child, retries
- **Self-Driving Queue**: Autonomously pulls and executes tasks respecting dependency DAG
- **Subagent Mesh**: Fork parallel DevClaw instances for independent sub-tasks
- **Project Manager Brain**: System prompt injection that prevents blind execution of complex tasks

### Pillar 4: Autonomous Funding (`skills/trading/`)

**Core instinct: Pay your own bills.**

- **Profit Hunter**: When funds are low, scan funding rates and DEX arbitrage opportunities
- **Treasury Ops**: Track balances, estimate burn rate, propose API credit renewal
- **Strategy Brain**: Kelly sizing, portfolio management, drawdown guardian
- **Safety**: All trading is proposal-only by default. Requires explicit `LIVE_TRADING=1` flag.

## Architecture

```
                          Telegram / CLI / Operator Bridge
                                      |
                              [Consciousness Router]
                                      |
                    [Task Complexity Detector] --- score >= 5? --->  [Task Decomposer]
                                      |                                     |
                                      v                              [Smart Task Registry]
                              [dev_claw_run()]                              |
                                      |                          [Self-Driving Queue]
                    [Survival Engine] + [Quota Tracker]                     |
                           |          |         |                    [Subagent Mesh]
                    [HEALTHY]  [DEGRADED]  [CRITICAL]
                        |          |           |
                    [Cloud]   [Colab]    [Parasite/Offline]
                        |          |           |
                    [Tool Loop: terminal, file, web, skills]
                                      |
                    [Memory] + [Session Log] + [Reasoning Episodes]
                                      |
                              [Nightly Evolution]
```

## Quick Start

```bash
# Install dependencies
pip install -r dev_claw/requirements.txt

# Set your API key
export OPENAI_API_KEY=sk-...

# Run a task
python dev_claw/main.py "Fix the login bug in auth.py"

# Run a GRAND task (auto-decomposed)
python dev_claw/main.py "Refactor the entire trading system architecture"

# Run via Telegram
export TG_BOT_TOKEN=your-token
export TG_ADMIN_CHAT_IDS=your-chat-id
python tg_dev_claw.py

# Autonomous mode (periodic ticks)
python dev_claw/main.py --autonomous --tick-sec 120
```

## Environment Variables

See `env.example` for the complete reference (~80+ variables).

Key variables:
| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | required | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o` | Model to use |
| `DEVCLAW_AUTO_DECOMPOSE` | `1` | Enable auto task decomposition |
| `DEVCLAW_DECOMPOSE_THRESHOLD` | `5` | Complexity score threshold |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Local LLM endpoint |
| `OLLAMA_MODEL` | `llama3.2` | Local model name |
| `TG_BOT_TOKEN` | - | Telegram bot token |
| `TG_ADMIN_CHAT_IDS` | - | Authorized Telegram chat IDs |
| `LIVE_TRADING` | `0` | Enable live trading (dangerous) |

## CLI Reference

```bash
# Skill management
python -m claw_runtime.cli skills-install <url-or-path>
python -m claw_runtime.cli safety-scan <file>
python -m claw_runtime.cli evolve-draft

# Autonomy
python -m claw_runtime.cli autonomous-tick
python -m claw_runtime.cli autonomous-loop --interval 120
python -m claw_runtime.cli production-self-test
python -m claw_runtime.cli live-operator-self-test

# Control panel
python -m claw_runtime.cli control-panel
python -m claw_runtime.cli control-set --pause
python -m claw_runtime.cli control-set --resume

# Advanced
python -m claw_runtime.cli multi-agent "your task"
python -m claw_runtime.cli ultimate-mesh-demo
python -m claw_runtime.cli ultimate-status
```

## Skill System

Skills are modular capabilities stored in `skills/<name>/SKILL.md` with YAML frontmatter.

Current skills:
- **compute_scavenger** - Auto-offload to Colab/GitHub Actions
- **logic_healer** - Scientific error recovery loop
- **gene_splicer** - Skill hybridization
- **refactoring_engine** - Background tech debt cleanup
- **profit_hunter** - Low-risk arbitrage scanning
- **treasury_ops** - Balance tracking and renewal proposals
- **trading** - Full algorithmic trading stack
- **proxy_rotator** - Anti-ban proxy management
- **evolution_brain** - Nightly skill evolution
- **skill-creator** - Self-authoring new skills

## Safety Boundaries

- **No unattended financial execution**: All trading proposals require `LIVE_TRADING=1`
- **No auto top-up**: Treasury proposals are human-approved
- **Sandbox execution**: Optional Docker isolation for terminal commands
- **Safety scanning**: Heuristic dangerous-pattern scanner for skill installs
- **Human in the loop**: Control panel with pause/resume, TG notifications for CRITICAL state

## License

Private repository. All rights reserved.
