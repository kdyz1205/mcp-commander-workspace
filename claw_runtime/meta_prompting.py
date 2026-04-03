"""
Genetic mutation of system prompts — A/B test engine that evolves DevClaw's
system prompt through random micro-mutations and fitness-based selection.

State stored in <workspace>/.claw/prompt_genomes.jsonl
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PromptGenome:
    """A single evolved variant of the system prompt."""

    genome_id: str
    base_prompt_hash: str
    mutations: list[str]
    fitness_score: float
    generation: int
    created_at: float
    task_success_rate: float
    avg_token_cost: float
    error_rate: float


# ---------------------------------------------------------------------------
# Mutation catalogue
# ---------------------------------------------------------------------------

_SYNONYM_PAIRS: list[tuple[str, str]] = [
    ("必须", "务必"),
    ("禁止", "严禁"),
    ("应该", "需要"),
    ("请", "请务必"),
    ("建议", "推荐"),
    ("注意", "务必注意"),
]

_EMPHASIS_MARKERS: list[str] = ["**", "!!", "⚠️", "🔴", ">>>"]

_REASONING_HINTS: list[str] = [
    "先思考再行动",
    "Step by step reasoning:",
    "Think carefully before responding.",
    "逐步分析后再给出答案",
    "Let's break this down:",
    "请先列出关键点再回答",
]

_TEMPERATURE_SWAPS: list[tuple[str, str]] = [
    ("谨慎", "大胆"),
    ("conservative", "aggressive"),
    ("carefully", "decisively"),
    ("稳健", "激进"),
]

_COT_NUDGES: list[str] = [
    "\n[Chain-of-Thought] Show your reasoning before the final answer.",
    "\n[思维链] 在最终回答前展示你的推理过程。",
    "\nReason step-by-step, then conclude.",
]


def _hash_prompt(prompt: str) -> str:
    """Deterministic short hash of a prompt string."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def _new_genome_id() -> str:
    """Generate a unique genome identifier."""
    return f"g-{int(time.time() * 1000) % 10**10}-{random.randint(1000, 9999)}"


# ---------------------------------------------------------------------------
# Mutation engine
# ---------------------------------------------------------------------------

def mutate_prompt(base_prompt: str, mutation_rate: float = 0.1) -> tuple[str, list[str]]:
    """Apply random micro-mutations to a system prompt.

    Args:
        base_prompt: The original prompt text.
        mutation_rate: Probability (0-1) of each mutation category firing.

    Returns:
        Tuple of (mutated_prompt, list_of_mutations_applied).
    """
    prompt = base_prompt
    applied: list[str] = []

    # 1. Synonym swaps
    for original, replacement in _SYNONYM_PAIRS:
        if random.random() < mutation_rate and original in prompt:
            prompt = prompt.replace(original, replacement, 1)
            applied.append(f"synonym_swap:{original}->{replacement}")

    # 2. Add / remove emphasis markers
    if random.random() < mutation_rate:
        marker = random.choice(_EMPHASIS_MARKERS)
        # Find a sentence boundary to emphasise
        lines = prompt.split("\n")
        if lines:
            idx = random.randint(0, len(lines) - 1)
            line = lines[idx].strip()
            if line and marker not in line:
                lines[idx] = f"{marker} {lines[idx]} {marker}"
                applied.append(f"add_emphasis:{marker}@line{idx}")
            elif marker in line:
                lines[idx] = lines[idx].replace(marker, "")
                applied.append(f"remove_emphasis:{marker}@line{idx}")
            prompt = "\n".join(lines)

    # 3. Add / remove reasoning step hints
    if random.random() < mutation_rate:
        hint = random.choice(_REASONING_HINTS)
        if hint not in prompt:
            prompt = prompt + "\n" + hint
            applied.append(f"add_reasoning_hint:{hint[:30]}")
        else:
            prompt = prompt.replace(hint, "")
            applied.append(f"remove_reasoning_hint:{hint[:30]}")

    # 4. Temperature-like language adjustment
    for cautious, bold in _TEMPERATURE_SWAPS:
        if random.random() < mutation_rate:
            if cautious in prompt:
                prompt = prompt.replace(cautious, bold, 1)
                applied.append(f"temp_swap:{cautious}->{bold}")
            elif bold in prompt:
                prompt = prompt.replace(bold, cautious, 1)
                applied.append(f"temp_swap:{bold}->{cautious}")

    # 5. Insert / remove chain-of-thought nudge
    if random.random() < mutation_rate:
        nudge = random.choice(_COT_NUDGES)
        if nudge not in prompt:
            prompt = prompt + nudge
            applied.append(f"add_cot_nudge:{nudge.strip()[:30]}")
        else:
            prompt = prompt.replace(nudge, "")
            applied.append(f"remove_cot_nudge:{nudge.strip()[:30]}")

    if not applied:
        applied.append("no_mutation")

    return prompt, applied


# ---------------------------------------------------------------------------
# Fitness evaluation
# ---------------------------------------------------------------------------

def evaluate_fitness(genome: PromptGenome, recent_outcomes: list[dict[str, Any]]) -> float:
    """Score a genome based on recent task outcomes.

    Formula: success_rate * 0.4 + (1 - error_rate) * 0.3 + cost_efficiency * 0.3

    Args:
        genome: The prompt genome to evaluate.
        recent_outcomes: List of dicts with keys ``success`` (bool),
            ``tokens_used`` (int), ``errors`` (int).

    Returns:
        Fitness score (higher = better), range roughly [0, 1].
    """
    if not recent_outcomes:
        return genome.fitness_score  # keep existing score

    total = len(recent_outcomes)
    successes = sum(1 for o in recent_outcomes if o.get("success"))
    errors = sum(1 for o in recent_outcomes if o.get("errors", 0) > 0)
    total_tokens = sum(o.get("tokens_used", 0) for o in recent_outcomes) or 1

    success_rate = successes / total
    error_rate = errors / total

    # Cost efficiency: fewer tokens per successful outcome is better.
    # Normalise so that <=500 tokens/task = 1.0, >=5000 = 0.0
    avg_tokens = total_tokens / total
    cost_efficiency = max(0.0, min(1.0, 1.0 - (avg_tokens - 500) / 4500))

    fitness = success_rate * 0.4 + (1.0 - error_rate) * 0.3 + cost_efficiency * 0.3
    return round(fitness, 6)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _genomes_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "prompt_genomes.jsonl"


def _load_population(workspace: Path) -> list[PromptGenome]:
    """Load all genomes from the JSONL file."""
    path = _genomes_path(workspace)
    if not path.is_file():
        return []
    genomes: list[PromptGenome] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    genomes.append(PromptGenome(**data))
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        pass
    return genomes


def _save_population(workspace: Path, genomes: list[PromptGenome]) -> None:
    """Persist genomes to the JSONL file (atomic write)."""
    path = _genomes_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for g in genomes:
                f.write(json.dumps(asdict(g), ensure_ascii=False) + "\n")
        tmp.replace(path)
    except OSError:
        pass


def _load_outcomes(workspace: Path, genome_id: str) -> list[dict[str, Any]]:
    """Load recorded outcomes for a specific genome."""
    path = Path(workspace).resolve() / ".claw" / "prompt_outcomes.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and data.get("genome_id") == genome_id:
                    out.append(data)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out[-100:]  # last 100 outcomes


# ---------------------------------------------------------------------------
# Evolution logic
# ---------------------------------------------------------------------------

def _crossover(parent_a: PromptGenome, parent_b: PromptGenome) -> list[str]:
    """Combine mutations from two parents (uniform crossover)."""
    combined = list(set(parent_a.mutations + parent_b.mutations))
    if not combined:
        return []
    k = max(1, len(combined) // 2)
    return random.sample(combined, min(k, len(combined)))


def _make_child(
    parent_a: PromptGenome,
    parent_b: PromptGenome,
    generation: int,
    base_prompt: str,
) -> PromptGenome:
    """Create a child genome from two parents via crossover + fresh mutation."""
    inherited = _crossover(parent_a, parent_b)
    _, new_muts = mutate_prompt(base_prompt, mutation_rate=0.15)
    all_muts = list(set(inherited + [m for m in new_muts if m != "no_mutation"]))

    return PromptGenome(
        genome_id=_new_genome_id(),
        base_prompt_hash=parent_a.base_prompt_hash,
        mutations=all_muts,
        fitness_score=0.5,  # neutral starting fitness
        generation=generation,
        created_at=time.time(),
        task_success_rate=0.0,
        avg_token_cost=0.0,
        error_rate=0.0,
    )


def run_evolution_step(workspace: Path) -> dict[str, Any]:
    """Run one generation of prompt evolution.

    Loads the current population, selects the top 2, breeds 2 children,
    culls the bottom 2, and saves.

    Args:
        workspace: Project workspace root.

    Returns:
        Dict with ``generation``, ``best_fitness``, ``mutations_applied``.
    """
    workspace = Path(workspace).resolve()
    population = _load_population(workspace)

    # Bootstrap with a seed genome if population is empty / too small
    if len(population) < 4:
        base = "You are DevClaw, an autonomous AI agent."
        while len(population) < 4:
            _, muts = mutate_prompt(base, mutation_rate=0.2)
            population.append(PromptGenome(
                genome_id=_new_genome_id(),
                base_prompt_hash=_hash_prompt(base),
                mutations=muts,
                fitness_score=0.5,
                generation=0,
                created_at=time.time(),
                task_success_rate=0.0,
                avg_token_cost=0.0,
                error_rate=0.0,
            ))

    # Re-evaluate fitness for each genome based on recorded outcomes
    for g in population:
        outcomes = _load_outcomes(workspace, g.genome_id)
        if outcomes:
            g.fitness_score = evaluate_fitness(g, outcomes)

    # Sort by fitness descending
    population.sort(key=lambda g: g.fitness_score, reverse=True)

    top_a, top_b = population[0], population[1]
    current_gen = max(g.generation for g in population) + 1

    base_prompt = "You are DevClaw, an autonomous AI agent."
    child_1 = _make_child(top_a, top_b, current_gen, base_prompt)
    child_2 = _make_child(top_b, top_a, current_gen, base_prompt)

    # Cull weakest 2
    population = population[:-2]
    population.extend([child_1, child_2])

    _save_population(workspace, population)

    return {
        "generation": current_gen,
        "best_fitness": top_a.fitness_score,
        "mutations_applied": child_1.mutations + child_2.mutations,
        "population_size": len(population),
    }


# ---------------------------------------------------------------------------
# Active prompt overlay
# ---------------------------------------------------------------------------

def get_active_prompt_overlay(workspace: Path) -> str:
    """Return the best genome's mutations as a prompt suffix.

    This is meant to be appended to the base system prompt at runtime.

    Args:
        workspace: Project workspace root.

    Returns:
        A string of mutation-derived instructions, or empty string.
    """
    population = _load_population(workspace)
    if not population:
        return ""

    population.sort(key=lambda g: g.fitness_score, reverse=True)
    best = population[0]

    if not best.mutations or best.mutations == ["no_mutation"]:
        return ""

    # Build a compact overlay from mutation descriptions
    overlay_parts: list[str] = []
    for mut in best.mutations:
        if mut.startswith("synonym_swap:"):
            overlay_parts.append(f"[Pref] {mut.split(':', 1)[1]}")
        elif mut.startswith("add_reasoning_hint:"):
            overlay_parts.append(f"[Hint] {mut.split(':', 1)[1]}")
        elif mut.startswith("add_cot_nudge:"):
            overlay_parts.append(f"[CoT] {mut.split(':', 1)[1]}")
        elif mut.startswith("temp_swap:"):
            overlay_parts.append(f"[Style] {mut.split(':', 1)[1]}")
        elif mut.startswith("add_emphasis:"):
            overlay_parts.append(f"[Emph] {mut.split(':', 1)[1]}")
        else:
            overlay_parts.append(f"[Mut] {mut}")

    return "\n--- Evolved Prompt Overlay (gen {}) ---\n{}".format(
        best.generation,
        "\n".join(overlay_parts),
    )


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------

def record_outcome(
    workspace: Path,
    genome_id: str,
    success: bool,
    tokens_used: int,
    errors: int,
) -> None:
    """Record a task outcome for the active genome.

    Args:
        workspace: Project workspace root.
        genome_id: The genome that was active during the task.
        success: Whether the task succeeded.
        tokens_used: Token count consumed.
        errors: Number of errors encountered.
    """
    workspace = Path(workspace).resolve()
    path = workspace / ".claw" / "prompt_outcomes.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "genome_id": genome_id,
            "success": success,
            "tokens_used": tokens_used,
            "errors": errors,
            "ts": time.time(),
        }, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError:
        pass

    # Also update inline metrics on the genome
    population = _load_population(workspace)
    for g in population:
        if g.genome_id == genome_id:
            outcomes = _load_outcomes(workspace, genome_id)
            total = len(outcomes) or 1
            g.task_success_rate = sum(1 for o in outcomes if o.get("success")) / total
            g.avg_token_cost = sum(o.get("tokens_used", 0) for o in outcomes) / total
            g.error_rate = sum(1 for o in outcomes if o.get("errors", 0) > 0) / total
            g.fitness_score = evaluate_fitness(g, outcomes)
            break
    _save_population(workspace, population)
