"""Ultimate-form capability stubs: resource, synthesis, nomad mesh, treasury (policy-safe)."""

from claw_runtime.ultimate.colab_bundle import build_colab_job_bundle
from claw_runtime.ultimate.nomad import (
    detect_environment,
    load_nomad_system_append,
    register_nomad_handlers,
    restore_nomad_identity,
    write_nomad_snapshot,
)
from claw_runtime.ultimate.proxy_env import load_proxy_list, proxy_env_for_index, rotate_proxy_index
from claw_runtime.ultimate.skill_synthesis import synthesize_two_skills
from claw_runtime.ultimate.storage_cold import compress_paths, telegram_send_document_if_configured
from claw_runtime.ultimate.subagent_mesh import run_subagent_mesh
from claw_runtime.ultimate.self_heal import emit_rebuild_venv_scripts
from claw_runtime.ultimate.treasury import (
    prepare_self_funding_review,
    probe_eth_balance,
    write_chain_action_draft,
    write_survival_revenue_plan_stub,
    write_treasury_proposal_stub,
)

__all__ = [
    "build_colab_job_bundle",
    "compress_paths",
    "detect_environment",
    "emit_rebuild_venv_scripts",
    "load_proxy_list",
    "load_nomad_system_append",
    "prepare_self_funding_review",
    "probe_eth_balance",
    "proxy_env_for_index",
    "register_nomad_handlers",
    "restore_nomad_identity",
    "rotate_proxy_index",
    "run_subagent_mesh",
    "synthesize_two_skills",
    "telegram_send_document_if_configured",
    "write_survival_revenue_plan_stub",
    "write_chain_action_draft",
    "write_nomad_snapshot",
    "write_treasury_proposal_stub",
]
