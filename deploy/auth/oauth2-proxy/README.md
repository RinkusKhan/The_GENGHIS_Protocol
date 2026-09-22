# Self-hosted auth edge — oauth2-proxy + Caddy (D25 slice 3 alternative)

The **no-Cloudflare** path: your own reverse proxy does the Entra/LDAP login and injects the identity headers
GENGHIS reads. Same role layer as the Cloudflare path — different front door. Use this for pure-LAN SSO or
when you don't want a cloud dependency. (For remote access with zero open ports, prefer the Cloudflare path in
`../README.md`.)

## Entra app registration
1. Entra → **App registrations → New**. Redirect URI (Web): `https://<DOMAIN>/oauth2/callback`.
2. **Certificates & secrets → New client secret** → copy into `CLIENT_SECRET`.
3. **Token configuration → Add groups claim** (so the token carries group GUIDs) — or skip it and map by
   email in GENGHIS `roles_by_user` instead.
4. Copy the **Tenant ID** + **Application (client) ID**.

## Run
```bash
cp genghis-auth.env.example genghis-auth.env      # fill in tenant/client/secret/cookie/proxy secrets
docker compose --env-file genghis-auth.env -f docker-compose.auth.yml up -d
```
Point DNS (or hosts) for `DOMAIN` at the Caddy host. Then flip GENGHIS into proxy mode with the SAME
`proxy_secret`, mapping your **Entra group GUIDs** to roles:
```bash
curl -s http://localhost:8899/config -H "Content-Type: application/json" \
  -d '{"auth":{"user_header":"X-Auth-Request-User","groups_header":"X-Auth-Request-Groups",
               "roles_by_group":{"<admins-group-guid>":"admin","<ops-group-guid>":"operator"},
               "default_role":"viewer","proxy_secret":"<same as PROXY_SECRET>"}}'
```
Find a group's GUID in Entra → Groups → (the group) → **Object ID**. GENGHIS grants the **highest** role any
of the user's groups matches.

## Notes
- Caddy **strips** inbound `X-Auth-Request-*` / `X-Genghis-Proxy` before setting the verified values — a
  client can't spoof them. Still firewall `:8899` so only the proxy reaches it.
- LDAP instead of Entra: point oauth2-proxy (or drop in **Authentik/Authelia**, which front LDAP + local +
  Azure in one) at your directory; GENGHIS is unchanged.
- Pinned images (D18): `oauth2-proxy v7.7.1`, `caddy 2.8`.
