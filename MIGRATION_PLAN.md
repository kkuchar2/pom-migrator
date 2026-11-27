# Migration Plan – Consolidate Libraries into parent

Background and terminology: see [CONTEXT.md](CONTEXT.md).

## Goal

Move **parent** and all libraries into one multi-module Maven build, released under **one shared version**. The model is Spring Boot's (`spring-boot-dependencies` + `spring-boot-starter-parent` + modules):

- One pipeline run builds and publishes parent, the BOM and every library, all with the same version (e.g. `20261127.3`).
- Services set **one** version (the `<parent>` version or the BOM import). They declare libraries without a `<version>`.
- Version drift between libraries and services goes away.

## Target structure

```
platform/
├── .mvn/
│   └── maven.config              ← -Drevision=0.0.1-SNAPSHOT (local default)
├── pom.xml                       ← aggregator only (<modules>), not deployed
├── platform-dependencies/
│   └── pom.xml                   ← BOM: every library at ${revision}
├── parent/
│   └── pom.xml                   ← current parent POM, version → ${revision}, imports the BOM
├── lib-1/
│   └── pom.xml                   ← <parent> = parent ${revision}, relativePath ../parent
├── lib-2/
│   └── pom.xml
└── ...
```

The `groupId` and `artifactId` of parent and of every library **stay unchanged**. Only the way versions are produced and referenced changes.

---

## Step 0 – Decisions to make before starting

| # | Decision | Default proposal |
|---|---|---|
| 0.1 | Are the libraries in separate git repositories today? | If yes: merge them and keep their history (Step 1) |
| 0.2 | Must every library share one version (lockstep)? | Yes. This is the whole point of the change |
| 0.3 | Name of the BOM artifact | `<parent-artifactId>-dependencies` |
| 0.4 | Should third-party versions also move into the BOM (full Spring Boot model)? | Not yet. Keep them in parent (Step 9, optional) |
| 0.5 | Minimum Maven version | 3.6.3+ (CI-friendly `${revision}` needs 3.5+) |
| 0.6 | Does any library depend on another library? | Find out. Those dependencies lose their `<version>` and rely on the BOM |

## Step 1 – Bring everything into one repository, keeping all history

Skip this step if all projects already live in one repository.

**Goal:** after the merge, `git log`, `git blame` and `git log --follow` keep working for every file of parent **and** every library, back to their first commit, at the file's new path (`lib-1/src/...`).

### Why `git filter-repo`, and not `git subtree add` or a plain copy

| Method | History kept? | `git log -- lib-1/X` shows old commits? | `git blame` works? |
|---|---|---|---|
| Copy files + commit | No | No | No |
| `git subtree add` (without `--squash`) | Yes | No, it stops at the merge commit (old commits have the file at the root path) | Partly |
| **`git filter-repo --to-subdirectory-filter` + merge** | **Yes** | **Yes** | **Yes** |

`git filter-repo` rewrites every old commit as if the project had always lived in its subdirectory. Commit authors, dates and messages stay the same. Only the commit hashes change.

### 1.1 Prepare

1. Install `git filter-repo` (`pip install git-filter-repo` or your system package).
2. **Freeze** parent and all library repositories. Announce a cut-off time, and merge or close open PRs before it.
3. Decide what happens to long-lived branches other than `main`. Either finish them before the freeze, or migrate them as well (1.3, step 4).

### 1.2 Rewrite each source repository into its subdirectory

Always work on a **fresh clone**. `filter-repo` refuses to run on anything else, and the original repository must never be touched.

```bash
mkdir -p ~/migration && cd ~/migration

# parent → parent/
git clone <parent-repo-url> parent
git -C parent filter-repo --to-subdirectory-filter parent --tag-rename '':'parent-'

# each library → lib-N/
git clone <lib-1-repo-url> lib-1
git -C lib-1 filter-repo --to-subdirectory-filter lib-1 --tag-rename '':'lib-1-'
# ... repeat for every library
```

`--tag-rename` gives every tag a prefix (e.g. `v1.0` → `lib-1-v1.0`). Without it, tags with the same name in different repositories would clash.

### 1.3 Merge everything into the new repository

```bash
git init platform && cd platform
git commit --allow-empty -m "chore: init platform repository"

for p in parent lib-1 lib-2; do          # list every project
  git remote add "$p" ../"$p"
  git fetch "$p" --tags
  git merge --allow-unrelated-histories --no-edit "$p/main" \
      -m "chore: import $p with full history"
  git remote remove "$p"
done
```

1. Every project now has its own root history that joins `main` through a merge commit.
2. No conflicts are possible, because each project sits in its own directory.
3. Repository-level files (`.gitignore`, `README.md`, CI config) end up inside each subdirectory. Write new root-level versions in a separate follow-up commit.
4. To also migrate a long-lived branch: `git branch lib-1/feature-x lib-1/feature-x` **before** `git remote remove`.

### 1.4 Verify the history

For each project:
```bash
# commit count must match the original repository
git -C ~/migration/lib-1 rev-list --count main      # rewritten clone
git rev-list --count main -- lib-1                   # in platform

# history and blame work at the new path
git log --oneline -- lib-1/pom.xml | tail -1         # first commit of the library
git blame lib-1/src/main/java/.../SomeClass.java     # shows original authors and dates
git tag -l 'lib-1-*'                                 # tags migrated
```

### 1.5 Publish and retire the old repositories

1. Push `platform` (`main` and `--tags`).
2. **Archive** the old repositories (read-only). **Don't delete them.** Old commit hashes, PR and issue links and old CI builds still point there.
3. Add a note to each old repository's README: "Moved to `platform/lib-N`".
4. Note: issue and PR numbers mentioned in old commit messages (`#123`) now refer to the old repository. If that matters, add a prefix with `filter-repo --message-callback` in step 1.2.

### 1.6 Then

1. Add `.flattened-pom.xml` to the root `.gitignore`.
2. Continue with Step 2. Make all the POM changes from Steps 2–6 in **separate commits after** the import, so that the history clearly separates "imported as it was" from "refactored".

## Step 2 – Set up CI-friendly versioning

`.mvn/maven.config` at the repository root:
```
-Drevision=0.0.1-SNAPSHOT
```
- Local builds produce `0.0.1-SNAPSHOT`, as they do today.
- The pipeline overrides it with `-Drevision=<build number>`, for example `mvn -Drevision=20261127.3 deploy`. A value given on the command line wins over `maven.config`.

## Step 3 – Aggregator `pom.xml` (root)

It only lists the modules. It is never published.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <groupId>{parent groupId}</groupId>
    <artifactId>platform-build</artifactId>
    <version>${revision}</version>
    <packaging>pom</packaging>

    <properties>
        <maven.deploy.skip>true</maven.deploy.skip>
    </properties>

    <modules>
        <module>platform-dependencies</module>
        <module>parent</module>
        <module>lib-1</module>
        <module>lib-2</module>
        <!-- ... -->
    </modules>
</project>
```

`maven.deploy.skip` applies only to this POM, because no module inherits from it.

## Step 4 – Create the BOM `platform-dependencies/pom.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project ...>
    <modelVersion>4.0.0</modelVersion>

    <groupId>{parent groupId}</groupId>
    <artifactId>{parent artifactId}-dependencies</artifactId>
    <version>${revision}</version>
    <packaging>pom</packaging>

    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>{lib-1 groupId}</groupId>
                <artifactId>{lib-1 artifactId}</artifactId>
                <version>${revision}</version>
            </dependency>
            <!-- one entry per library -->
        </dependencies>
    </dependencyManagement>

    <build>
        <plugins>
            <!-- flatten-maven-plugin, same configuration as in Step 5.3 -->
        </plugins>
    </build>

    <!-- copy <distributionManagement> from parent so the BOM can be deployed -->
</project>
```

## Step 5 – Adapt parent `parent/pom.xml`

Start from the current parent POM. Keep its `<parent>` (spring-boot-starter-parent), properties, dependency management, plugins, `<repositories>` and `<distributionManagement>` **as they are**, apart from the changes below.

### 5.1 Version
```xml
<version>${revision}</version>      <!-- was 0.0.1-SNAPSHOT -->
```

### 5.2 Import the BOM
Add this at the **end** of `<dependencyManagement><dependencies>`:
```xml
<dependency>
    <groupId>{parent groupId}</groupId>
    <artifactId>{parent artifactId}-dependencies</artifactId>
    <version>${revision}</version>
    <type>pom</type>
    <scope>import</scope>
</dependency>
```

### 5.3 Flatten plugin
Without this plugin, the published POMs contain the literal text `${revision}` and consumers can't resolve them. Add it to `<build><plugins>`. The libraries inherit it from there.
```xml
<plugin>
    <groupId>org.codehaus.mojo</groupId>
    <artifactId>flatten-maven-plugin</artifactId>
    <version>${flatten-maven-plugin.version}</version>
    <configuration>
        <updatePomFile>true</updatePomFile>
        <flattenMode>resolveCiFriendliesOnly</flattenMode>
    </configuration>
    <executions>
        <execution>
            <id>flatten</id>
            <phase>process-resources</phase>
            <goals><goal>flatten</goal></goals>
        </execution>
        <execution>
            <id>flatten-clean</id>
            <phase>clean</phase>
            <goals><goal>clean</goal></goals>
        </execution>
    </executions>
</plugin>
```
Add `<flatten-maven-plugin.version>` to the properties. `resolveCiFriendliesOnly` only replaces `${revision}` and leaves everything else in the POM untouched.

## Step 6 – Adapt each library `lib-N/pom.xml`

1. Change the `<parent>`:
   ```xml
   <parent>
       <groupId>{parent groupId}</groupId>
       <artifactId>{parent artifactId}</artifactId>
       <version>${revision}</version>
       <relativePath>../parent/pom.xml</relativePath>
   </parent>
   ```
2. **Remove** the library's own `<version>` so it inherits `${revision}`. Keep its `groupId` only if it differs from parent's.
3. Remove `<version>` from dependencies on **other libraries**. The BOM manages them now.
4. Remove `<repositories>` and `<distributionManagement>` if they repeat what parent already has.
5. Leave everything else as it is (dependencies, plugins, configuration).

## Step 7 – Pipeline

One pipeline for the whole `platform` repository:
```bash
mvn -B -Drevision=${BUILD_NUMBER} clean deploy
```
- `BUILD_NUMBER` uses the existing scheme (`YYYYMMDD.N`).
- Remove the old per-library pipelines, or disable them.
- The repository for parent must be configured in the CI `settings.xml`, because parent can't be downloaded through its own `<repositories>`.

## Step 8 – Verify before the first release

1. `mvn clean install` from the root succeeds locally (version `0.0.1-SNAPSHOT`).
2. `mvn -Drevision=99999999.1 clean install` succeeds.
3. Check the `.flattened-pom.xml` files and the installed POMs in `~/.m2`. **None of them may contain `${revision}`.**
4. Compare `mvn help:effective-pom` for one library before and after the migration. The dependency and plugin versions must match.
5. Deploy to a test or staging repository. Build one service against it.

## Step 9 – (Optional, later) Full Spring Boot model

Move the third-party `*.version` properties and the `dependencyManagement` entries from parent into `platform-dependencies`. parent then only imports the BOM and keeps the build configuration. Services with a different `<parent>` can then import the BOM and still get every version. This step is only worth doing if such services exist.

## Step 10 – Migrate the services

Do this one service at a time. The old artifacts stay in the repository, so services you haven't migrated yet keep working.

1. Set the service's `<parent>` version to the first platform release.
2. Remove `<version>` from every library dependency:
   ```xml
   <dependency>
       <groupId>{lib-1 groupId}</groupId>
       <artifactId>{lib-1 artifactId}</artifactId>
   </dependency>
   ```
3. Services that use a different `<parent>` import the BOM instead:
   ```xml
   <dependencyManagement>
       <dependencies>
           <dependency>
               <groupId>{parent groupId}</groupId>
               <artifactId>{parent artifactId}-dependencies</artifactId>
               <version>20261127.3</version>
               <type>pom</type>
               <scope>import</scope>
           </dependency>
       </dependencies>
   </dependencyManagement>
   ```
4. Build and test the service, and check `mvn dependency:tree` for the expected versions.

From now on, a service upgrade means changing **one** version number.

## Step 11 – Clean up

- Archive the old library repositories, or mark them read-only.
- Update `README.md` and the developer docs: how to build locally and how versions work.
- Optional: automate the service version bump (Renovate or Dependabot, or `versions:update-parent`).

---

## Risks and notes

- **Lockstep releases:** any change releases every library under a new version. That is intended, but it means more artifacts in the repository.
- **Build time:** the pipeline builds everything every time. If that becomes slow, use `mvn -pl <module> -am` for local work.
- **Reactor order:** `platform-dependencies` is listed first in `<modules>` so that it's available before parent imports it. Confirm this with a clean `install` on an empty `~/.m2`.
- **Maven 4** supports CI-friendly versions natively and doesn't need the flatten plugin. Revisit this when you upgrade.
- See CONTEXT.md, "Open questions". Some of them affect Steps 1, 6 and 10.
