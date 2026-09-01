# Release Process

This project uses [release-please](https://github.com/googleapis/release-please) to automate releases.

## How It Works

1. **Commit with Conventional Commits**: Use conventional commit messages:
   - `feat:` for new features (minor version bump)
   - `fix:` for bug fixes (patch version bump)
   - `feat!:` or `fix!:` with `BREAKING CHANGE:` in body (major version bump)
   - `docs:`, `chore:`, `refactor:`, `test:`, etc.

2. **Automated PRs**: When you push to `main`, release-please will:
   - Create/update a release PR with CHANGELOG updates
   - Bump version in `pyproject.toml`
   - Track all changes since the last release

3. **Create Release**: When you merge the release PR:
   - A GitHub release is created automatically
   - Tags are created
   - CHANGELOG is updated

## Commit Message Examples

```bash
# New feature (minor version bump)
git commit -m "feat: add user profile editing"

# Bug fix (patch version bump)
git commit -m "fix: resolve navbar dropdown alignment issue"

# Breaking change (major version bump)
git commit -m "feat!: refactor models into package structure

BREAKING CHANGE: models imports may need updates"

# Other changes (no version bump, but in CHANGELOG)
git commit -m "docs: update README with deployment instructions"
git commit -m "refactor: split views.py into views package"
```

## Version Bumping

- **Major (1.0.0 → 2.0.0)**: Breaking changes (`feat!:`, `fix!:` with `BREAKING CHANGE:`)
- **Minor (1.0.0 → 1.1.0)**: New features (`feat:`)
- **Patch (1.0.0 → 1.0.1)**: Bug fixes (`fix:`)

## Manual Release

If you need to trigger a release manually:

1. Ensure the release PR exists and is up to date
2. Review the CHANGELOG in the PR
3. Merge the release PR
4. The GitHub release will be created automatically

## Configuration Files

- `.github/workflows/release-please.yml` - GitHub Actions workflow
- `release-please-config.json` - Release-please configuration
- `.release-please-manifest.json` - Current version tracking
- `CHANGELOG.md` - Auto-generated changelog
