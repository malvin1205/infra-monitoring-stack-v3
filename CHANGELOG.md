# Changelog

Semua perubahan penting pada proyek **InfraWatch - Infrastructure Monitoring Stack v3** akan dicatat di dalam dokumen ini.

Format changelog ini mengacu pada standar [Keep a Changelog](https://keepachangelog.com/id/1.0.0/) dan mematuhi prinsip [Semantic Versioning](https://semver.org/).

---

## [3.5.0] - 2026-08-24

### 🛡️ Hardening Keamanan Lanjutan (Security Hardening)
- **API Key Tidak Lagi Terekspos ke Publik**:
  - `GET /` tidak lagi merender API key ke `<meta>` tag HTML — sebelumnya setiap perangkat di LAN yang membuka wallboard otomatis mendapat kredensial mutasi penuh (`view-source`), membuat lapisan `require_api_key` tidak efektif.
  - Operator kini diminta memasukkan API key sekali via prompt browser pada aksi mutasi pertama (`alarm.js`: `apiFetch()`); key disimpan di `localStorage` browser tersebut saja, dan dibersihkan otomatis kalau server menolaknya (401) sehingga request berikutnya meminta ulang.
- **Perbaikan SSRF DNS Rebinding**:
  - Endpoint Prometheus yang didaftarkan lewat `/api/endpoints` sebelumnya hanya divalidasi sekali saat registrasi — hostname yang di-rebind ke IP loopback/link-local/metadata setelahnya tetap dipercaya selamanya oleh poller & aggregator background.
  - `_filter_safe_candidates()` baru me-revalidasi IP hasil resolve tiap kali endpoint akan di-poll (TTL-cache 20 detik), dijalankan di executor DNS terpisah (`_DNS_CHECK_EXECUTOR`, timeout 1 detik) agar resolusi lambat tidak menyumbat worker pool query utama.
- **Rate Limiting**: limiter in-process ringan (fixed-window, tanpa Redis) — 20 request/60s untuk endpoint mutasi, 120 request/60s untuk endpoint query mahal (`/instances`, `/api/availability`, `/api/target-history`); mengembalikan `429` saat terlampaui.
- **Webhook Secret Header-Only**: fallback `?secret=` di query string dihapus dari `require_webhook_secret` (rawan bocor lewat access log reverse-proxy/Referer) — hanya `X-Webhook-Secret` yang diterima.
- **Proteksi Konfigurasi Telegram**: `GET /api/telegram` kini butuh `X-API-Key` — sebelumnya bot token (masked) dan chat ID bisa dibaca siapa saja di LAN tanpa autentikasi.
- **Docker Hardening**: `docker-compose.yml` menambahkan `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, dan `read_only: true` + `tmpfs: [/tmp]` (bind mount `./alarm:/app` tetap writable untuk state aplikasi).

### 🗄️ SQLite sebagai Single Source of Truth (Parsial)
- Domain **endpoints, deleted-targets, maintenance windows, dan dependencies** kini sepenuhnya dibaca/ditulis lewat SQLite (`EndpointRepository`, `DeletedTargetRepository`, `MaintenanceRepository`, `DependencyRepository`) — sidecar JSON-nya (`endpoints.json`, `deleted_targets.json`, `maintenance.json`, `dependencies.json`) dan seluruh dual-write/merge-by-id yang menyertainya dihapus total.
- `status.json`, `history.json`, dan `logs.json` **belum** dimigrasikan — domain ini menyentuh jalur alert paling kritis (webhook, poller, `record_alert_event`) sehingga sengaja ditunda sebagai pekerjaan terpisah demi menjaga risiko regresi tetap rendah.

### ♻️ Deduplikasi & Pembersihan Kode
- `derive_bucket_inputs()` dan `estimate_instance_cadence()` (baru, di `fleet_availability.py`) menyatukan pipeline probe→hourly-bucket yang sebelumnya diimplementasikan dua kali secara verbatim di `api_availability` (materialize path) dan `_aggregate_availability_cycle` (background aggregator).
- `_derive_probe_readings()` (baru) menyatukan dua loop enrichment target (probe-discovered vs custom) di `build_canonical_monitoring_state`.
- `PROMETHEUS_CANDIDATES` (daftar tebakan 4 URL fallback: `host.docker.internal`/`localhost`/`127.0.0.1`) dihapus — `/api/endpoints` sudah menyediakan cara eksplisit mendaftarkan endpoint, jadi menebak topologi deployment tidak diperlukan lagi.
- `alarm/check_subjobs.py` dihapus (CLI standalone tanpa referensi di mana pun di repo).

### 📚 Dokumentasi
- `README.md`: memperjelas bahwa Prometheus, Blackbox Exporter, dan Alertmanager adalah dependency **eksternal** yang tidak dikelola `docker-compose.yml` repo ini — sebelumnya diagram arsitektur dan tabel Tech Stack menyiratkan ketiganya bagian dari stack yang sama, padahal `docker compose up -d` hanya menjalankan container `alarm`. Menambahkan bagian Prasyarat dan tabel endpoint API dengan penanda 🔒 untuk rute yang butuh API key.

### 🧪 Pengujian (Testing)
- `alarm/test_security_hardening.py` baru: cakupan regresi untuk kelima perbaikan keamanan di atas (API key tidak bocor, mutasi ditolak tanpa key, SSRF revalidation, rate limit 429, dll).
- 158 test lolos (9 subtest) — diverifikasi 4x run berturut-turut via `pytest alarm -q`.

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
