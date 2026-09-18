# AdSpy v2 — Coolify Deploy Guide

Ye guide AdSpy v2 server ko **Coolify** (Docker-based PaaS) par deploy karne ke liye hai.
Saath wala `Dockerfile` server package ka Docker adaptation hai — original package ki
**koi file modify nahi ki gayi**, code `src/` me copy hai.

> **Caddy ki zaroorat NAHI hai.** Coolify ka Traefik reverse proxy + auto-HTTPS sambhalta hai.
> Caddy lagane se **double proxy** banega aur app ka `ProxyFix(x_for=1)` one-hop trust tootega —
> phir login rate-limiter asli visitor ke bajaye proxy ka IP dekhega. Sirf Traefik rakho.

---

## 0. Pehle ye taiyaar rakho

1. Ye folder (`adspy2-docker/`) ek **git repo** me push karo — Coolify git se build karta hai.
   Repo ka structure:
   ```
   adspy2-docker/
   ├── Dockerfile
   ├── .dockerignore
   ├── COOLIFY.md        (ye file)
   └── src/              (server package se copy: app/, migrations/, templates/, static/,
                          requirements.txt, deploy/backup.sh)
   ```
   `src/` isi folder me pehle se copy karke diya gaya hai. Server package update ho to
   `src/` dobara copy karke commit kar do.
2. Ek **domain** (jaise `ads.example.com`) jiska DNS Coolify server par point kare.
3. Do secrets generate karo (Coolify me paste karne ke liye, kahin save karke rakho):
   ```bash
   python3 -c 'import secrets;print(secrets.token_urlsafe(24))'   # admin password
   python3 -c 'import secrets;print(secrets.token_urlsafe(48))'   # secret key
   ```

---

## 1. Coolify me application banao

1. Coolify dashboard → **+ New Resource → Application**.
2. Source: apna git repo chuno.
3. **Build Pack: Dockerfile** select karo.
4. Settings:
   - **Dockerfile Location:** `/Dockerfile` (agar repo root me ye folder hai to `/adspy2-docker/Dockerfile`)
   - **Build Context:** `/adspy2-docker`
   - **Port / Exposes:** `4022` (container ka port — bahar Traefik sambhalega)

---

## 2. Environment variables

Coolify → application → **Environment Variables** me ye dalo:

| Variable | Required? | Value / default | Note |
|---|---|---|---|
| `ADSPY2_SERVER_MODE` | **haan** | `1` | Login gate ON karta hai. Bina iske app local mode me chalega = **koi login nahi**. |
| `ADSPY2_ADMIN_PASSWORD` | **haan** | 20+ chars ka generated secret | Dashboard ka single admin password. |
| `ADSPY2_SECRET_KEY` | **haan** | 48 chars ka generated secret | Login cookie sign karta hai. Password se **alag** hona chahiye. |
| `ADSPY2_PUBLIC_URL` | recommended | `https://ads.example.com` | Dashboard me extension ko dikhane wala URL. `https://` hona chahiye. |
| `ADSPY2_PORT` | nahi | default `4022` | Container ke andar gunicorn ka port. Coolify ka "Port" setting isi se match hona chahiye. |
| `ADSPY2_THREADS` | nahi | default `4` | Gunicorn threads. |
| `ADSPY2_TIMEOUT` | nahi | default `120` | Lambi requests ka timeout (seconds). |

**Bina 3 required vars ke app boot hi nahi hoga** (jaanboojh kar — galti se khula dashboard na chale).

> Advanced: `ADSPY2_DB_PATH` set karne se ek exact DB file pin ho jati hai aur dataset
> switching **disable** ho jati hai. Normal deploy me **mat use karo**.

---

## 3. Persistent volume (sabse zaroori step)

Container ka data directory hai:

```
/app/data
```

*(Verify kiya hua: `app/config.py` me `DATA_DIR = BASE_DIR / "data"`, aur `BASE_DIR` code wale
folder ka parent hai = `/app`. Isme hain: `adspy2.sqlite3` (OLD), `adspy2-new.sqlite3` (NEW),
`active_dataset.txt` (sidecar), `backups/nightly/` (backup output), `media/thumbs/`.)*

Coolify → application → **Storages / Volumes** me add karo:

- **Source (volume name):** `adspy2-data` (koi naam de do)
- **Destination (container path):** `/app/data`

**Volume ke bina har redeploy par saara data (DB, token, backups) ud jayega.** Ye step miss mat karna.

---

## 4. Healthcheck

Dockerfile me `HEALTHCHECK` pehle se hai: `GET /health` (ye endpoint server mode me bhi
bina login ke public hai). Coolify me bhi set kar sakte ho:

- **Type:** HTTP
- **Path:** `/health`
- **Port:** `4022`

---

## 5. Domain + HTTPS

1. Coolify → application → **Domains** me `https://ads.example.com` add karo.
2. Traefik **auto-HTTPS** (Let's Encrypt) dega — kuch aur karne ki zaroorat nahi.
3. Deploy dabao. Pehla boot thoda time lega (pip install + DB create + migrations auto-run hote hain).

---

## 6. Deploy ke baad — pehli baar setup (5 min)

1. `https://ads.example.com` kholo → login page aayega → `ADSPY2_ADMIN_PASSWORD` se login karo.
2. **Settings → Dataset section → NEW DATA par switch karo.**
   Fresh install **OLD DATA (frozen)** par start hota hai — switch kiye bina har scan
   `DATASET_FROZEN` se reject hoga. (Ye UI button `./adspy2 dataset new` CLI ke barabar hai —
   container me exec karne ki zaroorat nahi. Switch karte waqt worker token NEW dataset me
   copy ho jata hai.)
3. **Settings se worker token copy karo** (switch ke BAAD wala — token per-dataset hai).
4. Ghar ke PC par extension me `https://ads.example.com` + token paste karo → Allow → Test → connected.
5. 2–3 pages ka test job banao → data verify karo → phir daily jobs.

---

## 7. Backup (roz, automatic)

`src/deploy/backup.sh` image me `/app/deploy/backup.sh` par hai. Ye SQLite ke online
`.backup` API se WAL-safe snapshot leta hai → output default:

```
/app/data/backups/nightly/   (= volume ke andar, safe)
```

(Env `ADSPY2_BACKUP_DIR` se badal sakte ho, lekin volume ke andar hi rakho.)

**Coolify Scheduled Task** banao:
- **Schedule:** roz subah `0 3 * * *` (03:00)
- **Command:** `/app/deploy/backup.sh`
- **Container:** adspy2 application

**Off-server copy mat bhoolo:** volume ka backup sirf usi server par hai. Hafte me ek baar
`adspy2-data` volume se `.gz` file bahar copy karo (SCP / rclone / Coolify terminal se download).
Server gaya to volume bhi gaya.

---

## 8. Update kaise karein (naya code aane par)

1. Naye server package se `src/` dobara copy karo, commit + push.
2. Coolify me **Redeploy** dabao.
3. Data `/app/data` volume me hai — **kuch nahi udega**. Migrations boot par auto-run hongi.

---

## 9. Security notes

- **Secrets sirf Coolify env me.** Kabhi repo, chat, ya screenshot me nahi.
- **Caddy/nginx mat lagao** — double proxy se `ProxyFix` ka one-hop trust tootega (section 0 dekho).
- **Extra ports expose mat karo** — sirf Traefik (80/443) bahar dikhe.
- Worker token **per-dataset** hai aur DB me plaintext hai — kisi ko diya tha to
  **Settings se rotate** karo (naya token generate karke extension me dobara paste).
- Koi bhi AI agent/browser operator chal raha ho to engagement ke baad token rotate karo.
- Login rate limit (5 fails / 5 min) in-memory hai — container restart par reset hota hai.
  Isliye lamba admin password rakho.
- Extension hamesha `https://` URL use kare — `http://` par token khule me jayega
  (extension khud non-loopback `http://` refuse karta hai).

---

## 10. Troubleshooting

| Dikkat | Wajah / fix |
|---|---|
| Container boot nahi ho raha / crash loop | 3 required env vars set hain? Logs me `ADSPY2_ADMIN_PASSWORD` / `ADSPY2_SECRET_KEY` ka error dikhega. |
| Login page ke bajaye kuch aur | `ADSPY2_SERVER_MODE=1` set hai? Bina iske local mode = no login. |
| Extension ka har scan `DATASET_FROZEN` | Settings me NEW DATA par switch nahi kiya (section 6, step 2). |
| Extension "unauthorized" | Token purane dataset ka hai — switch ke baad Settings se **dobara copy** karo. |
| Batch upload par `413` | Traefik me body-size limit badhao (Coolify → application → advanced / Traefik labels). Batch uploads 32MB tak ja sakte hain. |
| Rate limit sabko ek saath lag raha hai | Traefik ke aage koi aur proxy/CDN to nahi? `ProxyFix` sirf 1 hop trust karta hai. |
| Volume ke baad bhi data gaya | Volume destination exactly `/app/data` hai? Spelling check karo. |

---

*Design details ke liye: `docs/11-server-mode.md` (security), `docs/00-decisions.md` (decision log) —
server package ke andar.*
