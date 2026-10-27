# Production Deployment Setup — example-org/example-api

Documentation only: no server changes have been performed.

## Project settings

| Setting | Value |
|---|---|
| GitHub repository | `example-org/example-api` (private recommended; configurable) |
| Project slug | `example-api` |
| Server | `prod-01` |
| Domain | `api.example.com` |
| Checkout directory | `/var/www/php/api.example.com` |
| Deploy script | `/var/www/scripts/deploy-example-api.sh` |
| SSH alias | `github-example-api` |
| SSH server port | `22` |
| GitHub read key | `/home/deploy/.ssh/github_example-api_readonly` |
| Actions login key | `/home/deploy/.ssh/gha_example-api_prod` |
| Docker project no. | `01` |

## 1. Configure GitHub repository (manual)

1. Create a repository (private recommended for production code) named `example-org/example-api`.
2. Create `main` and `develop` branches; set `main` as default.
3. Import/review your rulesets: targets, enforcement, PR reviews, no force push,
   no deletion, and bypass permissions. Use a separate `pull_request` workflow
   for mandatory pre-merge status checks.
4. Allow approved Actions under Settings > Actions > General.
5. Work in feature branches -> `develop`, then open reviewed PRs into `main`.

## 2. Prepare the Linux server (administrator)

Commands below are examples; inspect existing users, ownership and paths first.

```bash
id deploy || sudo adduser --disabled-password --gecos '' deploy
git --version
composer --version
php -v
sudo install -d -o deploy -g deploy -m 700 /home/deploy/.ssh
sudo install -d -o root -g root -m 755 /var/www/scripts
namei -l /var/www/php/api.example.com
# Apply an ACL only on the specific parent if deploy must create a new checkout:
# sudo setfacl -m u:deploy:rwx /var/www/php
```

Never change ownership of all `/var/www`, and never use `chmod -R 777`.
The checkout is owned by `deploy`; grant the PHP-FPM account write access only
on actual runtime directories (e.g. Storage/cache). Confirm Nginx docroot.
Back up code, uploads and database before a cutover.

## 3. Server -> GitHub (read-only deploy key)

Generate the key as `deploy` without overwriting existing keys:

```bash
sudo -iu deploy
umask 077
mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -C 'prod-01/example-api/server-to-github' -f /home/deploy/.ssh/github_example-api_readonly -N ''
cat /home/deploy/.ssh/github_example-api_readonly.pub
```

Add the public key to **this repository only** under Settings > Deploy keys;
leave **Allow write access** unchecked. Confirm the official GitHub SSH host
fingerprint before accepting `github.com` in `known_hosts`:
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints

Add the following block to `/home/deploy/.ssh/config` (preserve existing blocks):

```sshconfig
Host github-example-api
    HostName github.com
    User git
    IdentityFile /home/deploy/.ssh/github_example-api_readonly
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Test access *after* trusting GitHub's verified host key and adding the deploy key:

```bash
sudo -iu deploy ssh -T github-example-api
sudo -iu deploy git ls-remote git@github-example-api:example-org/example-api.git HEAD
```

## 4. First checkout (manual safety checkpoint)

Inspect current data, `.env`, Storage, symlinks, Nginx root, and backups.
Do **not** clone or reset into an occupied production directory.
If the target is new and its parent is writable by deploy:

```bash
sudo -iu deploy git clone --branch main --single-branch git@github-example-api:example-org/example-api.git /var/www/php/api.example.com
```

Transfer `.env` and runtime data from backup, then set permissions explicitly.
Ensure `.env` and generated uploads are **not tracked by Git**.
Review `.gitignore` case sensitivity and commit `composer.lock`.
Do not run the deploy script until `main` contains real deployable code.

## 5. GitHub Actions -> server (independent SSH key)

```bash
sudo -iu deploy
umask 077
ssh-keygen -t ed25519 -C 'example-org/example-api/actions-to-prod-01' -f /home/deploy/.ssh/gha_example-api_prod -N ''
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
PUBKEY="$(cat /home/deploy/.ssh/gha_example-api_prod.pub)"
grep -qxF "$PUBKEY" ~/.ssh/authorized_keys || printf '%s\n' "$PUBKEY" >> ~/.ssh/authorized_keys
```

Set repository **Actions secrets** (never commit the private key):

| Secret | Value |
|---|---|
| `HOST` | Reachable production SSH host/IP |
| `PORT` | `22` |
| `USERNAME` | `deploy` |
| `SSH_KEY` | Full private contents of `/home/deploy/.ssh/gha_example-api_prod` |
| `SSH_HOST_FINGERPRINT` | Independently verified SSH *server host* fingerprint |

Find the server's fingerprint as administrator (verify out of band):

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

**Only after** the Secret is saved and tested, securely remove the temporary
Actions **private** key from the server. Keep its public key authorized.

## 6. Deployment script and workflow

Place the generated deployment Bash script at `/var/www/scripts/deploy-example-api.sh`, owned by root,
mode 755. It executes as `deploy`; it uses flock, checks tracked changes,
checks `.env`/Storage, verifies origin, fetches `main`, and runs Composer.
Do not use `sudo` inside it. The script is designed for host PHP/Composer,
not a Docker-only application. Composer project scripts need review.

Place `deploy.yml` at `.github/workflows/deploy.yml` in the repository,
merge it via PR into `main`. It runs on push to `main` only.

Example syntax check and controlled test:

```bash
sudo -u deploy bash -n /var/www/scripts/deploy-example-api.sh
# ONLY after backups and an explicit decision to deploy:
# sudo -iu deploy bash /var/www/scripts/deploy-example-api.sh
```

**Important:** in-place `git reset --hard` + `composer install` is *not atomic*.
If Composer fails, the live site may be partially updated. For critical sites,
use versioned releases, atomic symlink switches and a tested rollback path.

## 7. Git hygiene and Docker ports

Check `git status --short`, `git ls-files .env Storage`, `.gitignore`, vendor,
node_modules, secrets and uploads before push. `.gitignore` does not untrack
previously committed files; use `git rm --cached` after review when needed.

Docker port convention `3ppss` for project `01`:
HTTP=30101, HTTPS=30102, MySQL=30103, PHP-FPM=30104, PostgreSQL=30105, Redis=30106
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
