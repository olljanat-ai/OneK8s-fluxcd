#!/usr/bin/env python3
"""Render every cluster of this repository the way its Flux would, and assert
the properties the repository exists to guarantee.

Run it locally exactly as CI does — it needs nothing but kustomize and PyYAML:

    python3 .github/validate.py

What it does, per directory under clusters/:

  1. kustomize build clusters/<cloud>            — the cluster's own overlay
  2. collect the variables that will be in scope on that cluster:
       - platform-contract.yaml's examples for <cloud>   (cluster-vars, which
         Terraform writes and this repository never sees)
       - the data of every ConfigMap the overlay itself applies
  3. for every Flux Kustomization the overlay applies:
       kustomize build <its path>, then substitute those variables the way
       postBuild does — and fail on any variable nobody supplies
  4. assert what the rendered objects have to say

Failures are printed as GitHub annotations on the file that has to change.
"""

import pathlib
import re
import subprocess
import sys

import yaml

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
IMAGE_TAG = re.compile(r"^sha-[0-9a-f]{7,40}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

status = 0


def error(where, message):
    global status
    print(f"::error file={where}::{message}")
    status = 1


def build(path):
    """kustomize build, as a list of objects."""
    out = subprocess.run(
        ["kustomize", "build", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        error(f"{path}/kustomization.yaml",
              f"kustomize build failed: {out.stderr.strip()}")
        return []
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def substitute(rendered, variables, where):
    """What Flux's postBuild does, and what it does when a variable is missing.

    Flux fails the whole Kustomization rather than applying a manifest with a
    "${...}" left in it, so this does too — reporting every missing variable at
    once, since fixing them one build at a time is nobody's idea of a good
    afternoon.
    """
    missing = sorted({
        name for name in VARIABLE.findall(rendered) if name not in variables
    })
    for name in missing:
        error(f"{where}/kustomization.yaml",
              f"{where} uses ${{{name}}}, which nothing supplies. Either add it "
              "to platform-contract.yaml (and make modules/fluxcd in the OneK8s "
              "repository write it into cluster-vars), or define it in the "
              "cluster's own release ConfigMap. Unsupplied, this stops the "
              "Kustomization on the cluster: 'variable substitution failed: "
              f"variable not set: {name}'.")
    return VARIABLE.sub(lambda m: variables.get(m.group(1), m.group(0)), rendered)


def render(path, variables, where):
    out = subprocess.run(
        ["kustomize", "build", str(path)], capture_output=True, text=True,
    )
    if out.returncode != 0:
        error(f"{where}/kustomization.yaml",
              f"kustomize build failed: {out.stderr.strip()}")
        return [], ""
    raw = out.stdout
    substituted = substitute(raw, variables, where)
    return [doc for doc in yaml.safe_load_all(substituted) if doc], raw


root = pathlib.Path(__file__).resolve().parent.parent
contract = yaml.safe_load((root / "platform-contract.yaml").read_text())["clusterVars"]
clusters = sorted(p.name for p in (root / "clusters").iterdir() if p.is_dir())

if not clusters:
    error("clusters", "no cluster reconciles this repository.")

for cloud in clusters:
    print(f"\n=== {cloud}")
    overlay = root / "clusters" / cloud
    objects = build(overlay)

    # The half of the variables the platform supplies. Absent from this
    # repository on the cluster's side too — Terraform writes it — so the
    # contract's examples are what stands in for it here.
    variables = {}
    for name, spec in contract.items():
        examples = spec.get("examples", {})
        if cloud not in examples:
            error("platform-contract.yaml",
                  f"{name} has no example for the {cloud} cluster, so nothing "
                  "here can render that cluster.")
            continue
        variables[name] = str(examples[cloud])

    # ...and the half this repository commits, beside the cluster it belongs to.
    committed = {
        obj["metadata"]["name"]: {k: str(v) for k, v in (obj.get("data") or {}).items()}
        for obj in objects if obj["kind"] == "ConfigMap"
    }
    for data in committed.values():
        variables.update(data)

    kustomizations = [obj for obj in objects if obj["kind"] == "Kustomization"]
    if not kustomizations:
        error(f"clusters/{cloud}/kustomization.yaml",
              f"the {cloud} cluster applies no Kustomization, so it deploys "
              "nothing at all.")

    for kustomization in kustomizations:
        name = kustomization["metadata"]["name"]
        spec = kustomization["spec"]
        where = f"clusters/{cloud}/{name}.yaml"

        # Every ConfigMap this Kustomization substitutes from is either the
        # platform's (cluster-vars, not in this repository) or one the same
        # overlay applies. A name that is neither is a Kustomization that will
        # never reconcile.
        for source in spec.get("postBuild", {}).get("substituteFrom", []):
            if source["kind"] != "ConfigMap":
                continue
            if source["name"] == "cluster-vars" or source["name"] in committed:
                continue
            error(where,
                  f"{name} substitutes from ConfigMap {source['name']}, which "
                  f"neither Terraform writes (cluster-vars) nor clusters/{cloud} "
                  "applies. Flux waits for it for ever.")

        if not spec.get("prune"):
            error(where,
                  f"{name} does not prune. An object dropped from {spec['path']} "
                  "would be left running on the cluster with nothing tracking it.")

        if spec.get("sourceRef", {}).get("name") != "flux-system":
            error(where,
                  f"{name} reads source {spec.get('sourceRef', {}).get('name')!r}, "
                  "but the only source on the cluster is the 'flux-system' "
                  "GitRepository that modules/fluxcd bootstraps.")

        # Repo-relative, because that is what a GitHub annotation can point at.
        app_dir = spec["path"].lstrip("./").rstrip("/")
        docs, raw = render(root / app_dir, variables, app_dir)

        # The property that makes this the "independent clusters" model rather
        # than two copies of one application: the definition is shared, and
        # nothing in it names a cloud. Checked against the rendered output, so
        # the comments explaining the design do not count.
        for other in clusters:
            if re.search(rf"\b{other}\b", raw):
                error(f"{app_dir}/kustomization.yaml",
                      f"{app_dir} names the cloud {other!r}. Both clusters "
                      "reconcile this same directory; anything that differs "
                      "between them belongs in a variable, not in a copy.")

        for doc in docs:
            kind = doc["kind"]
            namespace = doc["metadata"].get("namespace")
            if namespace != variables["TENANT"]:
                error(f"{app_dir}/{kind.lower()}.yaml",
                      f"{kind} {doc['metadata']['name']} lands in namespace "
                      f"{namespace!r}, not in the tenant's ({variables['TENANT']}). "
                      "Only the tenant's namespace has the quota, the "
                      "NetworkPolicy and the SecretStore this application needs.")

            if kind == "GitRepository":
                commit = doc["spec"].get("ref", {}).get("commit", "")
                if not FULL_SHA.match(commit):
                    error(f"clusters/{cloud}/{name}-release.yaml",
                          f"{doc['metadata']['name']} checks out {commit!r}, which "
                          "is not a full commit SHA. A branch here re-deploys "
                          "every chart change the moment it merges, which is "
                          "the one thing pinning a release is for.")

            if kind == "HelmRelease":
                values = doc["spec"].get("values", {})
                host = values.get("ingress", {}).get("host", "")
                wanted = f"{cloud}-{doc['metadata']['name']}.{variables['DOMAIN']}"
                if host != wanted:
                    error(f"{app_dir}/helmrelease.yaml",
                          f"{doc['metadata']['name']} publishes {host!r}; the "
                          f"{cloud} cluster's host for it is {wanted!r}. Hosts are "
                          "one label deep under the platform wildcard, prefixed "
                          "with the cloud, or the certificate does not cover them.")

                tag = str(values.get("image", {}).get("tag", ""))
                if not IMAGE_TAG.match(tag):
                    error(f"clusters/{cloud}/{name}-release.yaml",
                          f"{doc['metadata']['name']} runs image tag {tag!r}. The "
                          "build workflow publishes one immutable 'sha-<short>' "
                          "tag per build and no moving tag; anything else here "
                          "either does not exist or does not stay the same build.")

                if values.get("tenant") != variables["TENANT"]:
                    error(f"{app_dir}/helmrelease.yaml",
                          f"{doc['metadata']['name']} is labelled for tenant "
                          f"{values.get('tenant')!r} but deployed into "
                          f"{variables['TENANT']!r}.")

                print(f"  {doc['metadata']['name']:10} host={host} "
                      f"tag={tag} namespace={namespace}")

# One shared definition is the claim this repository makes about itself; the
# cheapest way for it to stop being true is a second copy of apps/hello2 that
# only one cluster reads.
app_paths = {
    kustomization["spec"]["path"]
    for cloud in clusters
    for kustomization in build(root / "clusters" / cloud)
    if kustomization["kind"] == "Kustomization"
}
for path in sorted(app_paths):
    readers = [
        cloud for cloud in clusters
        for k in build(root / "clusters" / cloud)
        if k["kind"] == "Kustomization" and k["spec"]["path"] == path
    ]
    print(f"\n{path} is reconciled by: {', '.join(readers)}")

sys.exit(status)
