# Microsoft Fabric CI/CD template for Azure DevOps (trunk-based)

Deploys Microsoft Fabric items (Lakehouse, Notebooks, Data Pipelines) to **DEV → TEST → PROD** using Microsoft's [`fabric-cicd`](https://microsoft.github.io/fabric-cicd/) library. There is one long-lived branch (`main`), an approval gate before TEST and before PROD, and step-by-step logging.

```
feature/1234-desc ──PR──▶ main ──▶ Build ──▶ DEV ──▶ [approve] TEST ──▶ [approve] PROD ──▶ tag
       ▲                    │       automatic   automatic   "DEV is fine"      "UAT signed off"
       └ developer's own    └ pr-validation runs on every PR (never deploys)
         Fabric workspace
```

## How a release works

1. A developer works in **their own Fabric workspace** on `feature/<work-item>-<desc>`, created from `main`.
2. They raise a **PR into `main`**. `pr-validation` checks it, and the PR's Summary tab lists the Fabric items and commits it changes.
3. After review, they squash-merge. **`cd-pipeline` starts automatically:**
   - **Build** validates, dry-runs, and writes the **release preview** to the Summary tab: everything changed since the last PROD release.
   - **DEV** deploys automatically.
   - The **TEST gate** emails the TEST approvers. The run waits while you check the DEV workspace.
   - **Approve** → TEST deploys. Then the **PROD gate** emails the PROD approvers.
   - **Approve** (after UAT/CAB) → PROD deploys **the same artefact**, and the commit is tagged `release/<run name>`.
4. **Reject** at either gate to stop. Fix the problem with a new PR, which starts a new run.

**Hotfix:** same flow, on a branch named `hotfix/<id>-<desc>`.
**Rollback:** revert the bad merge commit on `main` with a PR. The pipeline redeploys the previous code (code only, not data).

## Files

```
<your-repo>/
├── pipelines/
│   ├── cd-pipeline.yml            main → DEV → [gate] TEST → [gate] PROD → tag.   ← CONFIGURE ME
│   ├── pr-validation.yml          PR checks only, never deploys, no secrets
│   └── templates/
│       ├── build-stage.yml        validation, dry run, release preview, artefact
│       └── deploy-stage.yml       gate → deploy (5 logged phases) → post-deploy test → logs → tag
├── scripts/
│   ├── pipeline_log.py            log sections, collapsible groups, Summary-tab reports, run tags
│   ├── validate.py                static checks on the Fabric items
│   ├── dry_run.py                 offline deploy with the real fabric-cicd against a fake workspace
│   ├── check_config.py            pipeline settings: workspace IDs real and distinct, approvers set
│   ├── release_preview.py         what's changing since the last PROD release (or in a PR)
│   ├── deploy.py                  preflight → plan → publish → clean-up → smoke check, with report
│   └── run_item.py                optional: run a pipeline/notebook after deploy and wait for success
├── requirements.txt               fabric-cicd pinned to 1.2.0
└── workspace/                     ← Fabric Git folder
    ├── parameter.yml              DEV → TEST/PROD ID mapping                          ← fill in
    └── <items>.Lakehouse / .Notebook / .DataPipeline
```

## What you see in Azure DevOps (logging)

| Where | What |
|---|---|
| **Stage view** | `Build` → `DEV` → `TEST` (Approve / Deploy) → `PROD` (Approve / Deploy). Waiting gates show **Review**, with the instructions and Approve/Reject buttons |
| **Step log** | Each deploy is split into green sections: `1/5 Preflight`, `2/5 Deployment plan` (a table of every item: Create, Update or Skip), `3/5 Publish` (fabric-cicd output, collapsible), `4/5 Remove deleted items`, `5/5 Smoke check` (a table with OK/MISSING). Every line is timestamped |
| **Summary tab** | Release preview (items, commits, work items, warnings), then one report per environment: result, duration, and the item table with Create/Update and OK/MISSING |
| **Tests tab** | Static validation and dry-run results, one test per check or environment |
| **Warnings/errors** | Shown at the top of the run, e.g. *"'X.Notebook' is deleted in this release"*, *"commit abc123 has no linked work item"* |
| **Artefacts** | `logs-DEV-1`, `logs-TEST-1`, `logs-PROD-1`: the full deploy log, the report, and fabric-cicd's error log if it failed |
| **Run tags** | `deployed-dev`, `deployed-test`, `deployed-prod`, `released`. Filter the runs list by these |
| **Environments page** | Deployment history per environment: which run and commit is in DEV, TEST and PROD right now |

## Validation: what runs where

| Check | PR | Merge to main | Fails on |
|---|:-:|:-:|---|
| Branch rules | ✅ | | Only `feature/*`, `fix/*`, `bugfix/*` and `hotfix/*` can merge into `main`. Warns if the name has no work-item ID |
| Config check | | ✅ | Workspace IDs are placeholders, not GUIDs, or **two environments share a workspace**; a gate is on with no approvers |
| Static validation | ✅ | ✅ | Bad `.platform`; `/` in a name; duplicate names or IDs; invalid JSON; `parameter.yml` gaps or placeholders; a pipeline calling a deleted notebook; **committed secrets**. Warns on unmapped GUIDs and notebook syntax errors |
| Offline dry run | ✅ | ✅ | fabric-cicd rejects anything, **a DEV ID survives**, a notebook isn't re-pointed, or an item isn't published |
| Release preview | ✅ | ✅ | Never fails; informational |
| Approval gates | | ✅ | Rejected, or not answered within the timeout |
| Preflight | | ✅ | Service principal can't open the workspace, or it isn't on a capacity |
| Smoke check | | ✅ | Any repo item missing in the workspace after deploy |
| Post-deploy test (optional) | | ✅ | The chosen pipeline/notebook fails, is cancelled or times out |

## 1. Configure

### `pipelines/cd-pipeline.yml`: the `CONFIGURE ME` block (search `>>> CHANGE`)

| Setting | Example | Notes |
|---|---|---|
| `group:` | `vargrp-fabric-deploy` | The variable group from step 2 below |
| `projectSlug` | `sales-reporting` | Lower case, no spaces. Used for environment names, run names and tags |
| `devWorkspaceId` / `testWorkspaceId` / `prodWorkspaceId` | `'xxxxxxxx-…'` | The GUID after `/groups/` in the Fabric address bar. Keep the quotes |
| `testApprovalGate` / `prodApprovalGate` | `'true'` | `'false'` removes that gate |
| `testApprovers` / `prodApprovers` | `'a@org.com, [MyProject]\Release Approvers'` | Emails and/or Azure DevOps groups. **Keep the single quotes** |
| `allowSelfApproval` | `'false'` | `'false'` = whoever merged can't approve. Teams of 2–3: set `'true'` or list two or more approvers |
| `approvalTimeoutMinutes` | `'4200'` | Gate rejects after this (max 4200, about 3 days). Later, use **Rerun stage** on the rejected stage; it redeploys the same artefact |
| `devPostDeployItem` / `testPostDeployItem` | `'PL_Main'` | Optional: a pipeline or notebook to run after deploy, **only if safe to run there**. `''` = off |
| `fabricFolder`, `agentImage`, `pythonVersion` | defaults | Keep `pr-validation.yml` in sync |

### `workspace/parameter.yml`

| Placeholder | Where to find it |
|---|---|
| `<LAKEHOUSE_NAME>` | Lakehouse display name exactly as in Fabric |
| `<DEV_LAKEHOUSE_ID>` | Any `*.Notebook/notebook-content.py` → `# META "default_lakehouse": "…"` |
| `<DEV_WORKSPACE_ID>` | Same file → `"default_lakehouse_workspace_id": "…"` |
| Blocks 5–6 (optional) | Connections, or lakehouses in other workspaces |

### Scripts (optional)
- `deploy.py`: `ITEM_TYPES` (item types to deploy) and `EXCLUDE_REGEX` (names never deployed; default `*_dev`).
- `validate.py`: `ENVIRONMENTS` must match the `parameter.yml` keys.

## 2. One-time setup

1. **Service principal.**
   - Create an app registration and a client secret.
   - Fabric admin portal: enable *Service principals can use Fabric APIs* for a security group that contains it.
   - Make it **Contributor** on the DEV, TEST and PROD workspaces.
2. **Variable group** (Pipelines → Library): `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` 🔒.
3. **Environments** (Pipelines → Environments): `<projectSlug>-dev`, `-test`, `-prod`.
   - On each, go to Approvals and checks → **Exclusive lock**. With `lockBehavior: runLatest`, if two merges overlap only the newest run deploys.
4. **Pipelines.**
   - New pipeline → Azure Repos Git → Existing YAML → `/pipelines/cd-pipeline.yml`. Click **Validate**, then Save. Rename it `cd-pipeline`.
   - Same again for `/pipelines/pr-validation.yml`, renamed `pr-validation`.
5. **Branch policy on `main`.**
   - Require a PR; minimum 1 reviewer; linked work item; comment resolution.
   - **Build validation = `pr-validation`, required.**
   - Merge type: **squash** (one commit per change, so the release preview stays readable).
6. **Repo permissions.**
   - Developers: Contribute and Create branch.
   - *<Project> Build Service*: **Contribute** and **Create tag**.
7. **Seed once.**
   - Connect the current DEV workspace to `main` (Git folder = `fabricFolder`) and commit all items.
   - **Disconnect DEV from Git.** From then on the pipeline deploys DEV.
8. **Developers.**
   - Each developer connects their own workspace (not "My workspace") to this repo: Git folder = `fabricFolder`, branch = their feature branch.
   - One project per workspace at a time; commit before switching branches.

## 3. First-run test plan

| # | Do | Expect |
|---|---|---|
| 1 | Run `pr-validation` manually on any branch | Steps 3–6 green; results on the **Tests** tab (the branch rule only runs on real PRs) |
| 2 | PR `feature/1-pipeline-test` → `main` with a small notebook change | `pr-validation` runs as a required check; the PR run's Summary shows 1 modified item |
| 3 | Try a PR from a branch called `test` | Blocked by the branch rule |
| 4 | Merge the PR | Build → **DEV deploys by itself**. Check the log sections, the Summary-tab report and the `logs-DEV-1` artefact |
| 5 | Open the DEV workspace | The change is there, and notebooks point at the DEV lakehouse |
| 6 | The run shows **TEST: Review**. **Reject** it | Run stops cleanly; TEST unchanged |
| 7 | On the same run, **Rerun stage** TEST → Approve | TEST deploys the same artefact |
| 8 | Approve PROD | PROD deploys; tag `release/<run name>` appears in Repos → Tags; run tagged `released` |
| 9 | Merge another small PR | The release preview now compares with that tag and lists only the new change |

**Break it on purpose** to prove the checks:
- Put `prodWorkspaceId` = the TEST ID.
- Leave `<TEST_APPROVERS>` unfilled.
- Paste `AccountKey=abcdefghijklmnopqrstuvwxyz0123456789` into a notebook.
- Delete a notebook that the pipeline still calls.
- Remove a rule from `parameter.yml`.

Each must fail Build with a clear message.

## Good to know
- **Not deployed:** lakehouse data, connections, schedules and permissions. Run your pipeline once after the first deploy to each workspace, and set schedules there.
- **Lakehouses are never deleted**, even with *Remove items deleted from the repo*.
- **No hand edits:** don't edit DEV, TEST or PROD by hand. The next deployment overwrites portal changes.
- **Overlapping merges:** if two merges land before PROD is approved, approve the **newest** run; it contains both changes.
- **Dry-run limits:** it simulates the Fabric API for Lakehouse, Notebook and Data Pipeline items. If you add item types or bump `fabric-cicd` and it reports *calls the dry run does not simulate*, extend `FakeFabric` in `dry_run.py`.
