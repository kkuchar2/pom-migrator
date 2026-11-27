# pom-migrator

Tooling for the migration described in [MIGRATION_PLAN.md](MIGRATION_PLAN.md) (background: [CONTEXT.md](CONTEXT.md)).

The current version is **read-only**. It analyzes the workspace and never modifies POMs or git repositories. Git is only queried; it never fetches, and `--no-optional-locks` keeps it from touching the index.

## What the report shows

| Section | Content |
|---|---|
| Summary | parent coordinates, number of libraries / services / excluded projects, issue counts |
| Projects | role, directory, artifact, version, `<parent>` version, git branch, working-tree state, ahead/behind upstream |
| Branches | projects grouped by the branch they have checked out (detached HEAD flagged) |
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
f  Full report    e  Export report    r  Re-scan    q  Quit
```

Exit codes: `0` = no errors, `1` = the report contains errors, `2` = configuration problem, `130` = interrupted.

## Project layout

```
pom_migrator/
├── __main__.py   # `python -m pom_migrator`
├── cli.py        # arguments, interactive menu, config wizard
├── config.py     # TOML config load / validate / write
├── pom.py        # read-only pom.xml parsing (namespaces, comments, ${property} resolution)
├── gitinfo.py    # read-only git state
├── scanner.py    # classification, library graph (cycles, build order), checks
└── report.py     # rich rendering + export (html / txt / json)
```
