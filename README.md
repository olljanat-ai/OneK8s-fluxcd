# OneK8s-fluxcd

**The other delivery plane.** This repository is what [Flux](https://fluxcd.io)
reconciles on the AKS and EKS clusters of the OneK8s platform — the same job
[OneK8s-argocd](https://github.com/olljanat-ai/OneK8s-argocd) does for Argo CD,
deliberately done a different way so the two can be compared while running side
by side on the same clusters, against the same applications repository.

It holds no application source, no chart of an application and no Terraform.

| Repository | Owns |
|---|---|
| [OneK8s](https://github.com/olljanat-ai/OneK8s) | The clusters and the platform: foundations, tenants, and the delivery planes themselves — the Argo CD hub, Kargo, and the per-cluster Flux this repository is bootstrapped into. |
| [OneK8s-argocd](https://github.com/olljanat-ai/OneK8s-argocd) | Argo CD's answer to *where and when*: one hub, spokes registered as clusters, `ApplicationSet`s, and Kargo in front of production. |
| **OneK8s-fluxcd** (this one) | Flux's answer to the same question, with no hub and no promotion engine: one directory per cluster, and one shared application definition they both substitute into. |
| [OneK8s-hello](https://github.com/olljanat-ai/OneK8s-hello) | What is deployed: the example applications, their source, their Dockerfiles and their charts. **Both** delivery planes deploy these, unchanged. |

## The comparison, in one table

Both planes deploy the *same chart* (`apps/hello/chart` in OneK8s-hello) to the
*same two clusters*. Everything else differs, and that is the experiment.

| | Argo CD (`OneK8s-argocd`) | Flux (here) |
|---|---|---|
| Topology | **Hub and spoke.** Argo CD runs on AKS; EKS is registered as a spoke with a cluster `Secret` the `gitops` stack writes. | **Independent.** Each cluster runs its own Flux and reads this repository directly. Neither knows the other exists. |
| Fan-out | One `ApplicationSet`; a cluster generator turns it into one `Application` per matching cluster. | None. Each cluster has its own `Kustomization` pointing at the same `apps/hello2`. |
| Per-cluster values | Helm parameters rendered by the platform chart on the hub. | `${VARIABLE}` substitution at reconcile time, from `cluster-vars` (written by Terraform) and the cluster's own release ConfigMap. |
| What runs where | Kargo: a `Warehouse` freezes each build as Freight, staging takes it automatically, production takes only what staging ran, when a person promotes. | A pull request editing `clusters/<cloud>/hello2-release.yaml`. No Warehouse, no Freight, no policy. |
| Blast radius of the delivery plane failing | The hub is a single point of delivery for every cluster. | One cluster stops reconciling; the other never notices. |
| Tenant boundary | `AppProject`: allowed repositories, one namespace, no cluster-scoped resources. | The controllers run cluster-wide (see [Known gaps](#known-gaps)). |
| Cost of "deploy this everywhere" | One commit. | One commit per cluster. |

The visible half of the experiment is two URLs per cluster:

| Cluster | Argo CD delivers | Flux delivers |
|---|---|---|
| AKS (`azure`) | `https://azure-hello.onek8s.lol` — tenant `team-alpha` | `https://azure-hello2.onek8s.lol` — tenant `team-beta` |
| EKS (`aws`) | `https://aws-hello.onek8s.lol` — tenant `team-alpha` | `https://aws-hello2.onek8s.lol` — tenant `team-beta` |

Same application, same page, different delivery plane and different tenant. The
`A` records are created out of band, like every other host on this platform.

## Layout

```
apps/
└── hello2/                     ONE definition, reconciled by both clusters
    ├── gitrepository.yaml        the chart's source: OneK8s-hello, at a commit
    ├── helmrelease.yaml          the release, with every per-cluster value as ${VAR}
    └── kustomization.yaml
clusters/
├── azure/                      what the AKS cluster runs...
│   ├── hello2-release.yaml       ...which build and which chart commit  ← a release is an edit here
│   ├── hello2.yaml               the Kustomization: path + where the variables come from
│   └── kustomization.yaml        the only path Terraform points Flux at
└── aws/                        the same three files, and no relationship to azure/
platform-contract.yaml          what Terraform promises to put in "cluster-vars"
```

Nothing under `apps/` names a cloud — CI asserts it against the rendered
output. Anything that differs between the two clusters is either a variable or
a file under `clusters/`.

## How a cluster gets here

Terraform, in the OneK8s repository (`modules/fluxcd`, used by
`foundations/azure` and `foundations/aws`), does exactly three things per
cluster and then gets out of the way:

```
modules/fluxcd
  ├── Helm release  flux2            the controllers
  ├── ConfigMap     cluster-vars     the cluster's own facts (platform-contract.yaml)
  └── Helm release  flux-system      GitRepository → this repository
                                     Kustomization → ./clusters/<cloud>
```

That is the whole bootstrap. It is the counterpart of the single Argo CD root
`Application` that `gitops/root-app.tf` plants on the hub — and, like it, after
the first apply everything is Git.

## Making a release

A build reaches a cluster when somebody says so, in a pull request:

1. Find the build. The OneK8s-hello build workflow publishes one immutable
   `sha-<short>` tag per merge and no moving tag at all.
2. Edit that cluster's `clusters/<cloud>/hello2-release.yaml` —
   `HELLO2_IMAGE_TAG`, and `HELLO2_CHART_REVISION` if the chart itself moved.
3. Open a pull request. CI renders both clusters and checks the result.
4. On merge, that cluster's Flux picks it up within its `interval` (5m), or at
   once with `flux reconcile kustomization hello2 --with-source`.

Moving one cluster moves nothing else. Promoting "from azure to aws" is
copying two lines between two files, and nothing enforces that the second one
ever happens — which is precisely the difference this repository exists to
demonstrate against Kargo's `Stage`, where production can *only* run what
staging has already run.

## Onboarding another application

1. `apps/<name>/` — a `GitRepository` (or `HelmRepository`/`OCIRepository`) and
   a `HelmRelease`, with every per-cluster value written as `${VARIABLE}` and
   nothing naming a cloud.
2. `clusters/<cloud>/<name>-release.yaml` — what that cluster runs.
3. `clusters/<cloud>/<name>.yaml` — a `Kustomization` for `./apps/<name>`,
   substituting from `cluster-vars` and `<name>-release`.
4. Add it to `clusters/<cloud>/kustomization.yaml`, once per cluster it should
   run on.

Steps 2–4 are per cluster, which is the honest cost of having no hub. A new
platform variable is a change here *and* in `modules/fluxcd`; declare it in
`platform-contract.yaml` and CI will hold both ends to it.

## CI

`.github/validate.py` (run by PR Validation, and runnable locally with nothing
but `kustomize` and PyYAML) renders every cluster the way its own Flux would:
it builds `clusters/<cloud>`, collects the variables that will be in scope
there — the contract's examples plus every ConfigMap the overlay applies —
builds each Kustomization's path, and substitutes.

It fails on:

- a `${VARIABLE}` nobody supplies (on a cluster this is not an error anywhere,
  it is a Kustomization that silently stops applying);
- a `substituteFrom` naming a ConfigMap that neither Terraform writes nor the
  overlay applies;
- a Kustomization that does not prune, or reads a source that does not exist;
- an object landing outside the tenant's namespace;
- a host that is not `<cloud>-<app>.<domain>`;
- a moving image tag, or a chart pinned to a branch instead of a commit;
- anything under `apps/` naming a cloud.

## Known gaps

- **The controllers run privileged.** The `flux2` chart's multi-tenancy
  lockdown is off, so `kustomize-controller` and `helm-controller` hold
  `cluster-admin`: anything committed here can do anything to the cluster.
  Argo CD's side of the comparison has a real boundary (an `AppProject`
  allowing two repositories, one namespace and no cluster-scoped resources).
  Closing it means `spec.serviceAccountName` on every Kustomization plus a
  tenant ServiceAccount with deploy rights in its own namespace, which the
  tenants stack does not grant today.
- **No notifications.** `notification-controller` is installed but nothing
  configures an `Alert` or a `Provider`, so a failed reconcile is visible only
  to somebody running `flux get`.
- **No image automation.** The image-reflector and image-automation controllers
  are not installed at all (`enable_image_automation = false` in
  `modules/fluxcd`); discovering builds and writing them back to Git is the
  feature to turn on if this side is ever to answer Kargo on its own terms.
- **The repository is public and read-only to the clusters.** No credential is
  configured, and nothing here ever pushes.
