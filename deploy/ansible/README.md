# HyperMnesia with Ansible

These roles install the same HyperMnesia release on workstations, conventional Linux servers,
and Kubernetes. Client processes remain local stdio MCP servers; PostgreSQL, embedding services,
optional reranking, and optional LLM inference may run elsewhere.

## Roles

| Role | Responsibility |
| --- | --- |
| `hypermnesia_release` | Publishes an immutable Git revision through the transactional `hm install` workflow. |
| `hypermnesia_client` | Writes private settings, registers Claude Code or Codex, manages optional hooks and timer, then runs a read-only MCP smoke. |
| `hypermnesia_server` | Runs PostgreSQL, TEI, and optional reranking with Docker Compose on one Linux host. |
| `hypermnesia_kubernetes` | Reconciles PostgreSQL, TEI, optional reranking, optional vLLM, migrations, and memory jobs. |

The roles do not invent credentials, open public listeners, or silently replace a foreign MCP
registration. A conflict fails closed; an authorized forced replacement leaves a timestamped
backup.

## Requirements

- Ansible Core 2.16 or newer.
- Git and Rust on hosts that build HyperMnesia, or a prebuilt `hypermnesia_mcp_binary`.
- Docker with Compose and `community.docker` for the single-server role.
- `kubernetes.core` and a working kubeconfig for the Kubernetes role.

```sh
ansible-galaxy collection install -r deploy/ansible/requirements.yml
```

Use a commit SHA or immutable release tag. The release role rejects `HEAD`, `main`, and
`master`: a moving branch can produce different machines from one playbook run.

## Workstation client

```sh
cp deploy/ansible/examples/inventory.yml inventory.yml
cp deploy/ansible/examples/group_vars/hypermnesia_clients.yml group_vars/hypermnesia_clients.yml
ansible-vault create group_vars/hypermnesia_clients.vault.yml
ansible-playbook -i inventory.yml deploy/ansible/playbooks/workstation.yml --ask-vault-pass
```

The role writes `~/.HyperMnesia/hypermnesia.env` with mode `0600`. `MEM_AUTHOR` is a stable
identity claim, not authentication. `hypermnesia_settings` is only for the allowlisted tunables
that HyperMnesia reads from this protected file. Connection credentials and executable paths are
intentionally refused by that loader; inject them into the client process from Vault or another
secret backend. Codex inherits the names listed in `hypermnesia_client_env_vars`.

Fresh Claude Code registration uses a structural JSON merger. It preserves unrelated keys,
deduplicates hooks and deny rules, writes atomically, and refuses a foreign `hypermnesia` entry.
Codex registration is an Ansible-managed TOML block with the same conflict check. The Codex
allowlist is read-only; Claude receives explicit denies for `memory_write` and
`memory_supersede`.

Extraction is opt-in:

```yaml
hypermnesia_extract_timer_enabled: true
hypermnesia_linger_enabled: true
```

The default installs and disables the timer. It does not enable lingering merely because a role
ran.

## One Linux data-plane host

The `single-server.yml` play installs the release and starts the repository's Compose stack.
PostgreSQL, TEI, and reranking bind to `127.0.0.1` by default. Keep that default when clients
arrive through SSH forwarding, WireGuard, or another authenticated private path.

```sh
cp deploy/ansible/examples/group_vars/hypermnesia_servers.yml group_vars/hypermnesia_servers.yml
ansible-vault create group_vars/hypermnesia_servers.vault.yml
ansible-playbook -i inventory.yml deploy/ansible/playbooks/single-server.yml --ask-vault-pass
```

Migrations run through `hm migrate` after Compose reports healthy. Disable them when a separate
release pipeline owns schema changes.

## Kubernetes data plane

The Kubernetes role expects an explicit storage class and, by default, digest-pinned images. It
creates no Ingress or LoadBalancer. Services are ClusterIP. NetworkPolicies admit only explicitly
labelled client pods.

```sh
cp deploy/ansible/examples/group_vars/hypermnesia_kubernetes.yml group_vars/hypermnesia_kubernetes.yml
ansible-playbook -i inventory.yml deploy/ansible/playbooks/kubernetes.yml
```

Production values pin every enabled image as `name@sha256:...`. A disposable evaluation cluster
may set `hypermnesia_require_image_digests: false`. A migration Job reads a complete
`HM_ADMIN_URL` from `hypermnesia_migration_existing_secret`, avoiding URL construction from an
unescaped password. Give every release a new DNS-safe migration ID.

### Optional vLLM

Set `hypermnesia_vllm_enabled: true`, a digest-pinned image, exact model, and suitable resources.
vLLM can serve Dreamer, dialectic synthesis, and other model-backed jobs. It does not replace TEI:
embeddings and text generation have different APIs and resource profiles.

GPU node selectors, tolerations, runtime classes, and vendor resource keys are cluster-specific.
Keep them in a site overlay; guessing them in a portable role would be friendly-looking fiction.

## Verification and rollback

The client smoke sends `initialize`, `tools/list`, and `project_status({})` to the installed
stdio server. It verifies identity and the read-only catalog without printing memory content:

```sh
HM_REPO=my-project HM_ROOT="$PWD" \
  python3 deploy/ansible/roles/hypermnesia_client/files/smoke.py \
  "$HOME/.HyperMnesia/current/bin/hypermnesia-mcp"
python3 deploy/ansible/tests/test_config_merge.py
```

Run a play twice; the second run should report no changes. Check mode is useful before the first
run, but cannot prove a Docker health check or MCP handshake.

To roll back a host, set `hypermnesia_version` to the earlier commit and rerun. Registrations
point to `~/.HyperMnesia/current`, so they follow the atomic switch. In Kubernetes, restore the
previous image digests. Image rollback does not reverse a database migration, nor should an
automation role pretend that it does.
