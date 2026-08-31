# Automatic UV-CAM deployment to UP2

UV-CAM deploys the latest **tested `uvcam` commit** to the shop's UP2 server. The
`deploy-up2` job in `.github/workflows/integration.yaml` starts only after every unit,
system, feed-model, G-code audit, and JavaScript check succeeds for a push to `uvcam`.
Pull requests and other branches never receive production credentials or deploy.

UP2 is on the private shop network at `10.0.0.50`, so a GitHub-hosted runner cannot
reach it directly. The production job creates a short-lived Tailscale node, connects
over SSH, and removes that node when the job finishes. This avoids exposing SSH to the
internet and avoids attaching a self-hosted runner to this public repository. GitHub
specifically warns against public-repository self-hosted runners because pull-request
code can target the runner host.

## One-time UP2 prerequisites

The tracked `docker-compose.yml` is the production Compose definition. Persistent data
lives outside the rsynced `build/` tree:

```text
/mnt/user/appdata/penguincam/
├── .env                                # FLASK_SECRET_KEY=...
├── config/
│   └── PenguinCAM-config-2129.yaml     # machine limits and feeds
├── build/                              # replaced by each deployment
└── docker-compose.yml                  # installed by each deployment
```

Before the first run, verify on UP2:

```bash
docker compose version
rsync --version
docker network inspect cloudflare-net
test -f /mnt/user/appdata/penguincam/.env
test -f /mnt/user/appdata/penguincam/config/PenguinCAM-config-2129.yaml
```

The `.env`, config directory, and Docker network are preflight requirements and are
never copied, deleted, or replaced by the workflow.

## Private network access

Follow Tailscale's [GitHub Action guide](https://tailscale.com/docs/integrations/github/github-action)
to create a workload federated identity for this repository. Give it only the
`tag:penguincam-deploy` tag. The tag needs access only to TCP port 22 on UP2; UP2 must
either be a tailnet node or be reachable through an approved Tailscale subnet route for
`10.0.0.50`.

Create a dedicated SSH key for this workflow; do not upload a personal SSH key. Add the
public half to UP2's root `authorized_keys` with OpenSSH's `restrict` option, which
disables forwarding and PTYs while retaining the rsync and Docker commands deployment
needs:

```text
restrict ssh-ed25519 AAAA... penguincam-github-actions
```

The tailnet ACL and GitHub environment restriction are both required boundaries around
this key.

## GitHub environment

In **Settings → Environments**, create `up2-production` and allow deployments only from
the `uvcam` branch. Do not add a required reviewer if deployment should remain automatic.
Add these as environment secrets:

| Secret | Value |
|---|---|
| `TS_OAUTH_CLIENT_ID` | Tailscale federated identity client ID |
| `TS_AUDIENCE` | Tailscale federated identity audience |
| `UP2_SSH_PRIVATE_KEY` | Dedicated private key created for this workflow |
| `UP2_SSH_KNOWN_HOSTS` | Trusted UP2 host-key line for `10.0.0.50` |

Capture the host key from the trusted shop LAN, then verify its fingerprint against the
key UP2 already presents before saving it. Do not run `ssh-keyscan` inside the deployment
job: accepting a fresh key during a deployment would remove host authentication.

The workflow uses GitHub OIDC (`id-token: write`) to obtain the short-lived Tailscale
identity. It does not need a long-lived Tailscale client secret.

## What a deployment does

1. CI tests the exact commit on a GitHub-hosted runner.
2. The production environment admits only a successful `uvcam` push.
3. The runner joins the tailnet and verifies that UP2 responds.
4. `scripts/deploy_up2.sh` checks the exact destination and all persistent prerequisites.
5. It rsyncs the source with root-anchored exclusions and stages the tracked Compose file.
6. UP2 validates Compose and tags the current image as `penguincam:rollback`.
7. Docker builds the new image while the old container keeps serving traffic.
8. The image's OCI revision label must equal the tested Git SHA.
9. Compose replaces the container and waits up to 150 seconds for its health check.
10. A failed health check restores both the prior image and prior Compose file.

Deployments use a concurrency group and are never cancelled mid-rollout. A later commit
waits for the current deployment rather than racing it.

## Manual recovery and verification

From a trusted workstation whose SSH config contains `Host UP2`, the same guarded script
can deploy a checked-out commit:

```bash
UP2_DEPLOY_TARGET=UP2 bash scripts/deploy_up2.sh "$(git rev-parse HEAD)"
```

Check the live revision and health on UP2:

```bash
docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' penguincam:local
docker inspect --format '{{ .State.Health.Status }}' penguincam
docker compose -f /mnt/user/appdata/penguincam/docker-compose.yml logs --tail=100 penguincam
```

GitHub also records each run against the `up2-production` environment and links it to
<https://cam.roemen.org>.
