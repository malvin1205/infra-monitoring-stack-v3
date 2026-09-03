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
- **Role-Based Access Control (RBAC)**: Pemisahan role Administrator dan Read-Only Viewer.
- **Failover Prometheus Endpoint**: Dukungan multiple endpoint Prometheus dengan auto-failover jika server utama tidak dapat diakses.
- **Synthetic Alert Poller**: Poller background bawaan yang langsung mendeteksi status probe tanpa wajib memasang Alertmanager.

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

### 3. Setup Akun Admin Pertama Kali (First-Run)

1. Buka browser dan akses `http://<IP-SERVER>:5000`.
2. Saat pertama kali dijalankan, sistem otomatis memunculkan pop-up **"Create Administrator Account"**.
3. Masukkan **Nama Tampilan**, **Username Admin**, dan **Password** (minimal 6 karakter).
4. Klik **"Create Admin Account"**. Anda akan langsung login sebagai Administrator.
5. Klik **"Masuk & Aktifkan Audio Alarm"** pada splash screen untuk mengizinkan pemutaran audio sirine di browser TV NOC.

---

## Autentikasi & Hak Akses

- **First-Run Admin Setup**: Akun administrator utama dibuat langsung melalui web UI saat instalasi pertama.
- **Role Administrator**: Memiliki hak penuh untuk menambah/menghapus target, membuat jadwal maintenance, mengubah endpoint Prometheus, mengatur bot Telegram, dan mengelola user lain (`/api/auth/users`).
- **Role Viewer (Read-only)**: Hanya dapat melihat dashboard monitoring tanpa akses mengubah konfigurasi. Cocok untuk browser yang dipasang di layar TV NOC wallboard.
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
| `/api/auth/setup` | `POST` | Setup akun administrator pertama kali |
| `/api/auth/login` | `POST` | Login user (session-based) |
| `/api/auth/logout` | `POST` | Logout user |
| `/api/auth/users` | `GET` 🔒 / `POST` 🔒 | Manajemen daftar user (khusus Admin) |
| `/api/targets` | `GET` / `POST` 🔒 / `DELETE` 🔒 | Kelola daftar target monitoring (`targets/websites.yml`) |
| `/api/maintenance` | `GET` / `POST` 🔒 | List & pembuatan jadwal Maintenance Window |
| `/api/maintenance/<id>` | `DELETE` 🔒 | Hapus jadwal Maintenance Window |
| `/api/dependencies` | `GET` / `POST` 🔒 | List & pembuatan relasi Parent-Child (korelasi insiden) |
| `/api/dependencies/<id>` | `DELETE` 🔒 | Hapus relasi dependency |
| `/api/endpoints` | `GET` / `POST` 🔒 / `DELETE` 🔒 | Manajemen daftar failover endpoint Prometheus |
| `/api/endpoints/select` | `POST` 🔒 | Ganti endpoint Prometheus aktif secara manual |
| `/api/telegram` | `GET` 🔒 / `POST` 🔒 | Baca status token & simpan konfigurasi bot Telegram |
| `/api/telegram/test` | `POST` 🔒 | Uji kirim notifikasi pesan ke Telegram |
| `/status` | `GET` | Status global (`NORMAL`, `WARNING`, `CRITICAL`) & active alerts |
| `/logs` | `GET` | Log kejadian insiden |
| `/history` | `GET` | Riwayat insiden lengkap |
| `/webhook` | `POST` | Webhook receiver dari Alertmanager (opsional) |
| `/health` | `GET` | Healthcheck konektivitas Prometheus, storage, dan poller |

> 🔒 = Membutuhkan login sesi Administrator atau header `X-API-Key: <key>` / `Authorization: Bearer <key>`.

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
