# GENGHIS remote access + auth (D25 slice 3)

**There are two front doors; this file documents the Cloudflare one.** Pick in
[INSTALL.md → Reach it from anywhere](../../INSTALL.md#reach-it-from-anywhere-optional--two-front-doors):
- **Tailscale** — a private network between your own devices; nothing to expose, no domain needed. The reference
  fleet runs this. `tailscale serve` authenticates the visitor and injects `Tailscale-User-Login` (**verified
  2026-09-22**) — but it cannot add a secret of your own and proxies from `127.0.0.1`, so identity alone is not
  enforcement: with `proxy_secret` empty everyone still gets `local_role`. For real roles on a shared tailnet add a
  one-hop local proxy — [`tailscale-serve-caddy.example.Caddyfile`](tailscale-serve-caddy.example.Caddyfile) +
  [`genghis-auth.tailscale.example.json`](genghis-auth.tailscale.example.json).
- **Cloudflare Tunnel + Access** — below: a login at the edge on your own domain, for people you will *not* put on
  your network. Header: `Cf-Access-Authenticated-User-Email`.

Everything after this point — the role map, `proxy_secret`, `/whoami`, the oauth2-proxy variant — is the **same**
for both; only the header name and the proxy in front change.

## Cloudflare Tunnel + Access

> **Status: reference blueprint.** These files are a tested-in-pattern, *not-yet-run-in-this-repo* guide —
> they need YOUR Cloudflare account + domain, so adapt and verify in your environment. GENGHIS's side (the
> role layer) is built and tested; this wires a front door to it.

**What you get:** reach the GENGHIS Control Room, chat, and dashboards from anywhere — with **no open ports
and your home IP never exposed** (cloudflared dials *out*) — behind a login that only your chosen identities
pass, mapped to GENGHIS roles (viewer / operator / admin).

**Local stays the default.** None of this changes the home box until you turn it on. Direct-on-the-LAN access
is still full admin (D25 `local_role`). This is the opt-in "office / remote" mode.

```
  Internet ─▶ Cloudflare Access ─(authenticates: you@example.com)─▶ Cloudflare Tunnel
                                                                          │ (outbound-only)
                                          cloudflared on your host ◀──────┘
                                                     │  forwards Cf-Access-Authenticated-User-Email
                                                     ▼
                              GENGHIS coordinator :8899  →  email → role (admin)
```

---

## 1 · Create the tunnel

Install cloudflared on the coordinator host, then:
```bash
cloudflared tunnel login
cloudflared tunnel create genghis           # note the TUNNEL_ID it prints
cloudflared tunnel route dns genghis genghis.example.com
cloudflared tunnel route dns genghis chat.example.com
cloudflared tunnel route dns genghis grafana.example.com
```
Copy `cloudflared-config.example.yml` → `cloudflared-config.yml`, fill in your `TUNNEL_ID` + credentials path,
then run it (`cloudflared --config ./cloudflared-config.yml tunnel run`, or install as a service).

## 2 · Protect it with Cloudflare Access

In the **Zero Trust dashboard** → **Access → Applications → Add a self-hosted application**:
- **Application domain:** `genghis.example.com` (repeat for `chat.` and `grafana.`, or add all three subdomains).
- **Identity provider:** add one under **Settings → Authentication** first — for you, **Azure AD / Entra ID**
  (or one-time-PIN email, or Google — Access just needs to verify `you@example.com`).
- **Policy:** *Allow* where **Emails = `you@example.com`** (add teammates or a group later). Everything else
  is denied at Cloudflare's edge — traffic for anyone else never reaches your LAN.

Access now injects **`Cf-Access-Authenticated-User-Email`** on every allowed request.

## 3 · Add the anti-spoof header (defense in depth)

So a device on your LAN can't hit `:8899` directly and *claim* to be you, add a secret header that only
Cloudflare sends. **Zero Trust → (or the domain's) Rules → Transform Rules → Modify Request Header → Add:**
- Header `X-Genghis-Proxy` = value `<a long random string>` — when hostname is `genghis.example.com`.

Also **firewall `:8899` (and `:3080`, `:3000`) so only the tunnel/localhost reaches them** — cloudflared runs
on the host, so `localhost` is enough; block those ports from the rest of the LAN. Belt (the header) **and**
suspenders (the firewall).

## 4 · Flip GENGHIS into proxy mode

Only now — with the tunnel + Access live — apply the auth block (`genghis-auth.example.json`). Set
`proxy_secret` to the **same** random string as the Transform Rule, then POST it:
```bash
curl -s http://localhost:8899/config -H "Content-Type: application/json" \
  -d '{"auth":{"user_header":"Cf-Access-Authenticated-User-Email",
               "roles_by_user":{"you@example.com":"admin"},
               "default_role":"viewer",
               "proxy_secret":"<the same random string>"}}'
```
From now on: a request through Cloudflare (carrying the secret + your verified email) → **admin**; a direct LAN
hit without the secret → **viewer** (read-only), never admin. Verify at `https://genghis.example.com/whoami` —
it should show `you@example.com` / `admin`.

> **To undo / regain local admin:** POST `{"auth":{"proxy_secret":""}}` **with** the
> `X-Genghis-Proxy: <secret>` header (an admin call). Clearing `proxy_secret` returns the box to local mode.

## 5 · (Slice 4) One login for everything — SSO Open WebUI + Grafana

Add `chat.example.com` and `grafana.example.com` as Access applications too (step 2, same policy), then layer the
**SSO overlay** so those two services trust the same Cloudflare-forwarded email instead of prompting for their
own login:

```bash
# from the repo root, with your auth edge already live:
docker compose -f docker-compose.yml -f deploy/auth/docker-compose.sso.yml up -d
```

That override (`docker-compose.sso.yml`) sets:
- **Open WebUI** → `WEBUI_AUTH=True` + `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` = the edge's email header. The first
  email to sign in becomes its admin; everyone else is a normal user. No separate password.
- **Grafana** → `GF_AUTH_PROXY_ENABLED` trusting the same header, auto-creating accounts as **Viewer**
  (promote yourself to Grafana Admin once under *Server Admin → Users*). The built-in `admin/admin` stays as
  break-glass until you set `GF_AUTH_DISABLE_LOGIN_FORM=true`.

**Header name:** defaults to Cloudflare's `Cf-Access-Authenticated-User-Email`. For the self-hosted
oauth2-proxy path, run the overlay with `TRUSTED_EMAIL_HEADER=X-Auth-Request-Email`.

**Security (same rule as the coordinator):** the edge must strip inbound copies of this header (Cloudflare &
Caddy do), and **`:3080` / `:3000` must be reachable only via the tunnel/localhost** — firewall them from the
LAN, and set Grafana's `GF_AUTH_PROXY_WHITELIST` to your docker bridge gateway
(`docker network inspect genghis_default`). Otherwise a LAN device could set the header directly. To revert:
bring the stack up without the `-f deploy/auth/docker-compose.sso.yml` file.

Result: one Cloudflare login → Control Room, chat, **and** dashboards, all as `you@example.com`.

---

## Role reference

| Role | Can |
|---|---|
| `viewer` | see the Control Room, fleet, models, dashboards — **read-only** |
| `operator` | + change which model each goal runs, warm/unload models |
| `admin` | + node names/roles, default goal, the auth config itself, `/admin` |

Map more people in `roles_by_user` (email → role) or, if you use Entra **group** claims instead of email,
`roles_by_group` (group GUID → role). GENGHIS takes the **highest** role any of a user's matches grants.

## Alternative: self-hosted (no Cloudflare) — oauth2-proxy + Caddy

If you'd rather not depend on Cloudflare (pure LAN SSO, or your own edge), the `oauth2-proxy/` folder has a
Caddy + oauth2-proxy overlay that authenticates against **Entra ID or LDAP** and injects
`X-Auth-Request-User` / `X-Auth-Request-Groups` — the same headers GENGHIS reads. Same role layer, different
front door. See `oauth2-proxy/README.md`.

## Security notes
- **Never port-forward `:8899` to the internet.** The whole point of the tunnel is that you don't.
- The `proxy_secret` + firewall close the LAN-spoof gap. The most robust future hardening is validating the
  `Cf-Access-Jwt-Assertion` signature in the coordinator (a follow-up); the secret-header model is the
  pragmatic draft.
- `proxy_secret` is never served (`/config.json` redacts the whole auth token/secret set, like the PIN).
