# Changelog

Semua perubahan penting pada proyek **InfraWatch - Infrastructure Monitoring Stack v3** akan dicatat di dalam dokumen ini.

Format changelog ini mengacu pada standar [Keep a Changelog](https://keepachangelog.com/id/1.0.0/) dan mematuhi prinsip [Semantic Versioning](https://semver.org/).

---

## [3.4.0] - 2026-08-24

### 🚀 Provisioning Otomatis Kredensial (Automatic First-Run Provisioning)
- **Zero-Setup First Run**:
  - InfraWatch kini secara otomatis men-generate kredensial API key dan webhook secret 64-karakter hex yang aman menggunakan `secrets.token_hex(32)` pada startup pertama jika environment variable belum diset.
  - Menghilangkan ketergantungan pada OpenSSL host (`openssl rand -hex 32`) dan manual editing `.env`.
- **Persistensi & Hak Akses Aman**:
  - Kredensial yang di-generate disimpan ke file terisolasi (`alarm/.api_key` dan `alarm/.webhook_secret`) dengan izin akses `0600` (owner-only).
  - Kredensial bertahan melewati restart aplikasi, restart container, dan reboot mesin melalui mount volume `./alarm:/app`.
- **Presedensi Konfigurasi**:
  - Environment variable eksplisit (`INFRAWATCH_API_KEY`, `API_KEY`, `WEBHOOK_SECRET`) tetap memiliki prioritas tertinggi dan tidak akan pernah ditimpa secara diam-diam.
- **Docker Compose Zero-Config**:
  - `docker-compose.yml` disesuaikan agar `docker compose up -d` langsung dapat berjalan tanpa error variabel kosong pada fresh clone.
- **Pengujian Lengkap**:
  - Menambahkan modul `alarm/test_auth_provisioning.py` (10 test case) mencakup pengujian token generation, persistensi, env override, toleransi restart, proteksi 401/200, dan keamanan log.

---

## [3.3.0] - 2026-08-24

### 🛡️ Peningkatan Keamanan (Security)
- **Autentikasi API Key & Webhook Secret (`alarm/auth.py`)**:
  - Seluruh endpoint state-changing (`POST`/`DELETE` di `/api/endpoints`, `/api/targets`, `/api/maintenance`, `/api/dependencies`, `/api/telegram`) kini wajib mengirim header `X-API-Key` (atau `Authorization: Bearer <key>`), divalidasi terhadap `INFRAWATCH_API_KEY`.
  - `POST /webhook` kini wajib header `X-Webhook-Secret` (atau `?secret=`), divalidasi terhadap `WEBHOOK_SECRET`.
  - Fail-closed: jika `INFRAWATCH_API_KEY`/`WEBHOOK_SECRET` belum diset di environment, endpoint terkait menolak seluruh request dengan `401`/`500` alih-alih terbuka bebas.
  - Dashboard (`alarm.html`, `alarm.js`) menyisipkan API key secara otomatis lewat meta tag server-rendered, jadi UI tetap jalan tanpa perlu login manual.
- **Perbaikan Bypass SSRF (`is_safe_endpoint_url`, `select_endpoint_api`, `is_valid_target`)**:
  - `POST /api/endpoints/select` sebelumnya menerima URL Prometheus baru tanpa validasi anti-SSRF sama sekali — sekarang divalidasi sama seperti `POST /api/endpoints`.
  - `is_safe_endpoint_url` sekarang melakukan resolusi DNS (`socket.getaddrinfo`) dan memeriksa setiap IP hasil resolve, menutup celah bypass via hostname yang mengarah ke IP loopback/link-local/metadata.
  - `is_valid_target` menolak target ke alamat cloud metadata (`169.254.169.254`, `metadata.google.internal`, dll) secara eksplisit.
- **Git & Docker Hygiene**:
  - File data runtime (`history.json`, `status.json`, `endpoints.json`, `maintenance.json`, `dependencies.json`, `deleted_targets.json`, `history_archive.json`, `*.db`) di-untrack dari git dan ditambahkan ke `.gitignore` — sebelumnya ikut ter-commit dan berpotensi membocorkan topologi jaringan internal.
  - `alarm/.dockerignore` baru mencegah secret dan database ikut ter-copy ke Docker image layer.
  - `alarm/Dockerfile`: container kini berjalan sebagai non-root user (UID 10001) alih-alih root; paket `ffmpeg` yang tidak terpakai dicabut.
  - `alarm/requirements.txt`: `PyYAML` dipin ke versi exact (`==6.0.2`).

### 🔧 Diubah (Changed)
- `docker-compose.yml` menambahkan env var wajib `INFRAWATCH_API_KEY` dan `WEBHOOK_SECRET` (compose gagal start dengan pesan jelas jika belum diset).
- `.env.example` baru sebagai template konfigurasi.
- `README.md`: langkah instalasi memuat setup `.env` sebelum `docker compose up`.

### 🧪 Pengujian (Testing)
- `alarm/conftest.py` baru untuk menyuntik API key/webhook secret ke seluruh test suite sebelum modul `app` diimpor.
- 135 test case terverifikasi 100% lolos pasca perubahan.

### 👤 Kontributor (Contributor)
- **dimi** ([@dimimayoalvin1205](https://github.com/dimimayoalvin1205) - `dimimayoalvin1205@gmail.com`) — Remediasi temuan security audit: autentikasi API, perbaikan SSRF, hardening Docker, dan git hygiene.

---

## [3.2.0] - 2026-08-24

### 🚀 Ditambahkan (Added)
- **Integrasi Notifikasi Real-Time Telegram Bot (`telegram_notifier.py`)**:
  - Pengiriman notifikasi alert otomatis saat status target berubah menjadi **FIRING (Down)** maupun **RESOLVED (Recovered)**.
  - Dispatch asynchronous berbasis `ThreadPoolExecutor` non-blocking sehingga proses scraping dan poller backend tetap ultra-responsif tanpa latency tambahan.
  - Format pesan HTML yang elegan dan informatif:
    - Status badge visual (🚨 *CRITICAL / WARNING ALERT* dan ✅ *ALERT RESOLVED / RECOVERED*).
    - Informasi target instance, nama alert, summary, job kategori, dan latency probe (*ms*).
    - Perhitungan durasi downtime insiden secara otomatis pada alert pemulihan (misal: `2m 15s`, `1h 30m`).
    - Format waktu lokal presisi (default: WIB / UTC+7) dengan konfigurasi `ALERT_TZ_OFFSET_HOURS`.
  - Sanitasi pesan dengan HTML escaping untuk mencegah karakter khusus merusak format parsing Telegram Bot API.
- **REST API Konfigurasi Telegram**:
  - `GET /api/telegram`: Membaca status konfigurasi Telegram dengan masked token untuk keamanan.
  - `POST /api/telegram`: Memperbarui konfigurasi token bot, chat ID, dan opsi notifikasi secara dinamis tanpa perlu restart container.
  - `POST /api/telegram/test`: Endpoint pengujian konektivitas bot ke grup/chat Telegram tujuan.
- **Dukungan Konfigurasi via Environment Variables**:
  - `TELEGRAM_BOT_TOKEN`: Token bot Telegram dari BotFather.
  - `TELEGRAM_CHAT_ID`: ID grup, channel, atau private chat Telegram tujuan.
  - `TELEGRAM_ENABLED`: Switch aktif/nonaktif notifikasi Telegram (`true`/`false`).
  - Pembaruan file `docker-compose.yml` untuk menyertakan mapping environment Telegram.
- **Template Konfigurasi & Keamanan Secret**:
  - File template konfigurasi `alarm/telegram_config.json.example`.
  - Pembaruan `.gitignore` untuk melindungi file kredensial `alarm/telegram_config.json` agar tidak bocor ke repository publik.
- **Suite Pengujian Notifikasi Telegram**:
  - Skrip pengujian mandiri `alarm/test_telegram_alert.py` untuk memvalidasi uji koneksi, format pesan firing, dan format pesan resolved.

### 👤 Kontributor (Contributor)
- **Fachriyusuf** ([@Fachriyusuf](https://github.com/Fachriyusuf) - `fachriyusuf628@gmail.com`) — Pengembang integrasi notifikasi Telegram Bot, REST API Telegram, environment orchestration, dan dokumentasi changelog v3.2.0.

---

## [3.1.0] - 2026-08-24

### 🚀 Ditambahkan (Added)
- **Hybrid Availability Engine & SQLite Pre-Aggregation (`fleet_availability.py` & `storage.py`)**:
  - Lapisan penyimpanan persisten SQLite dengan mode WAL (*Write-Ahead Logging*) untuk performa query SLA berkecepatan tinggi.
  - Sistem bucket pre-aggregation 1 menit dan 5 menit untuk kalkulasi ketersediaan armada ribuan target dalam hitungan sub-detik.
  - Algoritma rekonstruksi interval deret waktu (*time-series interval reconstruction*) untuk mengatasi jitter jaringan dan scrape cadence yang bervariasi.
- **Per-Instance Scrape Interval Cadence**:
  - Penyesuaian interval scrape otomatis per-target instance guna mengoptimalkan beban probe Prometheus.
  - Pencegahan race condition pada request UI dashboard.

### 🛡️ Peningkatan Keamanan & Stabilitas (Security & Stability)
- Single-Flight Query Lock (`_FETCH_LOCKS`) pada backend Flask untuk mencegah *thundering herd problem* saat query Prometheus berat dieksekusi bersamaan.
- Thread-safe state retention dan file locking concurrency protection.
- Validasi input ketat dan canonical state sanitization pada seluruh API endpoints.
- Penambahan comprehensive unit & integration test suite (134 test cases terverifikasi 100% lolos).

---

## [3.0.0] - 2026-08-20

### 🚀 Ditambahkan (Added)
- **NOC TV Display Wallboard Console**:
  - Tampilan dashboard interaktif dengan kontras tinggi untuk kebutuhan layar Network Operation Center (NOC).
  - Splash screen consent overlay untuk otorisasi audio autoplay MP3 siren di browser modern.
  - Kartu metrik live response time (*ms*), status code HTTP, dan status probe ICMP Ping.
  - Tombol 1-Click Acknowledge Alarm & Mute Audio.
- **Maintenance Windows Engine (`/api/maintenance`)**:
  - Fitur penjadwalan perawatan server/website dengan auto-suppression sirine audio dan log insiden palsu.
- **Alert Correlation & Dependency Tree (`/api/dependencies`)**:
  - Pemetaan relasi parent-child antar node infrastruktur untuk root-cause analysis dan pencegahan *alert storm*.
- **Fleet SLA & Availability Metrics Breakdown**:
  - Kalkulasi ketersediaan uptime/downtime berbasis durasi observasi aktual.
  - Filter rentang waktu 24h, 7d, 30d, dan custom range.
  - Export laporan riwayat insiden dalam format CSV.

### 🔄 Diubah (Changed)
- Transisi dari pemantauan metrik internal host (Node Exporter v2) menjadi fokus penuh pada **Blackbox Exporter Probe Engine** untuk kecepatan deteksi insiden layanan publik/jaringan dalam hitungan 5–10 detik.
