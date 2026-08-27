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
| Tenant boundary | `AppProject`: allowed repositories, one namespace, no cluster-scoped resources. | Multi-tenancy is enforced — Flux objects stay in one namespace, deployment happens as `flux-applier` — but that account has full access (see [Known gaps](#known-gaps)). |
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
    ├── helmrelease.yaml          the release: every per-cluster value as ${VAR},
    │                             deployed into ${TENANT} as ${FLUX_APPLIER}
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

Terraform, in the OneK8s repository, and **not the same way on every cluster**.
AKS is what the real environments run, so Flux there is the Azure-managed
`microsoft.flux` extension — Azure owns the manifests, the upgrades and the
patching, exactly as it does for the Argo CD extension. The other clouds
install the same delivery plane from the community chart, and are where the
platform proves it is not tied to Azure.

```
AKS            modules/fluxcd-aks           EKS (and GKE/OKE)  modules/fluxcd
  extension  microsoft.flux                   Helm release  flux (flux2 chart)
                                              ServiceAccount flux-applier
                                                             (+ cluster-admin)
  ConfigMap  cluster-vars   ◀── the same module, so one contract ──▶  cluster-vars
  fluxConfiguration                           Helm release  flux-system
    GitRepository flux-system                   GitRepository flux-system
    Kustomization → ./clusters/azure            Kustomization → ./clusters/aws
```

Both produce the same two objects under the same names, which is what lets one
directory here serve both. That is the whole bootstrap, and it is the
counterpart of the single Argo CD root `Application` that `gitops/root-app.tf`
plants on the hub — after the first apply, everything is Git.

### Why the objects are laid out the way they are

The AKS extension enforces Flux's **multi-tenancy** by default, and this
repository is written for it rather than opting out:

- **Every Flux object of an application lives in `flux-system`** — its
  `GitRepository`, its `Kustomization`, its `HelmRelease` — because a
  `HelmRelease` in the tenant's namespace would be refused there (no applier
  account exists in it) and its `sourceRef` would be crossing a namespace
  besides.
- **The workload still lands in the tenant's namespace**, through the
  `HelmRelease`'s `targetNamespace`.
- **Everything deploys as `flux-applier`**, the ServiceAccount the extension
  creates and impersonates. `modules/fluxcd` creates one of the same name on
  the other clouds, so `serviceAccountName: flux-applier` means the same thing
  on either install.

None of that costs the community-chart install anything, which is why it is the
shape everywhere rather than an Azure special case.

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
   a `HelmRelease`, both in `${FLUX_NAMESPACE}`, with the workload placed by
   `targetNamespace: ${TENANT}`, deploying as `${FLUX_APPLIER}`, every
   per-cluster value written as a `${VARIABLE}`, and nothing naming a cloud.
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
- a Flux object outside `flux-system`, or a `sourceRef` crossing a namespace —
  both refused under the AKS extension's multi-tenancy;
- a `HelmRelease` whose `targetNamespace` is not the tenant's, or that deploys
  as an account the platform does not provide;
- a host that is not `<cloud>-<app>.<domain>`;
- a moving image tag, or a chart pinned to a branch instead of a commit;
- anything under `apps/` naming a cloud.

## Known gaps

- **The applier has full access.** Multi-tenancy is enforced, so nothing
  deploys as a controller — but the account it deploys as, `flux-applier`, is
  cluster-scoped (Azure's own word for that scope is *full access*), because
  one configuration in `flux-system` has to reach a tenant's namespace.
  Anything committed here can therefore still do anything to the cluster, where
  Argo CD's side has a real boundary in its `AppProject`. The shape that closes
  it is now one step away rather than a redesign: a **namespace-scoped**
  configuration per tenant — Flux objects in the tenant's own namespace, an
  applier bound only there — which also needs a tenant ServiceAccount with
  deploy rights that the tenants stack does not grant today.
- **No notifications configured.** The AKS extension installs
  `notification-controller` (it is not optional there) and the community chart
  is told not to (`enable_notifications = false`), but neither cluster has an
  `Alert` or a `Provider` — so a failed reconcile is visible only to somebody
  running `flux get`. Configuring one is the first thing to do if this plane
  ever carries something that matters.
- **No image automation.** The image-reflector and image-automation controllers
  are not installed on either cluster (`enable_image_automation = false` in
  `modules/fluxcd`; not enabled in the AKS extension either); discovering builds
  and writing them back to Git is the feature to turn on if this side is ever to
  answer Kargo on its own terms.
- **The repository is public and read-only to the clusters.** No credential is
  configured, and nothing here ever pushes.
