#!/usr/bin/env python3
"""Interactive production deployment bootstrapper for PHP projects (Linux).

Python 3.9+, standard library only. Run on the target host to apply changes.
"""
from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ask(label: str, default: str = "") -> str:
    prompt = f"{label} [{default}]: " if default else f"{label}: "
    while True:
        value = input(prompt).strip() or default
        if value:
            return value
        print("This field is required.")


def yesno(question: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    return (input(f"{question} [{suffix}]: ").strip().lower() or ("y" if default else "n")) in ("y", "yes")


def validate(label: str, value: str, pattern: str) -> str:
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def absolute_path(label: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) == "/":
        raise ValueError(f"{label} must be an absolute, non-root path with no '..' segments")
    if any(c in str(path) for c in ("\n", "\r", "\x00")):
        raise ValueError(f"Invalid characters in {label}")
    return path


@dataclass(frozen=True)
class Config:
    org: str
    repo: str
    slug: str
    server_label: str
    domain: str
    project_dir: Path
    script_path: Path
    ssh_alias: str
    ssh_port: int
    docker_no: str
    deploy_user: str = "deploy"

    @property
    def home(self) -> Path:
        return Path("/home") / self.deploy_user

    @property
    def read_key(self) -> Path:
        return self.home / ".ssh" / f"github_{self.slug}_readonly"

    @property
    def actions_key(self) -> Path:
        return self.home / ".ssh" / f"gha_{self.slug}_prod"

    @property
    def origin(self) -> str:
        return f"git@{self.ssh_alias}:{self.org}/{self.repo}.git"


def collect() -> Config:
    print("\nProject settings (press Enter to accept a suggested value):")
    org = validate("organization", ask("GitHub owner (user or organization)"), r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
    repo = validate("repository", ask("Repository name"), r"[A-Za-z0-9_.-]+")
    slug = validate("project slug", ask("Project slug", repo.lower().replace("_", "-")), r"[a-z0-9]+(?:-[a-z0-9]+)*")
    label = validate("server label", ask("Server label", "prod-01"), r"[A-Za-z0-9._-]+")
    domain = validate("domain", ask("Project domain"), r"[A-Za-z0-9.-]+")
    project_dir = absolute_path("project directory", ask("Absolute project checkout path", f"/var/www/php/{domain}"))
    script_path = absolute_path("deployment script path", ask("Absolute deployment script path", f"/var/www/scripts/deploy-{slug}.sh"))
    alias = validate("SSH alias", ask("GitHub SSH alias", f"github-{slug}"), r"[A-Za-z0-9_-]+")
    port = int(ask("Server SSH port", "22"))
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    docker_no = validate("Docker project number", ask("Docker project number (00-99)", "01"), r"[0-9]{2}")
    return Config(org, repo, slug, label, domain, project_dir, script_path, alias, port, docker_no)


def deployment_script(c: Config) -> str:
    # repr from shlex.quote also guards shell metacharacters in paths.
    return f'''#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_SLUG={shlex.quote(c.slug)}
PROJECT_DIR={shlex.quote(str(c.project_dir))}
EXPECTED_ORIGIN={shlex.quote(c.origin)}

LOCK_DIR="$HOME/.cache"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_DIR/deploy_${{PROJECT_SLUG}}.lock"
flock -n 9 || {{ echo "Deployment already running"; exit 1; }}

echo "=== Deploy started: $PROJECT_SLUG ==="
[[ "$(id -un)" == {shlex.quote(c.deploy_user)} ]] || {{ echo "Run as {c.deploy_user}"; exit 1; }}
[[ -d "$PROJECT_DIR/.git" ]] || {{ echo "Git checkout missing"; exit 1; }}
[[ -f "$PROJECT_DIR/.env" ]] || {{ echo ".env missing"; exit 1; }}
[[ -d "$PROJECT_DIR/Storage" ]] || {{ echo "Storage missing"; exit 1; }}
cd "$PROJECT_DIR"
[[ "$(git remote get-url origin)" == "$EXPECTED_ORIGIN" ]] || {{ echo "Unexpected origin"; exit 1; }}
[[ -z "$(git ls-files -- .env)" ]] || {{ echo "Tracked .env; refusing"; exit 1; }}
[[ -z "$(git ls-files -- Storage)" ]] || {{ echo "Tracked Storage entries detected; review required"; exit 1; }}
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {{ echo "Tracked changes detected; refusing"; exit 1; }}
[[ "$(git branch --show-current)" == main ]] || {{ echo "Not on main; refusing"; exit 1; }}
[[ -f composer.lock ]] || {{ echo "composer.lock missing; refusing"; exit 1; }}

# Safety gate: neither staged nor untracked paths should be displaced by the incoming commit.
git fetch --prune origin main
if [[ -n "$(git ls-files -o --exclude-standard)" ]]; then
    echo "Untracked non-ignored files present; review before deploying"; exit 1
fi
# Compare runtime files and verify backup/cutover before relying on reset.
git reset --hard origin/main
[[ -f composer.lock ]] || {{ echo "composer.lock missing after reset"; exit 1; }}
composer install --no-dev --no-interaction --prefer-dist --optimize-autoloader
composer check-platform-reqs --no-dev
echo "Deployed commit: $(git rev-parse --short HEAD)"
echo "=== Deploy finished ==="
'''


def workflow(c: Config) -> str:
    # The GitHub expression syntax must be preserved literally.
    return '''name: Deploy Production

on:
  push:
    branches:
      - main

permissions: {}

concurrency:
  group: production-deploy
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to Production
        uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.HOST }}
          port: ${{ secrets.PORT }}
          username: ${{ secrets.USERNAME }}
          key: ${{ secrets.SSH_KEY }}
          fingerprint: ${{ secrets.SSH_HOST_FINGERPRINT }}
          command_timeout: 20m
          script: |
            bash ''' + shlex.quote(str(c.script_path)) + '\n'


def guide(c: Config, applied: bool = False) -> str:
    ports = ", ".join(f"{name}={int('3' + c.docker_no + f'{num:02d}')}" for num, name in enumerate(("HTTP", "HTTPS", "MySQL", "PHP-FPM", "PostgreSQL", "Redis"), 1))
    status = "The bootstrap script was executed locally. Verify the results below." if applied else "Documentation only: no server changes have been performed."
    return f'''# Production Deployment Setup — {c.org}/{c.repo}

{status}

## Project settings

| Setting | Value |
|---|---|
| GitHub repository | `{c.org}/{c.repo}` (private recommended; configurable) |
| Project slug | `{c.slug}` |
| Server | `{c.server_label}` |
| Domain | `{c.domain}` |
| Checkout directory | `{c.project_dir}` |
| Deploy script | `{c.script_path}` |
| SSH alias | `{c.ssh_alias}` |
| SSH server port | `{c.ssh_port}` |
| GitHub read key | `{c.read_key}` |
| Actions login key | `{c.actions_key}` |
| Docker project no. | `{c.docker_no}` |

## 1. Configure GitHub repository (manual)

1. Create a repository (private recommended for production code) named `{c.org}/{c.repo}`.
2. Create `main` and `develop` branches; set `main` as default.
3. Import/review your rulesets: targets, enforcement, PR reviews, no force push,
   no deletion, and bypass permissions. Use a separate `pull_request` workflow
   for mandatory pre-merge status checks.
4. Allow approved Actions under Settings > Actions > General.
5. Work in feature branches -> `develop`, then open reviewed PRs into `main`.

## 2. Prepare the Linux server (administrator)

Commands below are examples; inspect existing users, ownership and paths first.

```bash
id {c.deploy_user} || sudo adduser --disabled-password --gecos '' {c.deploy_user}
git --version
composer --version
php -v
sudo install -d -o {c.deploy_user} -g {c.deploy_user} -m 700 {c.home}/.ssh
sudo install -d -o root -g root -m 755 {c.script_path.parent}
namei -l {shlex.quote(str(c.project_dir))}
# Apply an ACL only on the specific parent if deploy must create a new checkout:
# sudo setfacl -m u:{c.deploy_user}:rwx {shlex.quote(str(c.project_dir.parent))}
```

Never change ownership of all `/var/www`, and never use `chmod -R 777`.
The checkout is owned by `deploy`; grant the PHP-FPM account write access only
on actual runtime directories (e.g. Storage/cache). Confirm Nginx docroot.
Back up code, uploads and database before a cutover.

## 3. Server -> GitHub (read-only deploy key)

Generate the key as `{c.deploy_user}` without overwriting existing keys:

```bash
sudo -iu {c.deploy_user}
umask 077
mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -C '{c.server_label}/{c.repo}/server-to-github' -f {c.read_key} -N ''
cat {c.read_key}.pub
```

Add the public key to **this repository only** under Settings > Deploy keys;
leave **Allow write access** unchecked. Confirm the official GitHub SSH host
fingerprint before accepting `github.com` in `known_hosts`:
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints

Add the following block to `{c.home}/.ssh/config` (preserve existing blocks):

```sshconfig
Host {c.ssh_alias}
    HostName github.com
    User git
    IdentityFile {c.read_key}
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Test access *after* trusting GitHub's verified host key and adding the deploy key:

```bash
sudo -iu {c.deploy_user} ssh -T {c.ssh_alias}
sudo -iu {c.deploy_user} git ls-remote {c.origin} HEAD
```

## 4. First checkout (manual safety checkpoint)

Inspect current data, `.env`, Storage, symlinks, Nginx root, and backups.
Do **not** clone or reset into an occupied production directory.
If the target is new and its parent is writable by deploy:

```bash
sudo -iu {c.deploy_user} git clone --branch main --single-branch {c.origin} {shlex.quote(str(c.project_dir))}
```

Transfer `.env` and runtime data from backup, then set permissions explicitly.
Ensure `.env` and generated uploads are **not tracked by Git**.
Review `.gitignore` case sensitivity and commit `composer.lock`.
Do not run the deploy script until `main` contains real deployable code.

## 5. GitHub Actions -> server (independent SSH key)

```bash
sudo -iu {c.deploy_user}
umask 077
ssh-keygen -t ed25519 -C '{c.org}/{c.repo}/actions-to-{c.server_label}' -f {c.actions_key} -N ''
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
PUBKEY="$(cat {c.actions_key}.pub)"
grep -qxF "$PUBKEY" ~/.ssh/authorized_keys || printf '%s\\n' "$PUBKEY" >> ~/.ssh/authorized_keys
```

Set repository **Actions secrets** (never commit the private key):

| Secret | Value |
|---|---|
| `HOST` | Reachable production SSH host/IP |
| `PORT` | `{c.ssh_port}` |
| `USERNAME` | `{c.deploy_user}` |
| `SSH_KEY` | Full private contents of `{c.actions_key}` |
| `SSH_HOST_FINGERPRINT` | Independently verified SSH *server host* fingerprint |

Find the server's fingerprint as administrator (verify out of band):

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

**Only after** the Secret is saved and tested, securely remove the temporary
Actions **private** key from the server. Keep its public key authorized.

## 6. Deployment script and workflow

Place the generated deployment Bash script at `{c.script_path}`, owned by root,
mode 755. It executes as `deploy`; it uses flock, checks tracked changes,
checks `.env`/Storage, verifies origin, fetches `main`, and runs Composer.
Do not use `sudo` inside it. The script is designed for host PHP/Composer,
not a Docker-only application. Composer project scripts need review.

Place `deploy.yml` at `.github/workflows/deploy.yml` in the repository,
merge it via PR into `main`. It runs on push to `main` only.

Example syntax check and controlled test:

```bash
sudo -u {c.deploy_user} bash -n {shlex.quote(str(c.script_path))}
# ONLY after backups and an explicit decision to deploy:
# sudo -iu {c.deploy_user} bash {shlex.quote(str(c.script_path))}
```

**Important:** in-place `git reset --hard` + `composer install` is *not atomic*.
If Composer fails, the live site may be partially updated. For critical sites,
use versioned releases, atomic symlink switches and a tested rollback path.

## 7. Git hygiene and Docker ports

Check `git status --short`, `git ls-files .env Storage`, `.gitignore`, vendor,
node_modules, secrets and uploads before push. `.gitignore` does not untrack
previously committed files; use `git rm --cached` after review when needed.

Docker port convention `3ppss` for project `{c.docker_no}`:
{ports}
Publish only ports that genuinely need a host mapping.

## 8. Final verification checklist

- [ ] GitHub `main`/`develop`, repository visibility and rulesets validated.
- [ ] Backups made and Nginx/docroot verified.
- [ ] Deploy user and checkout-parent permissions checked.
- [ ] Separate SSH keys created; GitHub host fingerprint verified.
- [ ] Read-only GitHub Deploy Key registered; `git ls-remote` succeeds.
- [ ] Server host fingerprint verified and Actions secrets registered.
- [ ] `.env`, Storage and permissions preserved; no runtime files tracked.
- [ ] `composer.lock` exists on `main` and Composer scripts were reviewed.
- [ ] Deploy script and `.github/workflows/deploy.yml` paths match.
- [ ] PR `develop -> main` merged and Actions result inspected.
- [ ] HTTP health, PHP-FPM logs and deployed commit verified.

Reference: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
'''


def run(*cmd: str, user: str | None = None) -> None:
    args = (["runuser", "-u", user, "--"] if user else []) + list(cmd)
    print("+", shlex.join(args))
    subprocess.run(args, check=True)


def safe_write(path: Path, content: str, mode: int = 0o644) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            print(f"Unchanged: {path}")
            return True
        print(f"Existing file will NOT be overwritten: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, mode)
    print(f"Created: {path}")
    return True


def ssh_as(user: str, *args: str) -> None:
    run(*args, user=user)


def apply(c: Config) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Apply mode requires Linux and root privileges (sudo python3 ...)")
    print("\nApply mode will change ONLY local server settings; it cannot configure GitHub repository settings/secrets.")
    print(f"Target user: {c.deploy_user}; project: {c.org}/{c.repo}; checkout: {c.project_dir}")
    if not yesno("Proceed with server-side setup?"):
        return

    try:
        account = pwd.getpwnam(c.deploy_user)
    except KeyError:
        if not yesno(f"Create account '{c.deploy_user}' (disabled password)?"):
            raise RuntimeError("A deploy account is required")
        run("adduser", "--disabled-password", "--gecos", "", c.deploy_user)
        account = pwd.getpwnam(c.deploy_user)
    if str(Path(account.pw_dir)) != str(c.home):
        raise RuntimeError(f"Unexpected deploy home {account.pw_dir}; expected {c.home}")

    ssh_dir = c.home / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    os.chown(ssh_dir, account.pw_uid, account.pw_gid)
    os.chmod(ssh_dir, 0o700)

    for path, comment in ((c.read_key, f"{c.server_label}/{c.repo}/server-to-github"),
                          (c.actions_key, f"{c.org}/{c.repo}/actions-to-{c.server_label}")):
        if path.exists() or Path(str(path) + ".pub").exists():
            print(f"Preserving existing SSH key: {path}")
        else:
            ssh_as(c.deploy_user, "ssh-keygen", "-t", "ed25519", "-C", comment,
                   "-f", str(path), "-N", "")

    config_file = ssh_dir / "config"
    block = (f"Host {c.ssh_alias}\n    HostName github.com\n    User git\n"
             f"    IdentityFile {c.read_key}\n    IdentitiesOnly yes\n"
             f"    StrictHostKeyChecking yes\n")
    existing = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    if re.search(r"(?m)^\s*Host\s+" + re.escape(c.ssh_alias) + r"\s*$", existing):
        print(f"SSH alias already exists; review manually: {config_file}")
    else:
        with config_file.open("a", encoding="utf-8") as f:
            f.write(("\n" if existing and not existing.endswith("\n\n") else "") + block)
    os.chown(config_file, account.pw_uid, account.pw_gid)
    os.chmod(config_file, 0o600)

    authorized = ssh_dir / "authorized_keys"
    public = Path(str(c.actions_key) + ".pub").read_text(encoding="utf-8").strip()
    lines = authorized.read_text(encoding="utf-8").splitlines() if authorized.exists() else []
    if public not in lines:
        with authorized.open("a", encoding="utf-8") as f:
            f.write(("\n" if lines else "") + public + "\n")
    os.chown(authorized, account.pw_uid, account.pw_gid)
    os.chmod(authorized, 0o600)

    # Do not clone, change parent ACLs, chown an existing site or reset live code.
    c.script_path.parent.mkdir(parents=True, exist_ok=True)
    script_created = safe_write(c.script_path, deployment_script(c), 0o755)
    if script_created:
        run("bash", "-n", str(c.script_path))

    output_dir = ROOT / f"{c.slug}_deployment"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_write(output_dir / "deploy.yml", workflow(c))
    safe_write(output_dir / "SETUP_INSTRUCTIONS.md", guide(c, applied=True))
    print("\nServer-side bootstrap complete; GitHub setup, fingerprint verification, secrets and safe cutover remain MANUAL.")
    print(f"Deploy public key: {c.read_key}.pub")
    print(f"Actions private key for GitHub Secret SSH_KEY (DO NOT COMMIT): {c.actions_key}")
    print(f"Generated workflow and guide: {output_dir}")
    print("Do NOT deploy until GitHub access, production backup and runtime data are verified.")


def main() -> int:
    print("=" * 50)
    print("       Production Deploy Setup CLI")
    print("=" * 50)
    print("[1] Configure deployment on this server")
    print("[2] Generate a setup instruction file")
    print("[3] Exit")
    option = input("Select an option [1-3]: ").strip()
    if option == "3":
        return 0
    if option not in ("1", "2"):
        print("Invalid option.")
        return 2
    try:
        config = collect()
        if option == "2":
            target = ROOT / f"SETUP_{config.slug}.md"
            safe_write(target, guide(config))
            print(f"\nInstruction file location: {target}")
        else:
            apply(config)
    except (ValueError, RuntimeError, subprocess.CalledProcessError, OSError, EOFError, KeyboardInterrupt) as err:
        print(f"\nError: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
