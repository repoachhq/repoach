# SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB — Each CI job's LLM proxy runs on its own port and proves it is that job's process

Give every proxy-starting CI job a free port and an instance token, prove by PID (then by the /health instance echo) that the listener is the process the job spawned, export the URL through $GITHUB_ENV, and stop the proxy at job end.

## Step 1 — Echo the per-job instance token in /health

- **Files**: `src/repoach/llm_proxy/instance.py`, `src/repoach/llm_proxy/api/routes.py`, `tests/unit/test_job_proxy_instance.py`
- **Action**: Add repoach.llm_proxy.instance.current_instance() reading REPOACH_PROXY_INSTANCE, and make GET /health add an `instance` field to the anonymous and authenticated bodies when it is set, leaving the body unchanged otherwise.
- **Commit**: `feat(proxy): echo a per-job instance token in /health`
- **Done when**: pytest tests/unit/test_job_proxy_instance.py passes
- **Unit tests**: `tests/unit/test_job_proxy_instance.py::test_current_instance_reads_the_env`, `tests/unit/test_job_proxy_instance.py::test_health_echoes_instance_to_anonymous_and_authenticated`, `tests/unit/test_job_proxy_instance.py::test_health_without_instance_keeps_the_liveness_only_body`

## Step 2 — Start each job's proxy on a free port and prove ownership by PID then token

- **Files**: `scripts/job_proxy.sh`, `.github/workflows/auto-review.yml`, `.github/workflows/ci.yml`, `scripts/ci_local.sh`, `tests/job_proxy_fakes.py`, `tests/unit/test_job_proxy_prove.py`, `tests/unit/test_job_proxy_workflow_wiring.py`, `tests/integration/test_job_proxy_collision.py`
- **Action**: Write scripts/job_proxy.sh start|stop|prove (no repoach import): free port, instance token, PID proof via /proc/net/tcp inode in /proc/<pid>/fd, token proof via /health, $GITHUB_ENV export; embed it byte-identically in the review and auto_fix jobs, drop every hardcoded port, stop the proxy in an if: always() step, and route the ci.yml and ci_local.sh smokes through it.
- **Commit**: `fix(ci): prove proxy ownership by pid and embed the helper in jobs`
- **Done when**: pytest tests/unit/test_job_proxy_prove.py tests/unit/test_job_proxy_workflow_wiring.py tests/integration/test_job_proxy_collision.py passes
- **Unit tests**: `tests/unit/test_job_proxy_prove.py::test_dead_spawned_pid_fails_at_once`, `tests/unit/test_job_proxy_prove.py::test_listener_owned_by_another_pid_is_foreign_before_any_http`, `tests/unit/test_job_proxy_prove.py::test_live_pid_without_listener_is_not_ready`, `tests/unit/test_job_proxy_prove.py::test_own_listener_not_healthy_yet_is_not_ready`, `tests/unit/test_job_proxy_prove.py::test_own_pid_and_matching_token_is_own`, `tests/unit/test_job_proxy_prove.py::test_own_pid_with_another_token_is_foreign`, `tests/unit/test_job_proxy_prove.py::test_own_pid_without_echo_is_own_on_the_pid_proof_alone`, `tests/unit/test_job_proxy_prove.py::test_prove_usage_and_unknown_commands_exit_2`, `tests/unit/test_job_proxy_workflow_wiring.py::test_ci_smoke_uses_the_helper_and_no_fixed_port`, `tests/unit/test_job_proxy_workflow_wiring.py::test_embedded_helper_is_byte_identical_to_the_script`, `tests/unit/test_job_proxy_workflow_wiring.py::test_helper_exports_port_instance_and_url_and_imports_no_repoach_module`, `tests/unit/test_job_proxy_workflow_wiring.py::test_proxy_jobs_hardcode_no_port_and_do_not_shadow_github_env`, `tests/unit/test_job_proxy_workflow_wiring.py::test_proxy_jobs_start_through_the_materialized_helper`, `tests/unit/test_job_proxy_workflow_wiring.py::test_proxy_jobs_stop_their_proxy_last_and_always`

## Integration tests

- `tests/integration/test_job_proxy_collision.py::test_base_proxy_without_instance_echo_starts_on_the_pid_proof`
- `tests/integration/test_job_proxy_collision.py::test_old_fixed_port_wiring_is_refused_by_the_pid_proof`
- `tests/integration/test_job_proxy_collision.py::test_two_overlapping_jobs_get_two_ports_and_prove_their_own_proxy`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB",
  "title": "Each CI job's LLM proxy runs on its own port and proves it is that job's process",
  "summary": "Give every proxy-starting CI job a free port and an instance token, prove by PID (then by the /health instance echo) that the listener is the process the job spawned, export the URL through $GITHUB_ENV, and stop the proxy at job end.",
  "steps": [
    {
      "index": 1,
      "title": "Echo the per-job instance token in /health",
      "files": [
        "src/repoach/llm_proxy/instance.py",
        "src/repoach/llm_proxy/api/routes.py",
        "tests/unit/test_job_proxy_instance.py"
      ],
      "action": "Add repoach.llm_proxy.instance.current_instance() reading REPOACH_PROXY_INSTANCE, and make GET /health add an `instance` field to the anonymous and authenticated bodies when it is set, leaving the body unchanged otherwise.",
      "commit_message": "feat(proxy): echo a per-job instance token in /health",
      "done_when": "pytest tests/unit/test_job_proxy_instance.py passes",
      "unit_tests": [
        "tests/unit/test_job_proxy_instance.py::test_current_instance_reads_the_env",
        "tests/unit/test_job_proxy_instance.py::test_health_echoes_instance_to_anonymous_and_authenticated",
        "tests/unit/test_job_proxy_instance.py::test_health_without_instance_keeps_the_liveness_only_body"
      ]
    },
    {
      "index": 2,
      "title": "Start each job's proxy on a free port and prove ownership by PID then token",
      "files": [
        "scripts/job_proxy.sh",
        ".github/workflows/auto-review.yml",
        ".github/workflows/ci.yml",
        "scripts/ci_local.sh",
        "tests/job_proxy_fakes.py",
        "tests/unit/test_job_proxy_prove.py",
        "tests/unit/test_job_proxy_workflow_wiring.py",
        "tests/integration/test_job_proxy_collision.py"
      ],
      "action": "Write scripts/job_proxy.sh start|stop|prove (no repoach import): free port, instance token, PID proof via /proc/net/tcp inode in /proc/<pid>/fd, token proof via /health, $GITHUB_ENV export; embed it byte-identically in the review and auto_fix jobs, drop every hardcoded port, stop the proxy in an if: always() step, and route the ci.yml and ci_local.sh smokes through it.",
      "commit_message": "fix(ci): prove proxy ownership by pid and embed the helper in jobs",
      "done_when": "pytest tests/unit/test_job_proxy_prove.py tests/unit/test_job_proxy_workflow_wiring.py tests/integration/test_job_proxy_collision.py passes",
      "unit_tests": [
        "tests/unit/test_job_proxy_prove.py::test_dead_spawned_pid_fails_at_once",
        "tests/unit/test_job_proxy_prove.py::test_listener_owned_by_another_pid_is_foreign_before_any_http",
        "tests/unit/test_job_proxy_prove.py::test_live_pid_without_listener_is_not_ready",
        "tests/unit/test_job_proxy_prove.py::test_own_listener_not_healthy_yet_is_not_ready",
        "tests/unit/test_job_proxy_prove.py::test_own_pid_and_matching_token_is_own",
        "tests/unit/test_job_proxy_prove.py::test_own_pid_with_another_token_is_foreign",
        "tests/unit/test_job_proxy_prove.py::test_own_pid_without_echo_is_own_on_the_pid_proof_alone",
        "tests/unit/test_job_proxy_prove.py::test_prove_usage_and_unknown_commands_exit_2",
        "tests/unit/test_job_proxy_workflow_wiring.py::test_ci_smoke_uses_the_helper_and_no_fixed_port",
        "tests/unit/test_job_proxy_workflow_wiring.py::test_embedded_helper_is_byte_identical_to_the_script",
        "tests/unit/test_job_proxy_workflow_wiring.py::test_helper_exports_port_instance_and_url_and_imports_no_repoach_module",
        "tests/unit/test_job_proxy_workflow_wiring.py::test_proxy_jobs_hardcode_no_port_and_do_not_shadow_github_env",
        "tests/unit/test_job_proxy_workflow_wiring.py::test_proxy_jobs_start_through_the_materialized_helper",
        "tests/unit/test_job_proxy_workflow_wiring.py::test_proxy_jobs_stop_their_proxy_last_and_always"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_job_proxy_collision.py::test_base_proxy_without_instance_echo_starts_on_the_pid_proof",
    "tests/integration/test_job_proxy_collision.py::test_old_fixed_port_wiring_is_refused_by_the_pid_proof",
    "tests/integration/test_job_proxy_collision.py::test_two_overlapping_jobs_get_two_ports_and_prove_their_own_proxy"
  ]
}
```
