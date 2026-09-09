# InfraWatch - Infrastructure Monitoring & Alarm Dashboard (v3)

Dashboard monitoring ketersediaan server, website, dan jaringan secara real-time berbasis Flask dan data probe Prometheus (Blackbox Exporter). Dirancang untuk tampilan wallboard TV NOC dengan sirine audio otomatis saat insiden dan notifikasi Telegram.

> **Catatan Cakupan**: Repo ini berisi **InfraWatch** (Flask backend + web console). Prometheus dan Blackbox Exporter adalah service eksternal yang harus sudah berjalan dan dapat diakses oleh container InfraWatch via `PROMETHEUS_URL`.

---

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/e6e97e26-b230-41c5-ad9d-0c90aee830e7" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/36739564-8b7d-48ba-820a-14feb0683b28" />

<img width="1917" height="1078" alt="image" src="https://github.com/user-attachments/assets/eaf7a1e3-79f1-4e79-9057-b9047cc4c66a" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/237ac261-f64c-4f8d-a1a5-d13366dc19a0" />

<img width="1920" height="1080" alt="Telegram Notification Config" src="https://github.com/user-attachments/assets/ba7866d4-998c-4dc7-a69c-0d3cd6104ff3" />

<img width="1919" height="1079" alt="Target Management" src="https://github.com/user-attachments/assets/02c94b1e-5232-4cd1-9aa1-46294870a28d" />

---

## Fitur Utama

- **TV Wallboard Display**: Tampilan status dengan indikator kontras tinggi (Online, Down, Maintenance), live latency (ms), dan HTTP response code.
- **Audio Siren & Acknowledge**: Sirine otomatis berbunyi saat target DOWN, dengan tombol Acknowledge untuk membungkam suara saat penanganan.
- **Notifikasi Telegram**: Kirim alert otomatis (FIRING / RESOLVED) ke bot/channel Telegram secara asynchronous.
- **Maintenance Mode**: Penjadwalan jendela perawatan per target/job untuk mencegah alarm palsu.
- **Dependency / Alert Correlation**: Hubungan parent-child antar host untuk meredam alert turunan saat gateway/parent down.
- **Deep SLA & Availability Engine**: Kalkulasi ketersediaan deep yang menghitung persentase uptime (1 jam - 90 hari), rekonsiliasi hybrid Prometheus TSDB dan SQLite hourly buckets, SLA error budget, sparkline latensi, dan single-flight request coalescing.
- **Role-Based Access Control (RBAC)**: Pemisahan hak akses berjenjang antara Owner (akun pendiri yang diproteksi permanen), Administrator, dan Read-Only Viewer.
- **Failover Prometheus Endpoint & Per-Endpoint Job Filter**: Dukungan multiple endpoint Prometheus dengan auto-failover, sinkronisasi antar klien, dan preferensi Default Job tersimpan per endpoint.
- **Synthetic Target Poller & Availability Aggregator**: Background worker mandiri yang mendeteksi transisi status probe, SlowResponse debouncing, dan agregasi bucket availability berkala dengan sistem distributed lease.
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
  - Key otomatis dibuat di `alarm/data/.api_key` dan `alarm/data/.webhook_secret`.
  - Lihat key: `cat alarm/data/.api_key`
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
| `/api/targets` | `GET` / `POST` 🔒 / `DELETE` 🔒 | Kelola daftar target yang dimonitor (tombstone di SQLite) |
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

## Manajemen Target & Prometheus Auto-Discovery

Seluruh target monitoring di InfraWatch v3 dideteksi secara dinamis melalui **Auto-Discovery langsung dari Prometheus** (`/api/v1/targets`). Anda cukup mengonfigurasi scrape job dan target di Prometheus / Blackbox Exporter eksternal Anda.

- **Auto-Discovery**: Poller & dashboard membaca target aktif beserta metrik `probe_success` / `probe_duration_seconds` dari `PROMETHEUS_URL`. Tidak ada file YAML konfigurasi lokal yang perlu dikelola secara manual.
- **Hapus Target (Tombstone Reversibel)**: Operator dapat menyembunyikan target tertentu dari monitoring wallboard via tombol hapus di detail host (`DELETE /api/targets`). Target yang dihapus disimpan sebagai tombstone di SQLite (`deleted_targets`), bukan menghentikan scraping di Prometheus.
- **Pulihkan Target**: Target yang disembunyikan dapat dipulihkan kembali melalui dropdown **Add Target** di dashboard (`POST /api/targets`).

---

## Arsitektur & Desain Modular

InfraWatch v3 dibangun menggunakan prinsip arsitektur modular yang terisolasi dengan seam yang jelas antar subsistem:

- **`alarm/core/availability` (Availability Engine)**:
  - Mengelola kalkulasi ketersediaan armada, rekonsiliasi hybrid Prometheus TSDB dan pre-aggregated hourly bucket SQLite.
  - Menghitung proyeksi SLA error budget dan tren armada rolling.
  - Memiliki single-flight request coalescing dan TTL caching untuk mencegah lonjakan beban backend.

- **`alarm/core/workers` (Background Workers)**:
  - `TargetPoller`: Poller berbasis event Prometheus dengan debounce SlowResponse, auto-resume pasca-maintenance, dan rekonsiliasi alert orphaned.
  - `AvailabilityAggregator`: Worker periodik yang mematerialisasi bucket ketersediaan 1-jam ke SQLite dengan mekanisme lease election (`AggregationLeaseRepository`).

- **`alarm/core/monitoring` (Fleet State Engine)**:
  - Pipeline 3 tahap: ingestion probe Prometheus, pengayaan target (maintenance, relasi dependensi, incident SQLite, operator ACK), dan reduksi status armada kanonikal.

- **`alarm/storage` (Storage Subsystem)**:
  - Koneksi SQLite berperforma tinggi dengan mode WAL (`PRAGMA synchronous = NORMAL`, `busy_timeout = 30000`) dan isolasi transaksi `BEGIN IMMEDIATE`.
  - 4 domain repository terfokus: `alerts` (Incident, EventLog, Acknowledgment), `availability` (Buckets, Leases, SLA Targets, Slow Thresholds), `inventory` (Maintenance, Dependencies, Endpoints, Deleted Targets), dan `auth` (Users, Audit Logs).
  - Migrasi skema database otomatis dan backward-compatible facade.

- **`alarm/web` (Web & Security Layer)**:
  - Proteksi SSRF untuk URL endpoint Prometheus eksternal.
  - Sliding window rate limiting dan middleware keamanan request.

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
