# Production Deployment Setup — example-org/example-api-v2

Documentation only: no server changes have been performed.

## Project settings

| Setting | Value |
|---|---|
| GitHub repository | `example-org/example-api-v2` (private recommended; configurable) |
| Project slug | `example-api-v2` |
| Server | `prod-01` |
| Domain | `api.example.com` |
| Checkout directory | `/var/www/php/api.example.com` |
| Deploy script | `/var/www/scripts/deploy-example-api-v2.sh` |
| SSH alias | `github-example-api-v2` |
| SSH server port | `22` |
| GitHub read key | `/home/deploy/.ssh/github_example-api-v2_readonly` |
| Actions login key | `/home/deploy/.ssh/gha_example-api-v2_prod` |
| Docker project no. | `01` |

## 1. Configure GitHub repository (manual)

1. Create a repository (private recommended for production code) named `example-org/example-api-v2`.
2. Create `main` and `develop` branches; set `main` as default.
3. Import/review your rulesets: targets, enforcement, PR reviews, no force push,
   no deletion, and bypass permissions. Use a separate `pull_request` workflow
   for mandatory pre-merge status checks.
4. Allow approved Actions under Settings > Actions > General.
5. Work in feature branches -> `develop`, then open reviewed PRs into `main`.
6. A push/merge to `main` triggers a real deployment. Do not merge the workflow
   until the checkout, `.env`, Storage permissions, backups, and PHP are ready.

## 2. Prepare the Linux server (administrator)

Commands below are examples; inspect existing users, ownership and paths first.

```bash
id deploy || adduser --disabled-password --gecos '' deploy
git --version
composer --version
php -v
install -d -o deploy -g deploy -m 700 /home/deploy/.ssh
install -d -o root -g root -m 755 /var/www/scripts
namei -l /var/www/php/api.example.com
# Apply an ACL only on the specific parent if deploy must create a new checkout:
# setfacl -m u:deploy:rwx /var/www/php
```

Never change ownership of all `/var/www`, and never use `chmod -R 777`.
The checkout is owned by `deploy`; grant the PHP-FPM account write access only
on actual runtime directories (e.g. Storage/cache). Confirm Nginx docroot.
Back up code, uploads and database before a cutover.

## 3. Server -> GitHub (read-only deploy key)

Generate the key as `deploy` without overwriting existing keys:

```bash
# Run this with root; never print the private key.
runuser -u deploy -- ssh-keygen -t ed25519 -C 'prod-01/example-api-v2/server-to-github' -f /home/deploy/.ssh/github_example-api-v2_readonly -N ''
cat /home/deploy/.ssh/github_example-api-v2_readonly.pub
```

Add the public key to **this repository only** under Settings > Deploy keys;
leave **Allow write access** unchecked. Confirm the official GitHub SSH host
fingerprint before accepting `github.com` in `known_hosts`:
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints

Add the following block to `/home/deploy/.ssh/config` (preserve existing blocks):

```sshconfig
Host github-example-api-v2
    HostName github.com
    User git
    IdentityFile /home/deploy/.ssh/github_example-api-v2_readonly
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Test access *after* trusting GitHub's verified host key and adding the deploy key:

```bash
runuser -u deploy -- ssh -T github-example-api-v2
runuser -u deploy -- git ls-remote git@github-example-api-v2:example-org/example-api-v2.git HEAD
```

## 4. First checkout (manual safety checkpoint)

Inspect current data, `.env`, Storage, symlinks, Nginx root, and backups.
Do **not** clone or reset into an occupied production directory.
If the target is new and its parent is writable by deploy:

```bash
PROJECT_DIR=/var/www/php/api.example.com
REPO=git@github-example-api-v2:example-org/example-api-v2.git
# An existing EMPTY destination is safe to remove; never delete a nonempty one.
if [ -d "$PROJECT_DIR" ] && [ -z "$(ls -A "$PROJECT_DIR")" ]; then
    rmdir "$PROJECT_DIR" || echo "Could not remove empty destination"
fi
if [ ! -e "$PROJECT_DIR" ]; then
    runuser -u deploy -- git clone --branch main "$REPO" "$PROJECT_DIR"
else
    echo "Destination exists; inspect it before cloning"
fi
```

Transfer `.env` and runtime data from backup, then set permissions explicitly.
Ensure `.env` and generated uploads are **not tracked by Git**.
Review `.gitignore` case sensitivity and commit `composer.lock`.
Do not run the deploy script until `main` contains real deployable code.

## 5. GitHub Actions -> server (independent SSH key)

```bash
runuser -u deploy --
umask 077
ssh-keygen -t ed25519 -C 'example-org/example-api-v2/actions-to-prod-01' -f /home/deploy/.ssh/gha_example-api-v2_prod -N ''
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
PUBKEY="$(cat /home/deploy/.ssh/gha_example-api-v2_prod.pub)"
grep -qxF "$PUBKEY" ~/.ssh/authorized_keys || printf '%s\n' "$PUBKEY" >> ~/.ssh/authorized_keys
```

Set repository **Actions secrets** (never commit the private key):

| Secret | Value |
|---|---|
| `HOST` | Reachable production SSH host/IP |
| `PORT` | `22` |
| `USERNAME` | `deploy` |
| `SSH_KEY` | Full private contents of `/home/deploy/.ssh/gha_example-api-v2_prod` |
| `SSH_HOST_FINGERPRINT` | Independently verified SSH *server host* fingerprint |

Find the server's fingerprint as administrator (verify out of band):

```bash
for key in /etc/ssh/ssh_host_*_key.pub; do
    ssh-keygen -lf "$key" -E sha256
done
sshd -T | grep -E '^(hostkey|port|listenaddress) ' 
```

Only copy `SHA256:...` from the selected fingerprint line, not the bit length,
key comment or algorithm label. The Action checks the SSH *host* key chosen
for the connection; when multiple host key algorithms are active, confirm the
actual host key presented at the `HOST`/`PORT` from the runner network.
Check `HOST` and `PORT` when seeing `ssh: host key fingerprint mismatch`.
Do **not** disable fingerprint verification. To diagnose the destination, use
a temporary GitHub Actions step (this reads untrusted public keys, not proof):

```yaml
- name: Inspect offered SSH host keys
  env:
    TARGET_HOST: ${{ secrets.HOST }}
    TARGET_PORT: ${{ secrets.PORT }}
  run: |
    ssh-keyscan -T 10 -p "$TARGET_PORT" "$TARGET_HOST" 2>/dev/null | ssh-keygen -lf - -E sha256
```

Compare this output independently with the server's trusted local host keys.
For a **safe SSH-only smoke test**, temporarily change the action's script to
`whoami; hostname; pwd` *before* enabling the real deployment and merge via PR.
Expected SSH user is `deploy`. Re-enable the deployment command only
after all runtime prerequisites are checked.

**Only after** the Secret is saved and tested, securely remove the temporary
Actions **private** key from the server. Keep its public key authorized.

## 6. Deployment script and workflow

Place the generated deployment Bash script at `/var/www/scripts/deploy-example-api-v2.sh`, owned by root,
mode 755. It executes as `deploy`; it uses flock, checks tracked changes,
checks `.env`/Storage, verifies origin, fast-forwards `main`, and runs Composer.
Do not use `sudo` inside it. The script is designed for host PHP/Composer,
not a Docker-only application. Composer project scripts need review.

Place `deploy.yml` at `.github/workflows/deploy.yml` in the repository,
merge it via PR into `main`. It runs on push to `main` only.

Example syntax check and controlled test:

```bash
runuser -u deploy -- bash -n /var/www/scripts/deploy-example-api-v2.sh
# ONLY after backups and an explicit decision to deploy:
# runuser -u deploy -- bash /var/www/scripts/deploy-example-api-v2.sh
```

**Important:** in-place Git fast-forward + `composer install` is *not atomic*.
If Composer fails, the live site may be partially updated. For critical sites,
use versioned releases, atomic symlink switches and a tested rollback path.

## 7. Git hygiene and Docker ports

Storage policy: Git-tracked Storage files are deployed and updated from Git;
server-only files that remain untracked/ignored are preserved. **Do not edit
Git-tracked Storage files on the production server**: the deployment refuses
local tracked modifications. A file from a future Git commit that collides
with a server-only file causes fast-forward to refuse instead of silently
replacing it. `.gitignore` only governs *untracked* paths; it does not protect
a file already in Git. Decide which Storage subpaths are assets vs uploads.

Check `git status --short`, `git ls-files .env Storage`, `.gitignore`, vendor,
node_modules, secrets and uploads before push. If you want previously tracked
uploads to become server-only, back them up and explicitly use `git rm --cached`
from a development checkout after review. Never run `git clean -fdx`.

Docker port convention `3ppss` for project `01`:
HTTP=30101, HTTPS=30102, MySQL=30103, PHP-FPM=30104, PostgreSQL=30105, Redis=30106
Publish only ports that genuinely need a host mapping.

## 8. Troubleshooting reference (root SSH shell)

SSH authorization denied (`Permission denied (publickey)`):

```bash
runuser -u deploy -- ssh -G github-example-api-v2 | grep -E '^(user|hostname|identityfile|identitiesonly) '
runuser -u deploy -- ssh -vT github-example-api-v2
runuser -u deploy -- git ls-remote git@github-example-api-v2:example-org/example-api-v2.git HEAD
```

`GitHub Actions` handshake mismatch: compare server host key fingerprints;
verify `HOST`/`PORT`, and identify the offered algorithm. **Never confuse the
GitHub repository Deploy Key with the target server SSH host key.**

After clone, inspect the checkout without printing secrets:

```bash
runuser -u deploy -- git -C /var/www/php/api.example.com status
runuser -u deploy -- git -C /var/www/php/api.example.com remote -v
ls -ld /var/www/php/api.example.com
for item in .env Storage composer.json composer.lock; do
    test -e /var/www/php/api.example.com/"$item" && echo "[OK] $item" || echo "[MISSING] $item"
done
```

Do not use `exit 1` directly when pasting a one-off snippet into your
interactive root shell; this can close the entire SSH terminal. All install
examples here avoid a top-level `exit`.

## 9. Final verification checklist

- [ ] GitHub `main`/`develop`, repository visibility and rulesets validated.
- [ ] Backups made and Nginx/docroot verified.
- [ ] Deploy user and checkout-parent permissions checked.
- [ ] Separate SSH keys created; GitHub host fingerprint verified.
- [ ] Read-only GitHub Deploy Key registered; `git ls-remote` succeeds.
- [ ] Server host fingerprint verified and Actions secrets registered.
- [ ] `.env` untracked; tracked Storage assets intentional; server-only uploads preserved.
- [ ] `composer.lock` exists on `main` and Composer scripts were reviewed.
- [ ] Deploy script and `.github/workflows/deploy.yml` paths match.
- [ ] PR `develop -> main` merged and Actions result inspected.
- [ ] HTTP health, PHP-FPM logs and deployed commit verified.

Reference: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
