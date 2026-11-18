# cia-agentscope Kubernetes Helm Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, validate, document, and package a Helm Chart that deploys one `cia-agentscope` replica behind a ClusterIP service, uses existing MySQL/Redis services, and mounts the node-shared `/app/dify/sfs` path.

**Architecture:** A single application Chart owns only the application Deployment, ClusterIP Service, ConfigMap, optional Secret, and ServiceAccount. External MySQL, Redis, and later OpenSandbox endpoints are injected as configuration; shared session files use a parameterized hostPath by default, with an existing PVC compatibility branch for future migration.

**Tech Stack:** Helm 3, Kubernetes `apps/v1` and `v1` resources, JSON Schema draft-07, YAML, PowerShell verification commands

**Spec:** `docs/superpowers/specs/2026-10-13-kubernetes-helm-deployment-design.md`

## Global Constraints

- The application image repository defaults to `registry.ccdc.cn:5000/cia-agentscope` and no rendered workload may reference a public registry.
- The application listens on port `7010`; the Service type is `ClusterIP` and both Service and container ports default to `7010`.
- The production default is one replica because request cancellation, feedback, background tasks, and some orchestration state remain process-local.
- MySQL and Redis are existing external services; this Chart must not create either database.
- OpenSandbox deployment is out of scope; expose only connection settings and an optional API key reference.
- The accepted storage mode is hostPath `/app/dify/sfs` with `type: Directory`, mounted at `/data/session-files`; never use `DirectoryOrCreate`.
- The current Dockerfile runs as root, so `runAsNonRoot` must not be enabled by default; disable privilege escalation and drop all capabilities.
- Production secrets come from an existing Kubernetes Secret. Inline Secret creation is a development-only option and production example values contain no plaintext secret material.
- Do not modify the existing Dockerfile, Docker Compose file, OpenSandbox manifests, application code, `.env`, or unrelated dirty files while implementing this plan.
- Generated Chart archives go to `dist/` but only explicitly named Helm source and documentation files are staged in task commits unless the user requests binary artifacts in Git.

## Review Focus

- A missing or unmounted SFS directory must fail Pod volume setup instead of silently creating node-local storage; Task 3 asserts `hostPath.type: Directory` and the exact path.
- A missing production Secret name must make rendering fail, while the production example must render no Secret object or plaintext credentials; Tasks 2 and 3 cover both branches.
- Invalid storage types and an `existingClaim` mode without a claim name must fail schema or template validation; Tasks 1 and 3 include negative renders.
- The current root-based image must remain launchable without weakening the other container controls; Task 3 asserts no default `runAsNonRoot: true`, plus `allowPrivilegeEscalation: false` and `drop: [ALL]`.
- Accidental scale-out must not become the default while process-local state exists; Tasks 1 and 3 assert `replicaCount: 1` and `spec.replicas: 1`, and Task 4 documents the limitation.

## File Map

- `charts/cia-agentscope/Chart.yaml`: Chart identity, version `0.1.0`, and application version used as the fallback image tag.
- `charts/cia-agentscope/.helmignore`: Excludes editor, VCS, test cache, and packaged artifact noise.
- `charts/cia-agentscope/values.yaml`: Complete public configuration contract and safe defaults.
- `charts/cia-agentscope/values.schema.json`: Types, enums, numeric bounds, and conditional requirements for storage and secret modes.
- `charts/cia-agentscope/templates/_helpers.tpl`: Stable resource names, labels, selector labels, image tag, ServiceAccount name, and resolved Secret name.
- `charts/cia-agentscope/templates/configmap.yaml`: Non-sensitive application environment variables.
- `charts/cia-agentscope/templates/secret.yaml`: Optional development-only Secret; absent when `secret.create=false`.
- `charts/cia-agentscope/templates/serviceaccount.yaml`: Optional namespaced ServiceAccount without extra RBAC.
- `charts/cia-agentscope/templates/deployment.yaml`: Pod lifecycle, probes, security, secret references, scheduling options, and storage branch.
- `charts/cia-agentscope/templates/service.yaml`: Internal ClusterIP service.
- `charts/cia-agentscope/templates/NOTES.txt`: Post-install inspection and health-check commands.
- `charts/cia-agentscope/examples/values-prod-hostpath.yaml`: Non-secret production example for the internal registry, external data services, and SFS hostPath.
- `charts/cia-agentscope/README.md`: Prerequisites, image/Secret preparation, install/upgrade/rollback, SFS checks, and troubleshooting.
- `dist/cia-agentscope-0.1.0.tgz`: Generated offline Chart archive; local artifact, not staged by default.
- `dist/cia-agentscope-0.1.0.tgz.sha256`: Generated checksum; local artifact, not staged by default.

---

### Task 1: Establish the Chart contract and validation schema

**Files:**
- Create: `charts/cia-agentscope/Chart.yaml`
- Create: `charts/cia-agentscope/.helmignore`
- Create: `charts/cia-agentscope/values.yaml`
- Create: `charts/cia-agentscope/values.schema.json`
- Create: `charts/cia-agentscope/templates/_helpers.tpl`
- Create: `charts/cia-agentscope/examples/values-prod-hostpath.yaml`

**Interfaces:**
- Consumes: The exact image, port, external service, Secret, storage, probe, scheduling, security, and rollout decisions in the design spec.
- Produces: `.Values` keys used by Tasks 2-4 and helper templates named `cia-agentscope.name`, `cia-agentscope.fullname`, `cia-agentscope.chart`, `cia-agentscope.labels`, `cia-agentscope.selectorLabels`, `cia-agentscope.image`, `cia-agentscope.serviceAccountName`, and `cia-agentscope.secretName`.

- [ ] **Step 0: Prepare a temporary Helm 3 validator**

Run `Get-Command helm`. Because Helm is not currently installed on this workstation, if it is still absent at execution time, download the official Helm `v3.22.0` Windows amd64 archive into a task-specific temporary directory, verify the published SHA256 `ff490897e07e976c65a9bd7690cfc139b35ba5e8f25d00eaf1e53a30f1ad3f62`, extract it there, and add only that extracted directory to the current PowerShell process PATH. Do not install it globally or add it to the repository.

Run `helm version --short`.

Expected: output starts with `v3.22.0`.

- [ ] **Step 1: Record the missing-Chart failure**

Run:

```powershell
helm lint charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml
```

Expected: FAIL because `Chart.yaml` and the Chart directory do not exist.

- [ ] **Step 2: Create Chart metadata and the public values contract**

Create `Chart.yaml` with `apiVersion: v2`, `name: cia-agentscope`, `type: application`, `version: 0.1.0`, and `appVersion: "0.1.0"`. Define these top-level values in `values.yaml`:

- `replicaCount`, `image`, `imagePullSecrets`, `nameOverride`, `fullnameOverride`;
- `serviceAccount`, `podAnnotations`, `podLabels`, `podSecurityContext`, `securityContext`;
- `service`, `deploymentStrategy`, `terminationGracePeriodSeconds`;
- `probes.startup`, `probes.readiness`, `probes.liveness`;
- `storage.type`, `storage.hostPath`, `storage.mountPath`, `storage.existingClaim`;
- `externalMysql`, `externalRedis`, `opensandbox`, `logging`;
- `existingSecret.name`, the exact key mappings `mysqlPassword`, `jwtSecret`, `configEncryptionKey`, and `opensandboxApiKey`, plus `secret.create` and `secret.stringData`;
- `resources`, `nodeSelector`, `tolerations`, `affinity`, `extraEnv`, `extraEnvFrom`, `extraVolumes`, `extraVolumeMounts`.

Pin defaults to `replicaCount: 1`, `service.type: ClusterIP`, port `7010`, hostPath `/app/dify/sfs`, mount path `/data/session-files`, OpenSandbox domain `localhost:9080`, OpenSandbox base image `registry.ccdc.cn:5000/python:3.13-slim`, `OPENSANDBOX_POOL_SIZE=0`, `allowPrivilegeEscalation: false`, and dropped capability `ALL`. Leave external MySQL/Redis hosts, credentials, and production application image tag empty in the base values.

- [ ] **Step 3: Create the production hostPath example**

Set a clearly replaceable image tag, example internal MySQL/Redis DNS names, `existingSecret.name: cia-agentscope-secrets`, `secret.create: false`, one replica, ClusterIP, and the accepted SFS paths. Do not include any password, token, encryption key, or API key value.

- [ ] **Step 4: Implement values schema and helpers**

The JSON schema must reject:

- replica counts below 1;
- non-ClusterIP service types;
- container/service ports outside 1-65535;
- storage types outside `hostPath` and `existingClaim`;
- `hostPath` mode with an empty `storage.hostPath`;
- `existingClaim` mode with an empty `storage.existingClaim`;
- `secret.create=false` with an empty `existingSecret.name`;
- empty image repository, MySQL host/user/database, or Redis URL in a rendered production values set.

The image helper returns `<repository>:<tag>` using `.Values.image.tag` when non-empty and `.Chart.AppVersion` otherwise. The Secret-name helper returns the generated full name only when `secret.create=true`; otherwise it requires `existingSecret.name`.

- [ ] **Step 5: Run positive and negative contract checks**

Run:

```powershell
helm lint charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml
helm lint charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set storage.type=invalid
helm lint charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set storage.type=existingClaim --set storage.existingClaim=
helm lint charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set existingSecret.name=
```

Expected: the first command reports `0 chart(s) failed`; the following three commands fail with schema or required-value messages naming the invalid field. Confirm `values.yaml` contains `replicaCount: 1` and contains no plaintext secret values.

- [ ] **Step 6: Commit the Chart contract**

```powershell
git add -- charts/cia-agentscope/Chart.yaml charts/cia-agentscope/.helmignore charts/cia-agentscope/values.yaml charts/cia-agentscope/values.schema.json charts/cia-agentscope/templates/_helpers.tpl charts/cia-agentscope/examples/values-prod-hostpath.yaml
git commit -m "feat: define cia-agentscope helm chart contract"
```

### Task 2: Render non-sensitive configuration and Secret modes

**Files:**
- Create: `charts/cia-agentscope/templates/configmap.yaml`
- Create: `charts/cia-agentscope/templates/secret.yaml`
- Modify: `charts/cia-agentscope/values.yaml`
- Modify: `charts/cia-agentscope/values.schema.json`

**Interfaces:**
- Consumes: The values contract and naming/label helpers from Task 1.
- Produces: A ConfigMap consumed via `envFrom`, and either an existing Secret name or a generated development Secret consumed through individual `secretKeyRef` entries in Task 3.

- [ ] **Step 1: Verify configuration templates are initially absent**

Run:

```powershell
helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --show-only templates/configmap.yaml
```

Expected: FAIL because `templates/configmap.yaml` does not exist.

- [ ] **Step 2: Create the application ConfigMap**

Render these exact keys as strings:

`MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_DATABASE`, `REDIS_URL`, `REDIS_SESSION_TTL`, `SESSION_FILES_PERSIST_DIR`, `OPENSANDBOX_DOMAIN`, `OPENSANDBOX_PROTOCOL`, `OPENSANDBOX_USE_SERVER_PROXY`, `OPENSANDBOX_IMAGE`, `OPENSANDBOX_RESOURCE_CPU`, `OPENSANDBOX_RESOURCE_MEMORY`, `WORKSPACE_TTL`, `OPENSANDBOX_POOL_SIZE`, `OPENSANDBOX_POOL_REFILL`, `LOG_LEVEL`, and `LOG_DIR`.

Set `SESSION_FILES_PERSIST_DIR` from `storage.mountPath`. Keep passwords, JWT data, encryption keys, and API keys out of the ConfigMap.

- [ ] **Step 3: Create the optional development Secret template**

Render a `v1/Secret` with `type: Opaque` and `stringData` only when `secret.create=true`. When `secret.create=false`, render no Secret document. Require the inline MySQL password and JWT secret only in the create branch; include config-encryption and OpenSandbox keys only when supplied.

- [ ] **Step 4: Verify both Secret branches and sensitive-key separation**

Run:

```powershell
$prod = helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml
$prod | Select-String 'kind: ConfigMap|MYSQL_HOST|SESSION_FILES_PERSIST_DIR|/data/session-files|OPENSANDBOX_IMAGE|registry.ccdc.cn:5000/python:3.13-slim'
$leaks = $prod | Select-String 'kind: Secret|MYSQL_PASSWORD:|JWT_SECRET:'
if ($leaks) { throw "Production render contains a Secret or plaintext sensitive key" }

$dev = helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set secret.create=true --set existingSecret.name= --set-string secret.stringData.MYSQL_PASSWORD=test-only --set-string secret.stringData.JWT_SECRET=test-only
$dev | Select-String 'kind: Secret|MYSQL_PASSWORD: test-only|JWT_SECRET: test-only'
```

Expected: the production render contains the ConfigMap and `/data/session-files`, with no Secret kind or plaintext sensitive keys. The development render contains exactly one Opaque Secret with the two supplied test-only values.

- [ ] **Step 5: Commit configuration templates**

```powershell
git add -- charts/cia-agentscope/templates/configmap.yaml charts/cia-agentscope/templates/secret.yaml charts/cia-agentscope/values.yaml charts/cia-agentscope/values.schema.json
git commit -m "feat: add helm configuration and secret templates"
```

### Task 3: Implement the application workload, ClusterIP service, and SFS mount

**Files:**
- Create: `charts/cia-agentscope/templates/serviceaccount.yaml`
- Create: `charts/cia-agentscope/templates/deployment.yaml`
- Create: `charts/cia-agentscope/templates/service.yaml`
- Create: `charts/cia-agentscope/templates/NOTES.txt`
- Modify: `charts/cia-agentscope/values.yaml`
- Modify: `charts/cia-agentscope/values.schema.json`

**Interfaces:**
- Consumes: Resource helpers, ConfigMap name, resolved Secret name, values contract, and exact environment keys from Tasks 1-2.
- Produces: The installable `apps/v1/Deployment`, internal `v1/Service`, optional ServiceAccount, post-install commands, and both hostPath/PVC volume branches used by Task 4 packaging.

- [ ] **Step 1: Verify workload templates are initially absent**

Run:

```powershell
helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --show-only templates/deployment.yaml
```

Expected: FAIL because `templates/deployment.yaml` does not exist.

- [ ] **Step 2: Implement ServiceAccount and ClusterIP Service**

Create a ServiceAccount only when `serviceAccount.create=true`, using the explicit name when supplied and the Chart full name otherwise. The Service selects the stable selector labels, renders configurable annotations, uses only `ClusterIP`, and maps `.Values.service.port` to the named container port `http`.

- [ ] **Step 3: Implement Deployment metadata, image, rollout, and scheduling**

Render:

- `.Values.replicaCount`, RollingUpdate strategy, and 60-second default termination grace period;
- `imagePullSecrets`, pod annotations/labels, pod/container security contexts, resources, nodeSelector, affinity, and tolerations;
- the Task 1 image helper and `imagePullPolicy`;
- container port named `http` at `7010`;
- ConfigMap through `envFrom`, then `extraEnvFrom`, individual Secret refs, then `extraEnv`;
- a ConfigMap checksum pod annotation so non-sensitive configuration changes trigger a rollout.

Secret refs for MySQL password and JWT secret are mandatory. Config-encryption and OpenSandbox API key refs are optional so OpenSandbox can be deployed later without changing the Chart structure.

- [ ] **Step 4: Implement lifecycle probes and both storage branches**

All startup, readiness, and liveness probes call `GET /health` on named port `http`, with thresholds and periods from values. For `storage.type=hostPath`, render exact host path `/app/dify/sfs`, `type: Directory`, and mount it at `/data/session-files`. For `storage.type=existingClaim`, render the supplied PVC claim name instead. Append `extraVolumes` and `extraVolumeMounts` without changing the primary shared-data volume.

- [ ] **Step 5: Add NOTES output**

Print commands that show the Service, wait for Deployment rollout, port-forward local port 7010, call `/health`, and remind the operator that OpenSandbox-dependent functions remain unavailable until that service is deployed.

- [ ] **Step 6: Verify the accepted hostPath render**

Run:

```powershell
$rendered = helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml
$rendered | Select-String 'replicas: 1|type: ClusterIP|containerPort: 7010|path: /health|path: /app/dify/sfs|type: Directory|mountPath: /data/session-files'
$rendered | Select-String 'allowPrivilegeEscalation: false|drop:|ALL'
$forbidden = $rendered | Select-String 'runAsNonRoot: true|docker.io|quay.io|ghcr.io'
if ($forbidden) { throw "Rendered workload contains forbidden security or registry settings" }
```

Expected: all required hostPath, probe, ClusterIP, port, single-replica, and restricted-capability strings are present. The last command returns no matches.

- [ ] **Step 7: Verify validation and compatibility branches**

Run:

```powershell
helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set existingSecret.name=
helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set storage.type=existingClaim --set storage.existingClaim=shared-files | Select-String 'claimName: shared-files'
helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set storage.type=invalid
helm template test charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --set replicaCount=0
```

Expected: missing existing Secret, invalid storage type, and zero replicas fail. The valid existing-PVC command succeeds and renders `claimName: shared-files` with no hostPath.

- [ ] **Step 8: Commit workload templates**

```powershell
git add -- charts/cia-agentscope/templates/serviceaccount.yaml charts/cia-agentscope/templates/deployment.yaml charts/cia-agentscope/templates/service.yaml charts/cia-agentscope/templates/NOTES.txt charts/cia-agentscope/values.yaml charts/cia-agentscope/values.schema.json
git commit -m "feat: deploy cia-agentscope with helm"
```

### Task 4: Document operations and produce the offline Chart package

**Files:**
- Create: `charts/cia-agentscope/README.md`
- Create locally: `dist/cia-agentscope-0.1.0.tgz`
- Create locally: `dist/cia-agentscope-0.1.0.tgz.sha256`

**Interfaces:**
- Consumes: The complete Chart and production example from Tasks 1-3.
- Produces: Operator-facing installation/upgrade/rollback guidance and a checksum-verifiable offline package.

- [ ] **Step 1: Write operator documentation**

Document:

- prerequisites: Helm 3, Kubernetes with `startupProbe` support, reachable internal registry, existing MySQL/Redis, and SFS mounted on every eligible node;
- image build/tag/push commands using `registry.ccdc.cn:5000/cia-agentscope:<tag>`;
- a `kubectl create secret generic cia-agentscope-secrets` example using shell variables or `--from-file`, never literal real credentials;
- required edits in `examples/values-prod-hostpath.yaml`;
- `helm upgrade --install`, status, logs, port-forward, `/health`, upgrade, history, and rollback commands;
- SFS verification on every node with `findmnt -T /app/dify/sfs`, `mountpoint`, a cross-node marker-file check, and UID/GID write-permission checks against the actual image;
- the default single-replica limitation and how to keep scheduling on SFS-verified nodes;
- MySQL startup/DDL requirements, Redis connectivity, OpenSandbox-later behavior, SSE shutdown behavior, and common Helm/Pod failure checks;
- future `existingClaim` migration settings.

- [ ] **Step 2: Run complete source validation**

Run:

```powershell
helm lint charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml --strict
helm template cia-agentscope charts/cia-agentscope -f charts/cia-agentscope/examples/values-prod-hostpath.yaml | Out-File -Encoding utf8 $env:TEMP\cia-agentscope-rendered.yaml
$forbidden = Select-String -Path $env:TEMP\cia-agentscope-rendered.yaml -Pattern 'docker.io|quay.io|ghcr.io|kind: Secret|MYSQL_PASSWORD:|JWT_SECRET:'
if ($forbidden) { throw "Production render contains a public registry, Secret, or plaintext sensitive key" }
```

Expected: strict lint passes, template rendering succeeds, and the final search returns no matches.

If `kubeconform` is already available, additionally run it against the rendered file with strict mode and require zero invalid resources. Do not make package completion depend on downloading an unapproved secondary validator.

- [ ] **Step 3: Commit the operator documentation**

```powershell
git add -- charts/cia-agentscope/README.md
git commit -m "docs: add helm deployment runbook"
```

- [ ] **Step 4: Package and checksum the Chart**

Run:

```powershell
helm package charts/cia-agentscope --destination dist
$hash = (Get-FileHash -Algorithm SHA256 dist/cia-agentscope-0.1.0.tgz).Hash.ToLowerInvariant()
Set-Content -Encoding ascii dist/cia-agentscope-0.1.0.tgz.sha256 "$hash  cia-agentscope-0.1.0.tgz"
helm show chart dist/cia-agentscope-0.1.0.tgz
helm template cia-agentscope dist/cia-agentscope-0.1.0.tgz -f charts/cia-agentscope/examples/values-prod-hostpath.yaml | Out-Null
```

Expected: the package reports Chart version `0.1.0`, the checksum file contains one lowercase SHA256 entry, and the packaged Chart renders successfully with production example values.

- [ ] **Step 5: Perform final scoped review**

Run:

```powershell
git status --short
git log --oneline -5
git diff --check HEAD~4..HEAD
```

Expected: the Helm source commits contain only the explicitly listed Chart and documentation files; pre-existing unrelated working-tree changes remain untouched. Report that static Chart validation passed, while target-cluster installation, SFS cross-node behavior, external MySQL/Redis connectivity, image pull, and rollback remain cluster-side acceptance steps.
