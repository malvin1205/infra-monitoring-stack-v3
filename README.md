# InfraWatch - Infrastructure Monitoring & Alarm Dashboard (v3)

Dashboard monitoring ketersediaan server, website, dan jaringan secara real-time berbasis Flask dan data probe Prometheus (Blackbox Exporter). Dirancang untuk tampilan wallboard TV NOC dengan sirine audio otomatis saat insiden dan notifikasi Telegram.

> **Catatan Cakupan**: Repo ini berisi **InfraWatch** (Flask backend + web console). Prometheus dan Blackbox Exporter adalah service eksternal yang harus sudah berjalan dan dapat diakses oleh container InfraWatch via `PROMETHEUS_URL`.

---

<img width="1920" height="1080" alt="Dashboard Wallboard" src="https://github.com/user-attachments/assets/a79a7f51-4dbe-4b02-b68e-5b2a04212d24" />

<img width="1920" height="1080" alt="Availability Metrics" src="https://github.com/user-attachments/assets/0ce42ca3-36e2-4d00-b3fe-3a9d8e3da3e8" />

<img width="1920" height="1080" alt="Maintenance Window" src="https://github.com/user-attachments/assets/df0d3c5d-85a5-4493-b610-9a209139bf99" />

<img width="1920" height="1080" alt="Dependency Correlation" src="https://github.com/user-attachments/assets/4865a4ab-f06b-4b3d-9272-ab53a354bcf5" />

<img width="1920" height="1080" alt="Telegram Notification Config" src="https://github.com/user-attachments/assets/ba7866d4-998c-4dc7-a69c-0d3cd6104ff3" />

<img width="1919" height="1079" alt="Target Management" src="https://github.com/user-attachments/assets/02c94b1e-5232-4cd1-9aa1-46294870a28d" />

---

## Fitur Utama

- **TV Wallboard Display**: Tampilan status dengan indikator kontras tinggi (Online, Down, Maintenance), live latency (ms), dan HTTP response code.
- **Audio Siren & Acknowledge**: Sirine otomatis berbunyi saat target DOWN, dengan tombol Acknowledge untuk membungkam suara saat penanganan.
- **Notifikasi Telegram**: Kirim alert otomatis (FIRING / RESOLVED) ke bot/channel Telegram secara asynchronous.
- **Maintenance Mode**: Penjadwalan jendela perawatan per target/job untuk mencegah alarm palsu.
- **Dependency / Alert Correlation**: Hubungan parent-child antar host untuk meredam alert turunan saat gateway/parent down.
- **SLA & Availability Engine**: Menghitung persentase uptime (1 jam - 90 hari), sparkline riwayat latensi, dan statistik downtime.
- **Role-Based Access Control (RBAC)**: Pemisahan hak akses berjenjang antara Owner (akun pendiri yang diproteksi permanen), Administrator, dan Read-Only Viewer.
- **Failover Prometheus Endpoint & Per-Endpoint Job Filter**: Dukungan multiple endpoint Prometheus dengan auto-failover, sinkronisasi antar klien, dan preferensi Default Job tersimpan per endpoint.
- **Synthetic Alert Poller**: Poller background bawaan yang langsung mendeteksi status probe tanpa wajib memasang Alertmanager.
- **Liveness & Readiness Probes**: Endpoint `/health/live` dan `/health/ready` terstandar untuk healthcheck container dan orkestrasi Kubernetes.

---

## Prasyarat

1. **Docker & Docker Compose** (v2).
2. **Prometheus & Blackbox Exporter** yang sudah aktif menjalankan probe target (`probe_success`, `probe_duration_seconds`, `probe_http_status_code`).

---

## Setup & Cara Menjalankan

### 1. Clone Repo & Siapkan Environment

```bash
git clone https://github.com/malvin1205/infra-monitoring-stack-v3.git
cd infra-monitoring-stack-v3
cp .env.example .env
```

Sesuaikan nilai di file `.env`:
```ini
PROMETHEUS_URL=http://192.168.1.10:9090
TELEGRAM_BOT_TOKEN=123456789:AAExampleToken
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_ENABLED=true
```

### 2. Jalankan Service

```bash
docker compose up -d
```

Cek status container:
```bash
docker compose ps
```

### 3. Setup Akun Owner Pertama Kali (First-Run)

1. Buka browser dan akses `http://<IP-SERVER>:5000`.
2. Saat pertama kali dijalankan, sistem otomatis memunculkan modal inisialisasi akun.
3. Masukkan **Nama Tampilan**, **Username** (min. 3 karakter), dan **Password** (minimal 12 karakter).
4. Klik **"Initialize & Log In"**. Akun pertama ini secara otomatis dibuat dengan role **Owner** (pemilik/pendiri).
5. Klik **"Masuk & Aktifkan Audio Alarm"** pada splash screen untuk mengizinkan pemutaran audio sirine di browser TV NOC.

---

## Autentikasi & Hak Akses

- **First-Run Owner Setup**: Akun pendiri sistem dibuat langsung saat pertama kali aplikasi diakses via web UI. Akun ini memegang role permanen `owner`.
- **Role Owner (Founding Account)**: Memiliki hak administratif penuh. Akun ini dilindungi secara khusus: tidak dapat dinonaktifkan, role tidak dapat diubah, dan akun ini tidak dapat dimodifikasi oleh admin lain (hanya owner sendiri yang dapat mengubah kredensial profilnya). Instalasi lama yang di-upgrade otomatis mempromosikan akun pertama menjadi `owner`.
- **Role Administrator**: Memiliki hak penuh untuk konfigurasi operasional: menambah/menghapus target, membuat jadwal maintenance, mengubah endpoint Prometheus, mengatur bot Telegram, dan mengelola akun operator lain (`/api/auth/users`). Admin tidak dapat membuat atau memodifikasi akun Owner, serta dilindungi aturan anti-lockout (admin aktif terakhir tidak dapat dinonaktifkan).
- **Role Viewer (Read-only)**: Hanya dapat melihat dashboard monitoring tanpa akses mengubah konfigurasi. Sangat cocok untuk browser yang dipasang di layar TV NOC wallboard.
- **Machine API Key**: Digunakan untuk automasi skrip atau CI/CD.
  - Key otomatis dibuat di `alarm/.api_key` dan `alarm/.webhook_secret`.
  - Lihat key: `cat alarm/.api_key`
  - Atau tentukan key manual melalui variabel `INFRAWATCH_API_KEY` di file `.env`.
  - Gunakan header `X-API-Key: <key>` atau `Authorization: Bearer <key>` saat memanggil REST API.

---

## Daftar Endpoint REST API

| Endpoint | Method | Keterangan |
| --- | --- | --- |
| `/` | `GET` | Tampilan Web Console / Dashboard Wallboard |
| `/instances` | `GET` | Data status target, latency, HTTP code, & status maintenance |
| `/api/availability` | `GET` | Metrik kalkulasi SLA uptime & analisis stabilitas |
| `/api/target-history` | `GET` | Timeline sparkline latensi dan riwayat status target |
| `/api/auth/status` | `GET` | Cek status inisialisasi user dan sesi login saat ini |
| `/api/auth/setup` | `POST` | Setup akun founding owner pertama kali |
| `/api/auth/login` | `POST` | Login user (session-based) |
| `/api/auth/logout` | `POST` | Logout user |
| `/api/auth/me` | `GET` 🔒 | Profil user yang sedang login |
| `/api/auth/users` | `GET` 🔒 / `POST` 🔒 | Manajemen daftar user (khusus Owner & Admin) |
| `/api/auth/users/<id>` | `PATCH` 🔒 | Ubah role / status aktif / password user (Owner diproteksi) |
| `/api/prometheus-targets` | `GET` | Daftar target hasil discovery Prometheus (untuk dropdown Add Target) |
| `/api/targets` | `GET` / `POST` 🔒 / `DELETE` 🔒 | Kelola daftar kurasi target (`targets/websites.yml`) |
| `/api/jobs` | `GET` | Daftar nama job Prometheus (diagnostik curl) |
| `/api/maintenance` | `GET` / `POST` 🔒 | List & pembuatan jadwal Maintenance Window |
| `/api/maintenance/<id>` | `DELETE` 🔒 | Hapus jadwal Maintenance Window |
| `/api/dependencies` | `GET` / `POST` 🔒 | List & pembuatan relasi Parent-Child (korelasi insiden) |
| `/api/dependencies/<id>` | `DELETE` 🔒 | Hapus relasi dependency |
| `/api/endpoints` | `GET` / `POST` 🔒 / `DELETE` 🔒 | Manajemen daftar failover endpoint Prometheus |
| `/api/endpoints/select` | `POST` 🔒 | Ganti endpoint Prometheus aktif secara manual |
| `/api/alerts/ack` | `POST` 🔒 | Acknowledge (bungkam) outage down yang aktif |
| `/api/alerts/unack` | `POST` 🔒 | Batalkan acknowledge sebuah instance |
| `/api/alerts/resolve` | `POST` 🔒 | Paksa-resolve sebuah incident (backstop untuk phantom incident) |
| `/api/audit/logs` | `GET` 🔒 | Jejak audit tindakan operator |
| `/api/sla-targets` | `GET` 🔒 / `<instance>` `PUT` 🔒 / `DELETE` 🔒 | Target SLA availability per-instance |
| `/api/slow-thresholds` | `GET` 🔒 / `<instance>` `PUT` 🔒 / `DELETE` 🔒 | Threshold latensi SlowResponse per-instance |
| `/api/settings/availability` | `GET` 🔒 / `POST` 🔒 | Toggle korelasi Node Exporter untuk availability |
| `/api/telegram` | `GET` 🔒 / `POST` 🔒 | Baca status token & simpan konfigurasi bot Telegram |
| `/api/telegram/test` | `POST` 🔒 | Uji kirim notifikasi pesan ke Telegram |
| `/status` | `GET` | Status global (`NORMAL`, `WARNING`, `CRITICAL`) & active alerts |
| `/logs` | `GET` | Log kejadian insiden |
| `/history` | `GET` | Riwayat insiden lengkap |
| `/webhook`, `/api/webhook` | `POST` | Webhook receiver dari Alertmanager (opsional, butuh header `X-Webhook-Secret`) |
| `/health` | `GET` | Healthcheck: konektivitas Prometheus, storage, poller, & availability aggregator |
| `/health/live` | `GET` | Liveness probe ringan (200 jika proses web aktif) |
| `/health/ready` | `GET` | Readiness probe (200 jika storage database dapat ditulis) |

> 🔒 = Membutuhkan login sesi Administrator atau header `X-API-Key: <key>` / `Authorization: Bearer <key>`.

---

## Manajemen Target (`targets/websites.yml`)

`targets/websites.yml` adalah **daftar kurasi milik InfraWatch**, bukan konfigurasi scrape Prometheus. Isinya: subset target hasil *discovery* Prometheus yang di-*pin* operator lewat tombol **Add Target**. Efek nyata sebuah entri: target tetap tampil di wallboard dan tetap ikut kalkulasi SLA **meskipun** Prometheus berhenti men-scrape-nya (mem-`POST` ulang target yang belum di-hapus tidak mengubah apa-apa — sudah dimonitor). Synthetic poller & dashboard tetap men-scan **semua** target hasil discovery, bukan hanya yang di-pin.

- **Sumber utama data tetap Prometheus.** Poller & dashboard membaca `probe_success` / `probe_duration_seconds` dari `PROMETHEUS_URL`. Sebuah entri di `websites.yml` yang **belum** di-scrape Prometheus manapun akan muncul berstatus **Unknown** (tanpa latency / HTTP code) sampai scrape config Prometheus eksternal Anda menjangkaunya. API `POST /api/targets` mengembalikan field `warning` bila mendeteksi kondisi ini.
- **InfraWatch tidak mem-provision Prometheus.** Tidak ada `file_sd_config` yang di-generate dari repo ini dan tidak ada panggilan `/-/reload`. Bila Anda memang ingin `websites.yml` dipakai Prometheus, wiring `file_sd_config` + reload adalah tanggung jawab konfigurasi Prometheus Anda sendiri (di luar repo ini).
- **Persistensi.** File ini di-*bind mount* (`./alarm/targets:/app/targets`) sehingga selamat dari restart container, tetapi **tidak di-track git** dan tidak masuk image. Setiap `save` menulis `websites.yml.bak` sebagai titik pulih. Untuk migrasi host, salin `alarm/targets/websites.yml` secara manual — kalau tidak, host baru mulai dari daftar kosong (`websites.yml.example`).
- **Hapus target bersifat reversibel.** `DELETE /api/targets` menyembunyikan target dari InfraWatch (tombstone di SQLite) — bukan menghentikan Prometheus men-scrape-nya. `GET /api/targets` mengembalikan daftar `deleted`; mem-`POST` ulang URL yang sama akan memulihkannya.

---

## Troubleshooting Singkat

- **Audio sirine tidak berbunyi di TV:**
  Browser memblokir pemutaran audio otomatis (autoplay policy). Pastikan mengklik tombol splash screen atau tombol icon speaker/unmute di navbar dashboard.
- **Status Prometheus Unhealthy:**
  Cek endpoint `http://<IP-SERVER>:5000/health`. Pastikan URL `PROMETHEUS_URL` di `.env` sudah benar dan dapat dijangkau dari dalam container Docker.
- **Melihat log aplikasi:**
  ```bash
  docker compose logs -f alarm
  ```

---

## Tim & Kontributor

- **dimi** ([@malvin1205](https://github.com/malvin1205)) - Core
- **Fachriyusuf** ([@Fachriyusuf](https://github.com/Fachriyusuf)) - Telegram
