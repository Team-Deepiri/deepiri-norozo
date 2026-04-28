# CodeQL Setup for deepiri-norozo

This folder contains the CodeQL configuration for security scanning in this repository.

## What each file does

- `.github/workflows/codeql.yml`
  - Defines when scans run and how GitHub Actions executes CodeQL.
- `.github/codeql/codeql-config.yml`
  - Defines what folders to include and ignore during analysis.

## CodeQL workflow breakdown (`.github/workflows/codeql.yml`)

### `name: CodeQL`
The display name in the Actions tab.

### `on.pull_request.branches` and `on.push.branches`
```yaml
on:
  pull_request:
    branches: [main, dev]
  push:
    branches: [main, dev]
```
Runs scans when PRs target `main` or `dev`, and when commits are pushed to `main` or `dev`.

### `permissions`
```yaml
permissions:
  actions: read
  contents: read
  security-events: write
```
Uses least-privilege permissions. `security-events: write` is required so CodeQL can upload findings.

### Language setup (current)
```yaml
with:
  languages: python
```
This workflow currently runs analysis for Python.

### Checkout step
```yaml
with:
  fetch-depth: 0
```
- `fetch-depth: 0` keeps full git history (safe default for analysis and troubleshooting).

### Initialize CodeQL
```yaml
uses: github/codeql-action/init@v3
with:
  config-file: ./.github/codeql/codeql-config.yml
```
Starts the CodeQL engine and loads `.github/codeql/codeql-config.yml`.

### Analyze
```yaml
uses: github/codeql-action/analyze@v3
```
Executes queries and uploads results to GitHub Security.

## Config breakdown (`.github/codeql/codeql-config.yml`)

### `paths-ignore`
Generated, build, runtime artifact paths, and Python cache directories are excluded to reduce noise and runtime:

```yaml
paths-ignore:
  - '**/node_modules/**'
  - '**/dist/**'
  - '**/build/**'
  - '**/coverage/**'
  - '**/logs/**'
  - '**/*.min.js'
  - '**/__pycache__/**'
  - '**/*.pyc'
  - '**/venv/**'
  - '**/env/**'
  - '**/.egg-info/**'
```

These exclusions target:
- Common JavaScript/Node.js build artifacts (`node_modules`, `dist`, `build`)
- Test coverage reports (`coverage`)
- Application logs (`logs`)
- Minified assets (`*.min.js`)
- Python cache files (`__pycache__`, `*.pyc`)
- Python virtual environments (`venv`, `env`)
- Package build metadata (`.egg-info`)

## Best practices

1. **Keep trigger scope intentional**
   Use branch filters (`main`, `dev`) to control cost and noise.
2. **Keep language list explicit**
   Only include languages with meaningful source code.
3. **Exclude generated/vendor artifacts**
   Keep caches, dependencies, build outputs, logs, and minified files in `paths-ignore`.
4. **Pin to stable major action versions**
   `@v3` is the current stable major for CodeQL actions.
5. **Review alerts regularly**
   Triage high/critical findings first and suppress only with documented reasoning.

## Maintenance examples

Keeping this updated as code and language coverage evolve is important. Here are common maintenance changes.

### Keep language scope aligned with this repository
This workflow currently analyzes Python only:

```yaml
with:
  languages: python
```

If this repository adds production code in another supported language (e.g., `javascript-typescript`), update to:

```yaml
with:
  languages: python, javascript-typescript
```

### Exclude another generated folder
Add a glob to `paths-ignore`, for example:

```yaml
paths-ignore:
  - '**/generated/**'
  - '**/temp/**'
```

### Update action versions
When GitHub releases new CodeQL action versions, update the action references:

```yaml
uses: github/codeql-action/init@v4  # Update from v3 to v4
```

## Troubleshooting

- **Scans taking too long?** Review `paths-ignore` and add more artifact directories to exclude.
- **Missing alerts?** Ensure the branch filter in `on` includes the branches where you commit code.
- **Action failing?** Check the workflow run logs in the Actions tab for detailed error messages.
