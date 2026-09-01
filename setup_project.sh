#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# setup_project.sh — Bootstrap a new project from this template.
#
# Replaces all template placeholders and renames the starter_app/ directory.
# Run once, then delete this script.
# ──────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# BSD (macOS) sed requires an argument to -i; GNU sed must not get one.
sed_inplace() {
    if sed --version >/dev/null 2>&1; then
        sed -i "$@"
    else
        sed -i '' "$@"
    fi
}

prompt() {
    local var_name="$1" prompt_text="$2" default="${3:-}"
    local value
    if [[ -n "$default" ]]; then
        printf "${CYAN}%s${NC} [${BOLD}%s${NC}]: " "$prompt_text" "$default"
    else
        printf "${CYAN}%s${NC}: " "$prompt_text"
    fi
    read -r value
    value="${value:-$default}"
    if [[ -z "$value" ]]; then
        echo -e "${RED}Error: A value is required.${NC}" >&2
        exit 1
    fi
    eval "$var_name=\$value"
}

echo ""
echo -e "${BOLD}Django Starter — Project Setup${NC}"
echo "This will replace all template placeholders in the codebase."
echo ""

prompt APP_NAME        "Python package name (lowercase, underscores ok)" ""
prompt DESCRIPTION     "One-line project description"                    ""
prompt CLIENT_NAME     "Client / company display name"                   ""
prompt CLIENT_LEGAL    "Client legal name (for footer, etc.)"            "$CLIENT_NAME"

# Validate package name: lowercase letters, digits, underscores; must start with letter
if [[ ! "$APP_NAME" =~ ^[a-z][a-z0-9_]*$ ]]; then
    echo -e "${RED}Error: Package name must be lowercase, start with a letter, and contain only letters/digits/underscores.${NC}"
    exit 1
fi

if [[ ! -d "starter_app" ]]; then
    echo -e "${RED}Error: starter_app/ directory not found. Has this script already been run?${NC}"
    exit 1
fi

echo ""
echo -e "${BOLD}Summary:${NC}"
echo "  Package name:    $APP_NAME"
echo "  Description:     $DESCRIPTION"
echo "  Client name:     $CLIENT_NAME"
echo "  Client legal:    $CLIENT_LEGAL"
echo ""
printf "Proceed? [Y/n]: "
read -r confirm
# tr instead of ${confirm,,} — macOS ships bash 3.2, which lacks case conversion
confirm=$(printf '%s' "$confirm" | tr '[:upper:]' '[:lower:]')
if [[ "$confirm" == "n" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""

# 1. Rename the starter_app/ directory
echo -e "Renaming ${CYAN}starter_app/${NC} → ${GREEN}${APP_NAME}/${NC}"
mv "starter_app" "$APP_NAME"

# 2. Replace placeholders in all text files (skip .git, .venv, __pycache__, binary files)
replace_in_files() {
    local placeholder="$1" replacement="$2"
    # Use grep to find files containing the placeholder, then sed to replace
    grep -rl --include='*.py' --include='*.toml' --include='*.md' \
             --include='*.html' --include='*.json' --include='*.sh' \
             --include='*.txt' --include='*.cfg' --include='*.ini' \
             --include='*.yml' --include='*.yaml' --include='*.bru' \
             --include='*.env*' --include='Dockerfile' --include='Makefile' \
             --exclude-dir='.git' --exclude-dir='.venv' --exclude-dir='__pycache__' \
             --exclude-dir='node_modules' \
             "$placeholder" . 2>/dev/null | while read -r file; do
        sed_inplace "s|${placeholder}|${replacement}|g" "$file"
        echo "  Updated: $file"
    done
}

echo -e "Replacing ${CYAN}starter_app${NC} → ${GREEN}${APP_NAME}${NC}"
replace_in_files 'starter_app' "$APP_NAME"

echo -e "Replacing ${CYAN}<description>${NC} → ${GREEN}${DESCRIPTION}${NC}"
replace_in_files '<description>' "$DESCRIPTION"

echo -e "Replacing ${CYAN}<client legal name>${NC} → ${GREEN}${CLIENT_LEGAL}${NC}"
replace_in_files '<client legal name>' "$CLIENT_LEGAL"

echo -e "Replacing ${CYAN}<client>${NC} → ${GREEN}${CLIENT_NAME}${NC}"
replace_in_files '<client>' "$CLIENT_NAME"

# 3. Rename fly.toml mount source
sed_inplace "s|starter_app_data|${APP_NAME}_data|g" fly.toml 2>/dev/null || true

echo ""
echo -e "${GREEN}${BOLD}Done!${NC} Next steps:"
echo ""
echo "  1. cp .env.example .env     # and fill in SECRET_KEY"
echo "  2. make setup               # install deps, migrate, seed"
echo "  3. make dev                  # start the dev server"
echo ""
echo "  You can now delete this script: rm setup_project.sh"
echo ""
