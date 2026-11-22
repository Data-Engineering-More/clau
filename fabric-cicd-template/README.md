# Microsoft Fabric CI/CD template for Azure DevOps

Deploys Microsoft Fabric items (Lakehouse, Notebooks, Data Pipelines) across **DEV → TEST → PROD** with Microsoft's [`fabric-cicd`](https://microsoft.github.io/fabric-cicd/) library. It has validation at every stage and approval gates before TEST and PROD.

```
feature/*  ──PR──▶ develop ──▶ Build ──▶ DEV
                     ▲ pr-validation runs on every PR
develop    ──PR──▶ main    ──▶ Build ──▶ [gate] ──▶ TEST ──▶ [gate] ──▶ PROD ──▶ tag
hotfix/*   ──PR──▶ main    ──▶ same, then merge main back into develop
```

## Files

```
<your-repo>/                         copy the contents of this folder to your repo root
├── pipelines/
│   ├── cd-pipeline.yml              deploys: develop → DEV, main → TEST → PROD.  ← CONFIGURE ME block
│   ├── pr-validation.yml            PR checks only, never deploys
│   └── templates/
│       ├── build-stage.yml          validation + packaging (shared by both pipelines)
│       └── deploy-stage.yml         gate → preflight → deploy → smoke → post-deploy test → tag
├── scripts/
│   ├── validate.py                  static checks on the Fabric items
│   ├── dry_run.py                   offline deploy against a fake workspace
│   ├── check_config.py              sanity-checks the pipeline settings
│   ├── deploy.py                    preflight + fabric-cicd publish + smoke check
│   └── run_item.py                  runs a pipeline/notebook after deploy and waits for success
├── requirements.txt                 fabric-cicd pinned to 1.2.0
└── workspace/                       ← your Fabric Git folder
    ├── parameter.yml                DEV → TEST/PROD ID mapping                    ← fill in
    └── <your items>.Lakehouse / .Notebook / .DataPipeline
```

## Validation and gates: what runs where

| # | Check | PR | develop → DEV | main → TEST/PROD | Fails on |
|---|---|:-:|:-:|:-:|---|
| 1 | **Branch rules** | ✅ | | | `main` only accepts `develop`/`hotfix/*`; `develop` accepts `feature/*`, `fix/*`, `bugfix/*`, `hotfix/*`, `main`. Warns on names not like `feature/123-desc` |
| 2 | **Config check** | | ✅ | ✅ | Workspace IDs not real GUIDs; **two environments sharing a workspace**; a gate on with no approvers |
| 3 | **Static validation** (7 checks) | ✅ | ✅ | ✅ | Missing/invalid `.platform`; `/` in names; duplicate names or logicalIds (copied folders); invalid JSON; `parameter.yml` gaps, bad regex, placeholders; pipeline activity pointing at a deleted notebook; **committed secrets** (keys, SAS, passwords, private keys). Warns on unmapped GUIDs and notebook syntax errors |
| 4 | **Offline dry-run deploy** | ✅ | ✅ | ✅ | Real `fabric-cicd` deploys to a fake TEST and PROD workspace. Fails if `fabric-cicd` rejects anything, **any DEV ID survives**, a notebook's lakehouse doesn't point at the target, or an item isn't published |
| 5 | Test results → **Tests tab** | ✅ | ✅ | ✅ | — |
| 6 | **Approval gate** | | | ✅ TEST and PROD | Rejected, or nobody approves within `approvalTimeoutMinutes` |
| 7 | **Preflight** | | ✅ | ✅ | Service principal can't open the workspace, or it has no capacity |
| 8 | **Deploy + smoke check** | | ✅ | ✅ | Any repo item missing in the workspace afterwards |
| 9 | **Post-deploy test** (optional) | | | ✅ TEST | The chosen pipeline/notebook fails, is cancelled or times out in TEST |
| 10 | Release tag | | | ✅ PROD | — |

PROD always deploys the **same artefact** that passed TEST.

## 1. Configure

### `pipelines/cd-pipeline.yml`: the `CONFIGURE ME` block
Search for `>>> CHANGE`.

| Setting | Example | Where to find it |
|---|---|---|
| `group:` | `vargrp-fabric-deploy` | The variable group you create in step 2 |
| `projectSlug` | `sales-reporting` | Your choice: lower case, no spaces |
| `devWorkspaceId` / `testWorkspaceId` / `prodWorkspaceId` | `'xxxxxxxx-xxxx-…'` | Open the workspace in Fabric; it's the GUID after `/groups/` in the address bar |
| `testApprovalGate` / `prodApprovalGate` | `'true'` | `'false'` turns that gate off |
| `testApprovers` / `prodApprovers` | `'a@org.com, [MyProject]\Release Approvers'` | Emails and/or Azure DevOps groups. **Keep the single quotes** |
| `allowSelfApproval` | `'false'` | `'false'` = the person who triggered the run can't approve it. Teams of 2–3: set `'true'` or list several approvers |
| `approvalTimeoutMinutes` | `1440` | Unanswered gate is rejected after this (max 4320) |
| `testPostDeployItem` (optional) | `'PL_Main'` | A pipeline or notebook that is **safe to run in TEST**. `''` = off |
| `fabricFolder`, `agentImage`, `pythonVersion` | defaults | Change only if needed. Keep `pr-validation.yml` in sync |

### `workspace/parameter.yml`
| Placeholder | Where to find it |
|---|---|
| `<LAKEHOUSE_NAME>` | Lakehouse display name exactly as in Fabric |
| `<DEV_LAKEHOUSE_ID>` | Any `*.Notebook/notebook-content.py` → `# META "default_lakehouse": "…"` |
| `<DEV_WORKSPACE_ID>` | Same file → `"default_lakehouse_workspace_id": "…"` |
| Blocks 5–6 (optional) | Connections, or lakehouses in other workspaces |

### `scripts/deploy.py` and `scripts/validate.py` (optional)
`ITEM_TYPES` (item types to deploy), `EXCLUDE_REGEX` (names never deployed, default `*_dev`), and `ENVIRONMENTS` (must match the `parameter.yml` keys).

## 2. One-time setup in Azure DevOps and Fabric

1. **Service principal.**
   - Create an app registration and a client secret.
   - Fabric admin portal: enable *Service principals can use Fabric APIs* for a group that contains it.
   - Make it **Contributor** on all three workspaces.
2. **Variable group** (Pipelines → Library): `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` 🔒.
3. **Environments** (Pipelines → Environments): `<projectSlug>-dev`, `-test`, `-prod`. Add an **Exclusive lock** check on each, so two runs can't deploy at once.
4. **Pipelines.**
   - New pipeline → Existing YAML → `/pipelines/cd-pipeline.yml`.
   - New pipeline → `/pipelines/pr-validation.yml`.
   - Click **Validate** in the editor for each, then Save.
   - On the first run, authorise the variable group and environments.
5. **Branch policies on `develop` and `main`.**
   - Require a PR; minimum 1 reviewer; linked work item; comment resolution.
   - **Build validation = `pr-validation`, required.**
   - Merge type: squash into `develop`; **merge commit (no fast-forward)** into `main`.
6. **Repo permissions.**
   - Developers: Contribute and Create branch.
   - *<Project> Build Service*: Contribute and **Create tag**.
7. **Seed once.**
   - Connect the existing DEV workspace to `develop` (Git folder = `fabricFolder`), commit all items, then merge into `main`.
   - **Disconnect DEV from Git.** The pipeline deploys it from then on.

### About the approval gates
The gates are written in YAML (an agentless `ManualValidation@1` job), so they're visible and reviewed like code. Approvers get an email and approve from the run page. You can *also* add an **Approvals** check on the `-test`/`-prod` environments in the Azure DevOps UI. That check is enforced even if someone edits the YAML, but it means two approvals per stage. Pick one, or both for PROD.

## 3. Test it end to end (first run)

| Step | Do | Expect |
|---|---|---|
| 1 | Run `pr-validation` manually on a feature branch (step 1 only runs on real PRs) | Steps 3–5 green; results in the **Tests** tab |
| 2 | Raise a PR `feature/…` → `develop` | `pr-validation` runs as a required check |
| 3 | Try a PR `feature/…` → `main` | Blocked by the branch rule |
| 4 | Merge the PR into `develop` | Build, then `Deploy_DEV`: preflight OK, items published, smoke check OK |
| 5 | Raise and merge a PR `develop` → `main` | Build, then the **TEST gate** emails the approvers |
| 6 | Approve | TEST deploys (and the post-deploy test runs if set) |
| 7 | **PROD gate** emails the approvers. Reject it once to see a clean stop, then re-run the stage and approve | PROD deploys and the tag `release/<slug>_<date>.<n>` appears |
| 8 | Check each workspace | Notebooks point at that workspace's own lakehouse |

**Things to try on purpose to prove the checks work:**
- Leave a placeholder in `parameter.yml`.
- Paste a fake `AccountKey=…` into a notebook.
- Set `prodWorkspaceId` to the TEST ID.
- Delete a notebook that a pipeline still calls.

Each one should fail the Build stage with a clear message.

## 4. Daily workflow

Developers work in their **own** Fabric workspace, connected to the repo:
1. Create `feature/<work-item-id>-<desc>` from `develop`.
2. In the workspace: Source control → Branches → **Switch branch** (commit first).
3. Attach notebooks to the **DEV** lakehouse.
4. Commit, then raise a PR into `develop`. Once merged, DEV deploys.

**Release:** PR `develop` → `main`, then TEST gate, TEST, UAT, PROD gate, PROD.
**Hotfix:** branch from `main` → PR into `main` → then merge `main` into `develop`.
**Rollback:** revert the release merge commit on `main` with a PR. TEST and PROD redeploy the previous code. Data is not rolled back.

## Good to know
- **Not deployed:** lakehouse data, connections, schedules and permissions. Run your pipeline once after the first deploy to each workspace.
- **Lakehouses are never deleted**, even with *Remove items deleted from the repo* on.
- **Dry-run limits:** it simulates the Fabric API for Lakehouse, Notebook and Data Pipeline items. If you add item types or bump `fabric-cicd` and it reports *calls the dry run does not simulate*, extend `FakeFabric` in `dry_run.py`.
- **No hand edits:** nobody edits DEV, TEST or PROD by hand. The next deployment overwrites portal changes.
