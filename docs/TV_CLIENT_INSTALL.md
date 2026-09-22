# Tizen TV Client — Install & Config

How our Tizen app gets onto a Samsung TV. Covers the **HEARTH surface** (agent → TV display) and the
future **GENGHIS `STORE` role** (model repository, *only* once a USB drive is added — see [D8](../DECISIONS.md)).

Two audiences, two parts:
- **Part A — Developer runbook** — the exact, reproducible path we walked (needs the Tizen SDK). ✅ done & verified.
- **Part B — Toward a no-Visual-Studio end-user install** — the design problem, honestly. ⬜ to figure out.

---

## 0. Compatibility

| | |
|---|---|
| **Build target (TFM)** | `tizen60` (Tizen **6.0**) — **build once here** and it runs on **6.0, 6.5, 7.0, 8.0, 9.0, 10.0 (TV + Tizen)**. Tizen is backward-compatible; targeting the floor maximizes reach. |
| **Verified on** | Samsung **UN50CU7000** (CU7000, 2023) · Tizen **9.0** · Crystal Processor 4K · `armv7` / **32-bit** userspace · 4 cores · ~1.5 GB RAM |
| **Hard constraints** | 32-bit `armv7` userspace (any *native* engine must be armv7, never aarch64) · **no root, no shell** · **signed apps only** · a sandboxed app sees only its storage quota (external USB needs the `externalstorage` privilege) |

---

## Part A — Developer runbook (reproducible)

Prereqs on the dev PC: **Tizen Studio** (or VS + Tizen extension) providing `sdb`, the `tizen` CLI, and the
**Certificate Manager**; the **Samsung Certificate Extension**; and **.NET** (`dotnet`) for the build.
Our paths: `sdb` = `C:\tizen-studio\tools\sdb.exe`; `tizen` CLI (that shares the VS-extension profile store) =
`C:\Users\<you>\.tizen-extension-platform\server\sdktools\data\tools\ide\bin\tizen.bat`.

### 1. Put the TV in Developer Mode
On the TV: **Apps → type `12345` on the remote → Developer Mode ON → enter your PC's LAN IP** as the host,
then **restart the TV** (the sdb daemon only comes up on reboot). The TV whitelists that PC IP.

### 2. Connect over the LAN
```bash
sdb connect <TV_IP>            # the TV's LAN address (sdbd listens on :26101)
sdb devices                   # should list the TV, e.g. <TV_IP>:26101  device  <MODEL>
```
Troubleshooting: `Test-NetConnection <TV_IP> -Port 26101` — if the port is **closed**, Dev Mode isn't up
yet → confirm the **host IP matches your PC** and **reboot the TV**.

### 3. Read the device identity + specs
```bash
sdb -s <TV> capability          # arch (armv7/arm_32), platform_version, root=disabled, sockets=enabled
sdb -s <TV> shell 0 getduid     # DUID — needed to bind the distributor cert (a 13-character id)
```

### 4. Create the Samsung certificate (the gate)
Launch the **Certificate Manager** → **Create a new Certificate** → **profile type: SAMSUNG** →
- **Author** cert: new author name + password → **sign in to your Samsung account**.
- **Distributor** cert: Samsung account → **Privilege: Public** → enter the **DUID** from step 3 → Finish.

This writes `C:\Users\<you>\SamsungCertificate\<profile>\` (author.p12 + distributor.p12) and activates the
profile. **The distributor cert is bound to that DUID** — the signed app installs only on that TV. (This is
the crux for productization — see Part B.)

### 5. Build the `.tpk`
```bash
dotnet build <Project>.csproj -c Debug      # produces bin/Debug/tizen60/<pkgid>-<ver>.tpk
```

### 6. Re-sign with the Samsung profile
The build signs with a default/test cert (emulator-only). Re-sign for the real TV:
```bash
tizen package -t tpk -s <profile> -- <path-to-.tpk>     # e.g. -s genghis-tv
```

### 7. Install & launch
```bash
sdb -s <TV> install <path-to-.tpk>          # app_id[...] install completed
tizen run -p <pkgid> -s <TV>                # ... successfully launched pid = N
```
The app runs **full-screen**. (Our app wires the remote's **BACK / Escape** key to exit.) To stop remotely:
```bash
sdb -s <TV> shell 0 kill <pkgid>
```

### Gotchas banked
- Reboot the TV after enabling Dev Mode, or `:26101` stays closed.
- `sdb shell` is **restricted** on retail sets (and the emulator) — no arbitrary shell/root; everything goes
  through **signed apps**. Read device facts via app APIs (`Tizen.System`), not shell.
- Default test certs install on the **emulator** but are **rejected by a real TV** — a Samsung DUID-bound
  cert is mandatory for hardware.
- `dotnet build` works headless and targets `tizen60` (no VS needed for the *build* itself).

---

## Part B — Toward a no-Visual-Studio end-user install (design, ⬜ open)

Goal: a normal person installs the client on their Samsung TV **without VS, without the Tizen SDK, without
building anything.** The blocker is **not** the app — it's **Samsung's signing/distribution model.**

**The crux:** Samsung apps need an **author** cert (dev identity) + a **distributor** cert. For dev/sideload
the distributor cert is **DUID-bound** — valid only for the specific TV(s) whose DUIDs it lists. So a single
prebuilt `.tpk` we ship **cannot** install on a stranger's TV as-is. Three ways out, escalating:

| Tier | Who it's for | How | Cost / gate |
|---|---|---|---|
| **1 · Dev path** (today) | us + tinkerers | Part A above | needs Tizen SDK + manual cert |
| **2 · Prosumer installer** | enthusiasts w/ a Samsung account | ship a small bundled tool: our prebuilt `.tpk` + the `sdb`/`tizen` CLIs + a **wizard** that automates Dev-Mode guidance → reads DUID → **creates a Samsung cert for the user's own DUID** (their Samsung login) → signs → installs over sdb/USB | **no VS**, but still needs the user's **Samsung account** + Dev Mode; cert-gen automation against Samsung's flow is the unknown |
| **3 · Store distribution** | everyone | publish to the **Samsung Smart TV / Tizen app store**; the store issues a **public distributor cert** valid on **all** TVs → normal "install from store" UX, no Dev Mode, no per-user cert | requires a **Samsung Seller/partner account + app certification/review** — this is D5's "partner-gated," and it was right *about distribution*, just not about dev access |

**Design lean:** Tier 2 is the realistic near-term "share it with friends" path (self-contained installer,
no dev tools); Tier 3 is the mass-market endgame if this becomes a product. Both ship the **same `tizen60`
`.tpk`** (built once, runs 6.0→10.0).

### Open questions to figure out (honest TODO)
- **USB sideload signing:** does Dev-Mode **USB install** (the `userwidget` folder path) still require a
  DUID-bound distributor cert, or is it more permissive? (If permissive, it simplifies Tier 2.) — *verify.*
- **Cert-gen automation:** can the Samsung author/distributor cert creation be scripted/embedded (headless
  or minimal-UI) for a Tier-2 wizard, or does it always require the interactive Certificate Manager? — *verify.*
- **Partner/public distributor certs:** can a single distributor cert cover **many/all** DUIDs outside the
  store (partner level), avoiding per-device signing without full store publication? — *verify.*
- **Store certification bar:** what does Samsung's TV app review require (privileges, content, background
  execution)? Relevant if the client needs a background service (future GENGHIS `STORE` role). — *verify.*
- **Dev-Mode expiry:** Samsung Dev Mode can time out / require re-enabling; confirm behavior for a long-lived
  install. — *verify.*

### The `STORE`-role prerequisite (from D8) — ✅ VALIDATED with a USB (2026-09-02)
Independent of distribution: the TV becomes a real GENGHIS **`STORE`** (model-repo) node only if a **USB
drive is plugged in** — internal storage is tiny (~3.8 GB total, ~0.9 GB free). **Confirmed working:**
1. Add `<privileges><privilege>http://tizen.org/privilege/externalstorage</privilege></privileges>` to
   `tizen-manifest.xml`.
2. Rebuild → re-sign → redeploy.
3. The sandboxed app then **sees the USB** and can **read/write arbitrary files** on it — verified with a
   write→read→delete test.

Measured result: `External 28.9 / 28.9 GB free`, mount path **`/opt/media/USBDriveA1`**, privilege
**auto-granted (no user prompt)**. A bigger drive just yields a bigger repo — same code path. Next: wire the
actual repo service (index + serve GGUFs to the fabric). Note for Tier 3: confirm the `externalstorage`
privilege passes Samsung store certification.

---

*Status: Part A verified 2026-09-02 (HEARTH surface running on UN50CU7000). Part B is a design sketch — the
Samsung distribution end is the thing to work out before this installs for other people.*
