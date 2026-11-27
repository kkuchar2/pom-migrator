# POM Migrator – Project Context

## 1. Overview

Several Maven projects sit in one directory:

- **parent**: the main project (`packaging=pom`). It holds shared versions, dependency management, plugin management, repositories and distribution management. In this document, "parent" (in bold or on its own) means this project. The `<parent>` XML element is always written with angle brackets.
- **Libraries**: other projects in the same directory. Each one has its own `pom.xml` that uses **parent** as its `<parent>`.

```
<root-dir>/
├── parent/            ← main POM (packaging pom)
│   └── pom.xml
├── library-1/
│   └── pom.xml       ← <parent> → parent
├── library-2/
│   └── pom.xml       ← <parent> → parent
└── ...
```

## 2. parent – `pom.xml` structure

The file starts with the standard XML declaration (`<?xml version="1.0" encoding="UTF-8"?>`), followed by a normal `<project>` root element.
Comments and blank lines may appear anywhere between elements.

Sections, in order:

### 2.1 `<parent>`
Points to Spring Boot:
```xml
<parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>x.y.z</version>
</parent>
```

### 2.2 Project coordinates and metadata
```xml
<groupId>...</groupId>
<artifactId>...</artifactId>
<packaging>pom</packaging>
<name>...</name>
<description>...</description>
<version>0.0.1-SNAPSHOT</version>
```

### 2.3 `<properties>`
Fixed part:
```xml
<java.version>21</java.version>
<maven.compiler.source>${java.version}</maven.compiler.source>
<maven.compiler.target>${java.version}</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
```
Followed by many version properties:
```xml
<somename.version>{version}</somename.version>
```
Comments and blank lines may be mixed in.

### 2.4 `<dependencyManagement>` → `<dependencies>`
Entries can use any combination of these elements:
- `groupId`, `artifactId`, `version` (the minimum)
- `type` + `scope` (e.g. BOM import: `<type>pom</type><scope>import</scope>`)
- `scope` (e.g. `test`)
- `exclusions` (one or more `<exclusion>` entries)
- versions given literally or as property references (`${somename.version}`)

### 2.5 `<build>`
- **`<pluginManagement>` → `<plugins>`**: plugin definitions in any setup (version, configuration, executions, dependencies, ...). Versions may be literal or come from properties.
- **`<plugins>`**: plugins actually used by the build.

### 2.6 `<repositories>`
N `<repository>` entries. **Keep as is.**

### 2.7 `<distributionManagement>`
**Keep as is** (to be confirmed).

## 3. Versioning of parent

| Build context | Produced version | Notes |
|---|---|---|
| Local build | `0.0.1-SNAPSHOT` | value written in `pom.xml` |
| CI pipeline | e.g. `20260734.3` | made from the build date plus the build's order number that day (`YYYYMMDD.N` style) |

The pipeline version is set at build time. It is not stored in `pom.xml`.

## 4. Libraries – `pom.xml` structure

Each library:
- has a `<parent>` that points to parent:
  ```xml
  <parent>
      <groupId>{parent groupId}</groupId>
      <artifactId>{parent artifactId}</artifactId>
      <version>{parent pipeline build version, e.g. 20260734.3}</version>
  </parent>
  ```
- has its own dependencies, plugins and other configuration.
- uses versions and plugin configuration managed by parent.

## 5. Services – `pom.xml` structure

Services are other projects that consume the libraries. Each service:
- has a `<parent>` that points to parent (same pattern as the libraries, version = a parent pipeline build).
- has one or more of the libraries in its `<dependencies>`:
  ```xml
  <dependency>
      <groupId>{library groupId}</groupId>
      <artifactId>{library artifactId}</artifactId>
      <version>{library version}</version>
  </dependency>
  ```
- has its own dependencies, plugins and other configuration, with versions managed by parent.

## 6. Key relationships

```
spring-boot-starter-parent
        ▲ <parent>
   parent (pom, 0.0.1-SNAPSHOT locally / YYYYMMDD.N in CI)
        ▲ <parent>                       ▲ <parent>
   Library 1 ... Library N   ◄──────  Service 1 ... Service M
                              depends on (a subset of the libraries)
```

The parent version therefore shows up in two places:
1. Library `<parent>` → parent
2. Service `<parent>` → parent

The two can point to different parent builds. Services also pull in libraries, and those libraries carry their own `<parent>` version, so one service build can have more than one parent version in its dependency tree.

## 7. Open questions (for the refactor follow-up)

- How does the pipeline inject the version (`-Drevision`, `versions:set`, something else)?
- Do libraries resolve parent from the remote repository only, or also through `<relativePath>`?
- Is `<distributionManagement>` definitely left untouched?
- Should the formatting, comments and blank lines in the POMs be kept exactly during the refactor?
- Is the `<parent>` version in libraries and services updated by hand, or should the tooling update it?
- How are library versions given in the services: literal, a property, or managed in parent's `dependencyManagement`?
- Do the services live in the same directory as parent and the libraries?
- Do the libraries depend on each other?

## 8. Refactor goals

_TBD – to be filled in with the follow-up request._
