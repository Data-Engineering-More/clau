# Microsoft Fabric CI/CD template for Azure DevOps

A single-file Azure DevOps pipeline that deploys Microsoft Fabric items (Lakehouse, Notebooks, Data Pipelines) across **DEV → TEST → PROD** workspaces using Microsoft's [`fabric-cicd`](https://microsoft.github.io/fabric-cicd/) library.

```
feature/*  ──PR──▶ develop ──▶ DEV workspace     automatic on merge
develop    ──PR──▶ main    ──▶ TEST workspace    automatic on merge
                           ──▶ PROD workspace    after approval, then git tag
hotfix/*   ──PR──▶ main    ──▶ TEST ──▶ PROD     then merge main back into develop
```

## What's in this folder

| File | Goes where in your repo | Purpose |
|---|---|---|
| `azure-pipelines.yml` | Anywhere, e.g. the repo root | The whole pipeline: validation, packaging, DEV/TEST/PROD deploys, release tag |
| `workspace/parameter.yml` | Root of your Fabric Git folder | Maps DEV IDs to TEST/PROD IDs on every deploy |
| `README.md` | — | This guide |

Your repo should end up like this:
```
<your-repo>/
├── azure-pipelines.yml
└── workspace/                          ← Fabric Git folder (Workspace settings → Git integration)
    ├── parameter.yml
    ├── <Your Lakehouse>.Lakehouse/
    ├── <Your Notebook>.Notebook/
    └── <Your Pipeline>.DataPipeline/
```

## 1. Replace the placeholders

Search both files for `>>> CHANGE` or `<`. The Build stage **fails while any placeholder is left**, so nothing half-configured can deploy.

### In `azure-pipelines.yml` (the `CONFIGURE ME` block)

| Setting | Example | Where to find it |
|---|---|---|
| `group:` | `vargrp-fabric-deploy` | The name you give the variable group in step 2 |
| `projectSlug` | `sales-reporting` | Your choice: lower case, no spaces. Used for environment names, run names and tags |
| `devWorkspaceId` | `xxxxxxxx-xxxx-…` | Open the DEV workspace in Fabric; it's the GUID after `/groups/` in the address bar |
| `testWorkspaceId` | | Same, for the TEST workspace |
| `prodWorkspaceId` | | Same, for the PROD workspace |
| `fabricFolder` (optional) | `workspace` | The **Git folder** set in Workspace settings → Git integration |
| `agentImage` (optional) | `ubuntu-latest` | Change only for a self-hosted pool (see the comment in the file) |
| Branch names (optional) | `develop`, `main` | Change in `trigger:` **and** in the three `refs/heads/...` stage conditions |

### In `workspace/parameter.yml`

| Placeholder | Where to find it |
|---|---|
| `<LAKEHOUSE_NAME>` | The lakehouse display name exactly as shown in Fabric (case-sensitive) |
| `<DEV_LAKEHOUSE_ID>` | Any `*.Notebook/notebook-content.py` in the repo → `# META "default_lakehouse": "…"` |
| `<DEV_WORKSPACE_ID>` | Same file → `"default_lakehouse_workspace_id": "…"` |
| Block 5 (optional) | Only if a Data Pipeline uses a connection: the connection ID for each environment |
| Block 6 (optional) | Only if notebooks read a lakehouse in another workspace |

### In the embedded `deploy.py` (inside `azure-pipelines.yml`, optional)

| Setting | Default | Change when |
|---|---|---|
| `ITEM_TYPES` | Lakehouse, Notebook, DataPipeline | Your workspace has other item types (Environment, SemanticModel, Report…) |
| `EXCLUDE_REGEX` | `.*_dev$` | You use a different naming rule for sandbox copies |

## 2. One-time Azure DevOps and Fabric setup

1. **Service principal.**
   - Create an app registration and a client secret.
   - Fabric admin portal → Tenant settings → allow *Service principals can use Fabric APIs* for a security group that contains it.
   - Add it as **Contributor** on the DEV, TEST and PROD workspaces.
2. **Variable group** (Pipelines → Library): `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`. Mark the secret 🔒.
3. **Environments** (Pipelines → Environments), named from your `projectSlug`:

   | Environment | Checks |
   |---|---|
   | `<projectSlug>-dev` | Exclusive lock |
   | `<projectSlug>-test` | Exclusive lock |
   | `<projectSlug>-prod` | **Approvals** and an Exclusive lock |

4. **Pipeline.**
   - New pipeline → Azure Repos Git → Existing YAML → `azure-pipelines.yml`.
   - Click **Validate**, then Save.
   - On the first run, authorise the variable group and the environments.
5. **Branch policies on `develop` and `main`.**
   - Require a pull request; block direct pushes.
   - Minimum 1 reviewer, who can't be the author.
   - Linked work item and comment resolution required.
   - **Build validation** = this pipeline, required. PR runs only validate; they never deploy.
   - Merge type: squash into `develop`; **merge commit (no fast-forward)** into `main`. Squashing the release PR makes the next one show old commits again.
6. **Repo permissions.**
   - Developers: Contribute and Create branch.
   - *<Project> Build Service*: **Contribute** and **Create tag**, so PROD can tag releases.
7. **Seed the repo once.**
   - Connect the existing DEV workspace to `develop` (Git folder = `fabricFolder`) and commit all items.
   - Merge `develop` into `main`.
   - Then **disconnect DEV from Git**. From now on the pipeline deploys it.

## 3. Daily workflow

**Developers work in their own Fabric workspace** (not "My workspace"):
1. First time only: Workspace settings → Git integration → this repo, branch `develop`, the same Git folder.
2. For each change, create `feature/<work-item-id>-<short-desc>` from `develop`.
3. In your workspace, go to Source control → Branches → **Switch branch** to the feature branch. Commit or discard pending changes first, because switching replaces the workspace content.
4. Attach notebooks to the **DEV** lakehouse to work with real data. `parameter.yml` re-points them for each environment.
5. Commit, then raise a PR into `develop`. Once it's merged, DEV deploys automatically.

**Release:** raise a PR from `develop` into `main` and merge it. TEST deploys automatically. After UAT, approve the PROD stage. PROD deploys and the commit is tagged `release/<projectSlug>_<date>.<n>`.

**Hotfix:** branch from `main`, raise a PR into `main` (TEST, then PROD), then merge `main` back into `develop`.

**Rollback:** revert the release merge commit on `main` with a PR. TEST and PROD redeploy the previous code. This rolls back code only, not data.

## 4. Good to know

- **Not deployed:** lakehouse tables and files, connections, schedules and workspace permissions. After the first deploy to a workspace, run your pipeline once to build the tables, then set schedules there.
- **Lakehouses are never deleted** by this pipeline, even with *Remove items deleted from the repo* turned on.
- **Hand edits get overwritten.** Nobody edits DEV, TEST or PROD by hand; the next deployment overwrites portal changes.
- **Never put `/` in an item name.** Fabric Git sync breaks; the Build stage flags it.
- **Unmapped IDs are flagged.** The Build stage warns about any GUID in notebook or pipeline code that `parameter.yml` doesn't map, because it would still point at DEV after deployment.

## What the pipeline checks

| Check | Stage |
|---|---|
| Every item has a valid `.platform` file; no `/` in names | Build |
| `parameter.yml` parses, has DEV/TEST/PROD for every rule, no placeholders left | Build |
| Hard-coded GUIDs not covered by `parameter.yml` (warning) | Build |
| Workspace IDs are real GUIDs; secrets are present | Each deploy |
| Every repo item exists in the target after deployment | Each deploy |

Tested against a mocked Fabric API with `fabric-cicd` 1.2.0. Azure DevOps YAML can only be compiled by Azure DevOps, so use **Validate** in the pipeline editor before your first run.
