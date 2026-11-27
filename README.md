# pom-migrator

Tooling for the migration described in [MIGRATION_PLAN.md](MIGRATION_PLAN.md) (background: [CONTEXT.md](CONTEXT.md)).

The analysis is **read-only**: it never modifies POMs or git repositories. The one exception is the explicit, confirmed **reset to `origin/main`** menu option (see below). During analysis git is only queried: it never fetches, and `--no-optional-locks` keeps it from touching the index.

## What the report shows

| Section | Content |
|---|---|
| Summary | parent coordinates, number of libraries / services / excluded projects, issue counts |
| Projects | role, directory, artifact, version, `<parent>` version, git branch, working-tree state, ahead/behind upstream |
| Branches | projects grouped by the branch they have checked out (detached HEAD flagged) |
| Git remotes | `origin` URL of every project grouped by provider: **Azure DevOps** (`dev.azure.com`, `ssh.dev.azure.com`, `*.visualstudio.com`), **GitHub** (`github.com`, `*.ghe.com`), other, none. Projects still on Azure are listed first. Credentials in URLs are shown as `***`. |
| Parent versions in use | which parent version each library and service references (shows version drift) |
| Library → library dependencies | **does any library depend on another library?**, which ones, cycles, build order |
| Who uses which library | for every library: the libraries/services that use it, with an explicit or managed version |
| Excluded | projects matching the prefix that do **not** use parent, and why |
| Issues | errors / warnings / info (missing libraries, unparsable POMs, detached HEADs, cycles, …) |

Classification:
- **parent**: the `parent` directory from the config.
- **library**: a directory listed in `libraries` whose `<parent>` is parent.
- **service**: any other directory (matching the prefix) whose `<parent>` is parent.
- **excluded**: no `pom.xml`, an unreadable `pom.xml`, no `<parent>`, or a different `<parent>`.

## Requirements

- Python **3.11+** (uses `tomllib`)
- `git` on `PATH`
- Python package `rich`

## Setup

```bash
cd pom-migrator

# option A: virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# option B: uv
uv venv && source .venv/bin/activate && uv pip install -r requirements.txt
```

## Configuration

Copy the example and edit it:

```bash
cp pom-migrator.example.toml pom-migrator.toml
```

```toml
[workspace]
root = "~/projects"          # directory containing all projects (absolute, ~, or relative to this file)
prefix = "acme-"             # only directories starting with this are scanned ("" = all)
parent = "acme-parent"       # directory name of the parent project
libraries = [                # directory names of library projects
    "acme-lib-common",
    "acme-lib-security",
]
```

**Windows paths:** `"C:\Users\me\projects"`, `'C:\Users\me\projects'` and `"C:/Users/me/projects"` all work. Strict TOML treats `\` inside double quotes as an escape character, so the tool reads such values with backslashes taken literally and prints a warning. Single quotes or forward slashes avoid the warning.

If you have no config file yet, just start the tool. It offers to create one interactively: it lists the directories, and you pick the parent and the libraries by number (e.g. `2,4,6-9`).

## Running

Run these from the `pom-migrator` directory (with the venv activated):

```bash
# interactive (uses ./pom-migrator.toml)
python3 -m pom_migrator

# use another config file
python3 -m pom_migrator -c ~/configs/acme.toml

# print the full report once, no prompts (CI / piping)
python3 -m pom_migrator -n

# also save the report: .html (colored), .txt (plain) or .json (machine-readable)
python3 -m pom_migrator -n -e report.html
python3 -m pom_migrator -n -e report.json

# help
python3 -m pom_migrator --help
```

Interactive menu:

```
1  Summary                        6  Parent versions in use
2  All projects                   7  Library → library dependencies
3  Libraries only                 8  Who uses which library
4  Services only                  9  Excluded projects
5  Branches                       i  Issues
g  Git remotes (Azure DevOps / GitHub)
f  Full report    e  Export report    r  Re-scan    q  Quit
s  Reset all to clean origin/main (destructive)
c  Consolidate libraries into parent (modifies parent)
```

### Reset all to clean `origin/main` (menu `s`, destructive)

This is the only option that modifies repositories, and it only runs from the interactive menu. For every repository of the parent, the libraries and the services (each repository once, even if it holds several projects):

1. `git fetch --prune origin`, then a **preview**: current branch, modified and untracked files, local `main` ahead of `origin/main`, unpushed commits. Repositories without `origin` or `origin/main` are skipped. Repositories that are already clean are left alone.
2. You can exclude repositories by number, choose whether to delete untracked files (`git clean -fd`; ignored files such as `target/` and IDE settings are kept), and choose whether to make a **backup** (default: yes).
3. Without backup, the tool lists exactly what will be lost. You must type `RESET` to continue.
4. Per repository: `git checkout --force -B main origin/main`, set the upstream, `git reset --hard origin/main`, optionally `git clean -fd`.

Backup and recovery:
- Uncommitted and untracked work goes to `git stash` (`git stash list`, `git stash pop`).
- Local `main` commits that aren't on `origin/main`, and commits on a detached HEAD, are kept as branches: `backup/pom-migrator/<timestamp>/...`.
- Other local branches (e.g. `feature/x`) are **never deleted**. They simply stop being checked out.

Remote and branch name are `REMOTE` / `BRANCH` in `pom_migrator/gitsync.py` (`origin` / `main`).

### Consolidate libraries into parent (menu `c`)

Copies every library into the parent directory as a Maven module, rewrites the POMs and verifies the result with a Maven build. **The source library repositories are only read.** Only the parent changes, on a new branch, and nothing is committed.

Preconditions: the parent's working tree is clean, no library directory exists in the parent yet, and there are no library dependency cycles.

| Where | Change |
|---|---|
| parent `pom.xml` | `<version>` → `${revision}` (old value kept as the `<revision>` property = local default) |
| | `<modules>` with every library (in dependency order) |
| | `dependencyManagement`: library entries set to `${revision}` (missing ones are added) |
| | `flatten-maven-plugin` (`resolveCiFriendliesOnly`): published POMs contain the real version |
| parent `.gitignore` | `.flattened-pom.xml` |
| `parent/<library>/` | copied files: tracked + untracked-not-ignored (no `target/`, `.git`, IDE files, `*.iml`, Maven wrapper) |
| `parent/<library>/pom.xml` | `<parent><version>` → `${revision}`, `<relativePath>` removed, own `<version>` removed, explicit versions of other libraries removed |

The POMs are edited as text: comments, blank lines and formatting stay as they are.

Flow: preview of every change → branch name (default `consolidate-libraries`) → confirm → copy and rewrite → optional `mvn clean verify` (tests optional). The output is a compact per-module result plus errors; the full log goes to `pom-migrator-build.log`. If the build fails, you can revert automatically (removes the copied modules, resets the parent, switches back, deletes the branch).

Why `${revision}` and not `${project.version}` in `dependencyManagement`: inherited properties are resolved in the child, so a service would get **its own** version for the libraries. With `${revision}` plus flatten, published POMs contain fixed numbers.

**Pipeline change:** build the parent with `mvn -Drevision=<build number> deploy`. Without `-Drevision`, the local default (e.g. `0.0.1-SNAPSHOT`) is used.

Exit codes: `0` = no errors, `1` = the report contains errors, `2` = configuration problem, `130` = interrupted.

## Project layout

```
pom_migrator/
├── __main__.py   # `python -m pom_migrator`
├── cli.py        # arguments, interactive menu, config wizard
├── config.py     # TOML config load / validate / write
├── pom.py        # read-only pom.xml parsing (namespaces, comments, ${property} resolution)
├── gitinfo.py    # read-only git state
├── gitsync.py    # reset to origin/main (fetch, plan, backup, execute)
├── sync_ui.py    # interactive reset flow (preview, confirm, results)
├── pomedit.py    # formatting-preserving pom.xml editor (text splices, comments kept)
├── consolidate.py      # plan / copy / rewrite POMs / revert
├── consolidate_ui.py   # interactive consolidation flow + build verification
├── maven.py      # runs mvn, condenses output (module results, errors)
├── scanner.py    # classification, library graph (cycles, build order), checks
└── report.py     # rich rendering + export (html / txt / json)
```
