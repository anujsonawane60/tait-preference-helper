# Deploying to AWS EC2

## The short version: you do not upload the PDFs

At runtime the app never opens a school advertisement. It only reads the two cache files.
The PDF folders are needed **only** by `tait_engine.py build`, which you run here on Windows.

| What | Size | Upload? |
|---|---|---|
| `ALL_PDF's/` | 447 MB | **no** |
| `ALL_PDF's_Without_Interview/` | 57 MB | **no** |
| `cache/*.json` | **3.8 MB** | yes |
| code (4 `.py` + requirements) | 0.07 MB | yes |
| **total to upload** | **3.9 MB** | |

Verified by running the app from a folder containing only the code and the caches, with no
PDF directories present: full analysis returned 100 rows / 2,822 posts, identical to here.
The sidebar *Rebuild* buttons show a "folder not found" error there — harmless, and you
rebuild locally anyway.

When advertisements change: rebuild locally, then upload the two JSON files again.

## Sizing

Measured runtime memory:

| Stage | RSS |
|---|---|
| libraries imported | 111 MB |
| both caches loaded | 128 MB |
| one full analysis + 631-row PDF built | **194 MB** |

- **Disk:** ~1.3 GB total (Ubuntu ~1 GB + venv ~420 MB + app 4 MB). The default **8 GB**
  root volume is plenty.
- **RAM:** budget ~250 MB per concurrent user. **t3.small (2 GB) is the right choice.**
  t2.micro/t3.micro (1 GB) will serve one user but is likely to be killed by the OOM
  reaper with two or three at once — Streamlit keeps a separate session per browser tab.
- **CPU:** t3.small's 2 vCPU is fine. Analysis is a dictionary lookup; the only heavy step
  is building a large PDF (~4 s).

## Concurrency

Sized for **3–5 people at once**, which the app is built for:

- Streamlit runs **one process** serving every visitor, so the 128 MB of loaded caches is
  paid once and shared. Only the per-session dataframe and any generated report are
  per-user, roughly 60–70 MB each at peak.
- 5 concurrent users ≈ `128 + 5 × 70` ≈ **480 MB**, comfortably inside t3.small's 2 GB.
- The PDF report is built **on request** (a *Prepare PDF* button), not on every rerun.
  ReportLab is pure Python and holds the GIL, so building reports eagerly while several
  people adjusted filters would have made the app feel slow for everyone.
- `get_prefs` and `build_pdf` caches are bounded (`max_entries` + `ttl`) so a full day of
  use cannot grow memory without limit.

Add a swap file as cheap insurance against a memory spike:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## A note on uploads

Candidates' preference PDFs are personal documents — a name and a full eligible-school
list. Streamlit keeps uploads in memory and never writes them to disk, so nothing
accumulates on the server. Keep it that way: do not add code that saves uploads.

---

## Steps

### 1. Open the firewall

In the EC2 console → your instance → Security → Security groups → inbound rules:

- SSH (22) from **your IP only**, not `0.0.0.0/0`
- If you chose option 2 or 3: HTTP (80) and HTTPS (443) from anywhere
- **Never** open 8501 — nginx will talk to it locally

### 2. Install the runtime

```bash
ssh myserver
sudo apt update && sudo apt install -y python3-venv python3-pip nginx
mkdir -p ~/tait
```

### 3. Upload the app (run this on Windows, in the project folder)

```powershell
scp app.py tait_engine.py tait_report.py requirements.txt myserver:~/tait/
ssh myserver "mkdir -p ~/tait/cache"
scp cache/*.json myserver:~/tait/cache/
```

About 4 MB — a few seconds.

### 4. Install the dependencies

```bash
ssh myserver
cd ~/tait
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python tait_engine.py info      # should list both corpora
```

`info` reading 2,416 and 309 advertisements confirms the caches arrived intact.

### 5. Run it as a service so it survives reboots

```bash
sudo tee /etc/systemd/system/tait.service > /dev/null <<'EOF'
[Unit]
Description=TAIT Preference Helper
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tait
ExecStart=/home/ubuntu/tait/venv/bin/streamlit run app.py \
  --server.port=8501 --server.address=127.0.0.1 \
  --server.headless=true --browser.gatherUsageStats=false \
  --server.maxUploadSize=10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now tait
systemctl status tait --no-pager
```

`--server.address=127.0.0.1` binds it to localhost only, so it cannot be reached from the
internet even if the security group is wrong. `--server.maxUploadSize=10` caps uploads at
10 MB; preference PDFs are under 1 MB.

**If you chose option 1 (private), you are done.** Tunnel in with
`ssh -L 8501:localhost:8501 myserver`.

### 6. Point the subdomain at the server

The apex domain stays where it is — on GitHub Pages, serving the portfolio. Only a new
subdomain is added, so the portfolio is untouched.

In the **Cloudflare** dashboard → your domain → DNS → *Add record*:

| Field | Value |
|---|---|
| Type | `A` |
| Name | `tait` |
| IPv4 address | `<EC2_PUBLIC_IP>` |
| Proxy status | **DNS only (grey cloud)** |
| TTL | Auto |

Grey cloud matters. With the orange cloud on, Cloudflare terminates TLS itself and
certbot's HTTP-01 challenge cannot reach your server, so certificate issuance fails. Get
the certificate first; you can switch the proxy on afterwards if you want.

Check it resolves before continuing:

```bash
nslookup tait.yourdomain.com     # must return your EC2 IP, not Cloudflare's
```

### 7. nginx in front

```bash
sudo tee /etc/nginx/sites-available/tait > /dev/null <<'EOF'
server {
    listen 80;
    server_name tait.yourdomain.com;
    client_max_body_size 12M;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/tait /etc/nginx/sites-enabled/tait
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

The `Upgrade`/`Connection` headers are required — Streamlit uses a WebSocket, and without
them the page loads but never finishes connecting.

### 8. HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d tait.yourdomain.com
```

Certbot rewrites the nginx config for port 443 and sets up automatic renewal. The app is
then live at `https://tait.yourdomain.com`.

### Optional: put a password on it

If you would rather only you and your helpers could use it:

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd yourname     # prompts for a password
sudo sed -i '/location \/ {/a\        auth_basic "TAIT";\n        auth_basic_user_file /etc/nginx/.htpasswd;' \
  /etc/nginx/sites-available/tait
sudo nginx -t && sudo systemctl reload nginx
```

### Taking it down again

The app is only meant to run for a day or two at a time. To stop serving without
destroying anything:

```bash
sudo systemctl stop tait && sudo systemctl disable tait
```

Bring it back with `sudo systemctl enable --now tait`. Stopping the EC2 instance itself
also stops the bill for compute; the EBS volume keeps charging while it exists.

---

## Updating later

```powershell
scp app.py tait_engine.py tait_report.py myserver:~/tait/
scp cache/*.json myserver:~/tait/cache/          # only if you rebuilt
ssh myserver "sudo systemctl restart tait"
```

## Checking on it

```bash
sudo systemctl status tait          # running?
sudo journalctl -u tait -f          # live logs
free -m                             # memory headroom
```

If the page loads but sits on "Please wait…", it is the WebSocket — re-check the `Upgrade`
headers in the nginx config.

If the service keeps restarting, look for `Killed` in `journalctl` — that is the OOM
reaper, and it means the instance is too small. Move to t3.small.
