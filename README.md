# Infrastructure Monitoring Stack v3 (NOC TV Display Ready)

Sistem monitoring ketersediaan infrastruktur enterprise berbasis Docker yang memantau ketersediaan server, jaringan, dan website secara **real-time** dengan pengujian probe ultra-responsif (**v3.0**).

> **Penting — cakupan repo ini**: repo ini berisi **InfraWatch** — backend Flask + dashboard NOC wallboard yang mengonsumsi Prometheus. Prometheus, Blackbox Exporter, dan Alertmanager **tidak** dikelola oleh `docker-compose.yml` di repo ini; ketiganya adalah dependency eksternal yang harus sudah berjalan dan bisa dijangkau dari container InfraWatch sebelum `docker compose up -d` dijalankan. Lihat [Prasyarat](#prasyarat-sebelum-menjalankan-infrawatch) di bawah.

---

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/a79a7f51-4dbe-4b02-b68e-5b2a04212d24" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/0ce42ca3-36e2-4d00-b3fe-3a9d8e3da3e8" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/df0d3c5d-85a5-4493-b610-9a209139bf99" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/4865a4ab-f06b-4b3d-9272-ab53a354bcf5" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/af1111c1-0bfa-4685-ad3d-95fcf48016e1" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/ba7866d4-998c-4dc7-a69c-0d3cd6104ff3" />

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/02c94b1e-5232-4cd1-9aa1-46294870a28d" />





---

## Transisi v2 ke v3

### 1. Perubahan Fokus Monitoring (Node Exporter vs. Blackbox Probe)
* **Pada v2**: Sistem menggabungkan pemantauan metrik internal hardware host (*Node Exporter*: CPU, RAM, Swap, Disk Space, Disk I/O, Network Traffic) dan ketersediaan publik (*Blackbox Exporter*).
* **Pada v3**: Sistem secara spesifik dioptimalkan berfokus pada **Blackbox Probe Availability (Status UP/DOWN, Response Latency (ms), HTTP Status Code, dan ICMP Ping)**. 

### 2. Mengapa Metrik Internal Node Exporter Dihilangkan di v3?
1. **Aksesibel & Optimal untuk TV Display NOC**:
   Menampilkan grafik CPU, RAM, dan Disk internal pada dashboard utama menciptakan *visual clutter* yang membingungkan bagi tim operasional non-SysAdmin di ruang **Network Operation Center (NOC)**. `v3` memangkas gangguan visual tersebut agar status kesehatan jaringan dan layanan dapat dibaca dengan jelas dari jarak jauh pada layar TV Display Wallboard.
2. **Eliminasi Beban Resource Backend**:
   Monitoring ratusan metrik internal dari Node Exporter membutuhkan resource memori dan query Prometheus yang sangat besar. Dengan berfokus pada probe availability, `v3` mampu memantau ribuan target secara simultan dengan penggunaan CPU/RAM yang sangat ringan.
3. **Fokus pada End-User & Network Service Availability**:
   Pertanyaan paling mendasar bagi tim IT NOC saat terjadi insiden adalah: *"Apakah service atau IP ini bisa diakses sekarang?"*. Blackbox probe memberikan jawaban langsung dalam 5–10 detik.

---

## 🛠️ Arsitektur & Komponen Utama Sistem v3

```text
┌─ EKSTERNAL — dikelola & di-deploy TERPISAH, bukan oleh docker-compose.yml repo ini ─┐
│                                                                                      │
│  Target Infrastructure (HTTP/HTTPS, ICMP Ping, Ports)                               │
│         │                                                                           │
│         ▼                                                                           │
│  Blackbox Exporter Probe Engine                                                     │
│         │                                                                           │
│         ▼                                                                           │
│  Prometheus Time-Series Engine (Scrape: 2s, Query Window: 5s)                       │
│         │                                                                           │
│         ├──────────────────────────────────────────┐                               │
│         ▼                                          ▼                               │
│  Synthetic Alert Poller (15s, di dalam InfraWatch)  Alertmanager Webhook (opsional)  │
└─────────┼──────────────────────────────────────────┼───────────────────────────────┘
          │                                          │
          └────────────────────┬─────────────────────┘
                                ▼
┌─ INFRAWATCH — repo ini, di-deploy oleh docker-compose.yml ──────────────────────────┐
│                                                                                      │
│      InfraWatch Engine Backend (Python Flask - app.py)                              │
│      ├── Single-Flight Query Lock (_FETCH_LOCKS)                                    │
│      ├── Failover Prometheus Pool (endpoint terdaftar via /api/endpoints)            │
│      ├── Maintenance Window Suppression (/api/maintenance)                          │
│      ├── Alert Correlation Engine (/api/dependencies)                               │
│      ├── Fleet SLA & Availability Engine (fleet_availability.py)                    │
│      └── Thread-Safe State Retention (_WEBHOOK_LOCK)                                │
│                                ▼                                                     │
│      InfraWatch TV Wallboard Console (http://localhost:5000)                        │
│      ├── Audio Consent Splash Overlay & Autoplay Permission                         │
│      ├── High-Contrast Visual Beacon (Healthy / Critical)                           │
│      ├── Live Response Time (ms) & HTTP Status Code                                 │
│      ├── 1-Click Acknowledge Alarm & Audio Mute                                     │
│      └── CSV Incident Report Exporter                                               │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

`docker compose up -d` di repo ini **hanya** menjalankan container InfraWatch. Prometheus/Blackbox Exporter/Alertmanager harus sudah reachable dari container tersebut lewat `PROMETHEUS_URL` (lihat [Prasyarat](#prasyarat-sebelum-menjalankan-infrawatch)).

---

## 📑 Penjelasan Fitur Sistem v3 Secara Menyeluruh

### 1. 🖥️ NOC TV Display Wallboard & Splash Consent Screen
- **Audio Autoplay Permission Handler**: Karena browser modern melarang pemutaran audio otomatis tanpa gestur pengguna, `v3` dilengkapi *Splash Screen Overlay* interaktif. Cukup 1x klik saat pertama kali membukanya di TV NOC untuk mengaktifkan izin pemutaran suara alarm MP3 secara permanen.
- **High-Contrast Status Beacons**: Kartu status target menggunakan indikator visual berwarna kontras tinggi (Hijau = Online, Merah = Critical Down, Kuning = Warning/Maintenance) yang sangat jelas terlihat dari jarak jauh.
- **Real-time Live Probe Metrics**: Setiap kartu target menampilkan latensi *Response Time (ms)* aktual, *HTTP Status Code* (misal: 200, 404, 500), serta label *Job Category*.

### 2. 🚨 Audio Siren Alarm & Acknowledge Control
- **Automatic MP3 Siren**: Saat ada target yang mengalami status DOWN, sistem backend mengirim sinyal insiden dan memicu sirine suara MP3 secara otomatis.
- **1-Click Acknowledge Alarm**: Tersedia tombol *Acknowledge Alarm* pada bagian navigasi utama untuk membungkam sirine sementara saat tim NOC sedang melakukan koordinasi insiden.

### 3. 🛠️ Maintenance Windows Engine (`/api/maintenance`)
- **Penjadwalan Perawatan Rutin (Instance & Job Scope)**: Tim dapat menentukan durasi perawatan untuk target tertentu (misal: 1 jam untuk server database).
- **Auto-Suppression Alarm & Log**: Selama window maintenance aktif, target ditandai dengan badge khusus, sirine suara **tidak akan berbunyi**, dan insiden palsu tidak akan mengotori riwayat log.
- **Auto-Resume Monitoring**: Begitu periode maintenance berakhir, poller backend otomatis melanjutkan pemantauan secara presisi.

### 4. 🌳 Alert Correlation & Dependency Tree (`/api/dependencies`)
- **Hirarki Parent-Child**: Operator dapat menentukan hubungan ketergantungan antar node (misal: `Server-A` bergantung pada `Gateway-Router`).
- **Cascade Suppression**: Apabila `Gateway-Router` mengalami DOWN, alert pada `Server-A` otomatis ditandai sebagai `suppressedBy=Gateway-Router` dan di-demote pada tampilan wallboard untuk mencegah kepanikan (*alert storm*).

### 5. 📊 Enterprise SLA & Fleet Availability Engine (`fleet_availability.py`)
Sistem mengkalkulasi 4 jenis metrik ketersediaan secara matematis dari data Prometheus:
1. `per_server`: Persentase ketersediaan individual tiap target dalam rentang waktu (1 jam s/d 90 hari).
2. `fleet_average`: Rata-rata ketersediaan *unweighted* seluruh target.
3. `fleet_aggregate`: **Weighted SLA Availability** berbasis total menit terdeteksi (Metrik SLA resmi untuk laporan manajemen).
4. `health_ratio`: Persentase target yang 100% bebas dari gangguan (zero downtime).
- **Instability Analytics**: Menyajikan data total insiden, *Mean Outage Duration (menit)*, serta daftar *Top 5 Most Unstable Targets*.
- **Target History Sparkline (`/api/target-history`)**: Menampilkan grafik tren latensi dan blok durasi status ONLINE/OFFLINE target dari waktu ke waktu.

### 6. ⚡ Prometheus High-Availability Pool & Query Optimizer
- **Failover Prometheus Endpoint (`/api/endpoints`)**: Operator mendaftarkan satu atau lebih URL Prometheus secara eksplisit lewat API/UI ini. Jika endpoint aktif offline, backend otomatis melakukan failover ke endpoint terdaftar lain secara transparan; setiap kandidat (termasuk `PROMETHEUS_URL` default) divalidasi ulang tepat sebelum dipakai untuk menutup celah DNS rebinding.
- **Single-Flight Lock (`_FETCH_LOCKS`)**: Mencegah query Prometheus berulang saat banyak layar TV / pengguna mengakses dashboard bersamaan (*thundering herd protection*).
- **Backend Query Caching (`PROMETHEUS_CACHE`)**: Caching hasil query PromQL dengan mekanisme auto-pruning berkala (`_maybe_prune_cache`).

### 7. 🤖 Synthetic Alert Poller (Autonomy Layer)
- Berjalan di background thread (`start_alert_poller`), memantau `probe_success` setiap 15 detik.
- Apabila sistem digunakan tanpa Alertmanager, poller ini secara otomatis mensintesis event `TargetDown` dan pemulihannya (*resolved*) langsung ke `status.json`, `logs.json`, dan `history.json`.
- **Authoritative Webhook Backoff**: Jika webhooks eksternal dari Alertmanager masuk ke `/webhook`, poller otomatis *back-off* agar tidak terjadi ganda notifikasi.

### 8. 📱 Notifikasi Real-time Telegram Bot (`telegram_notifier.py`)
- **Asynchronous Alert Dispatcher**: Mengirimkan pemberitahuan instan ke grup/channel Telegram saat status target `FIRING` (Down) maupun `RESOLVED` (Recovered) tanpa menghambat poller backend.
- **Rich HTML Alert Cards**: Format pesan yang dilengkapi status visual (🚨/✅), nama instance, job, latency (*ms*), durasi downtime terhitung, serta timestamp lokal WIB (UTC+7).
- **Masked Token Security**: Perlindungan token bot melalui masking di UI/API dan isolasi file konfigurasi dari repository publik.
- **Dynamic Config & Uji Koneksi**: Konfigurasi dapat diperbarui secara dinamis via REST API `/api/telegram` dan diverifikasi dengan tombol tes koneksi.

### 9. 🛡️ System Self-Health Diagnostics (`/health`)
- Melakukan verifikasi kesehatan komponen internal InfraWatch:
  1. **Prometheus Engine Connection**: Status konektivitas dan keaktifan Prometheus.
  2. **Storage Filesystem**: Ketersediaan akses write pada database dan file state.
  3. **Monitoring API**: Responsivitas endpoint API.
  4. **Alarm Poller Service**: Heartbeat keaktifan background poller thread (`_LAST_POLLER_TICK`).
- Digunakan sebagai *Healthcheck Engine* bawaan pada Docker Compose.

---

## 🔌 Daftar Endpoint REST API Lengkap

| Route Endpoint | Method | Fungsi & Deskripsi |
| --- | --- | --- |
| `/` | `GET` | Menampilkan dashboard utama NOC InfraWatch Console (`alarm.html`) |
| `/instances` | `GET` | Mengambil data live target, health status, response time (ms), HTTP code, & maintenance |
| `/api/availability` | `GET` | Perhitungan matematis 4 metrik SLA Availability & analisis stabilitas target |
| `/api/target-history` | `GET` | Riwayat timeline status & titik latensi sparkline untuk target tertentu |
| `/api/targets` | `GET` / `POST` 🔒 `DELETE` 🔒 | CRUD manajemen target secara dinamis (sinkronisasi ke `targets/websites.yml`) |
| `/api/maintenance` | `GET` / `POST` 🔒 | Menampilkan dan membuat jadwal Maintenance Window baru |
| `/api/maintenance/<id>` | `DELETE` 🔒 | Menghapus jadwal Maintenance Window |
| `/api/dependencies` | `GET` / `POST` 🔒 | Menampilkan dan membuat hirarki ketergantungan Parent-Child |
| `/api/dependencies/<id>` | `DELETE` 🔒 | Menghapus hirarki ketergantungan |
| `/api/endpoints` | `GET` / `POST` 🔒 `DELETE` 🔒 | Manajemen & failover kandidat endpoint Prometheus |
| `/api/endpoints/select` | `POST` 🔒 | Memilih endpoint Prometheus aktif secara manual |
| `/api/telegram` | `GET` 🔒 / `POST` 🔒 | Membaca status (masked token) dan memperbarui konfigurasi notifikasi Telegram |
| `/api/telegram/test` | `POST` 🔒 | Mengirim pesan uji koneksi ke Bot & Chat ID Telegram |
| `/status` | `GET` | Mengambil data status global (`NORMAL`, `WARNING`, `CRITICAL`) & active alerts |
| `/logs` | `GET` | Mengambil log kejadian insiden terkini (limit hingga 200 log) |
| `/history` | `GET` | Mengambil riwayat insiden berdurasi lengkap |
| `/webhook` | `POST` | Receiver webhook resmi dari Alertmanager |
| `/health` | `GET` | Self-health diagnostics 4 komponen internal InfraWatch |

🔒 = butuh header `X-API-Key` (lihat Zero Manual Key Setup di atas). Rute lain bisa diakses tanpa key — dashboard viewer tidak pernah butuh kredensial.


---

# Tech Stack

| Komponen | Dikelola oleh | Deskripsi |
| --- | --- | --- |
| Docker Compose | — | Container Orchestration Engine |
| InfraWatch Console | **repo ini** (`docker-compose.yml`) | Dashboard NOC TV Display, SLA Engine, Maintenance & Correlation Manager (Python/Flask) |
| Prometheus | **eksternal** — deploy terpisah | Time-series metrics collection & rule evaluation engine; harus reachable dari container InfraWatch via `PROMETHEUS_URL` |
| Alertmanager | **eksternal** (opsional) — deploy terpisah | Routing & webhook notification engine; jika tidak ada, Synthetic Alert Poller InfraWatch mengambil alih |
| Blackbox Exporter | **eksternal** — deploy terpisah | Dynamic HTTP/HTTPS & ICMP Ping availability probe, di-scrape oleh Prometheus |

InfraWatch sendiri tidak menspesifikasikan versi Prometheus/Blackbox Exporter/Alertmanager tertentu — pakai versi apa pun yang sudah berjalan di infrastruktur Anda, selama Prometheus mengekspos `probe_success`/`probe_duration_seconds`/`probe_http_status_code` dari job Blackbox seperti biasa.

---

# Prasyarat Sebelum Menjalankan InfraWatch

Sebelum `docker compose up -d` di repo ini:

1. **Prometheus sudah berjalan dan reachable** dari container InfraWatch (baik sebagai container lain di Docker network yang sama, service terpisah, atau instance di host/jaringan lain).
2. **Blackbox Exporter dikonfigurasi sebagai scrape target Prometheus** (di luar repo ini), dengan job blackbox probe yang menghasilkan metrik `probe_success`, `probe_duration_seconds`, `probe_http_status_code`.
3. Set `PROMETHEUS_URL` (via `.env`, lihat `.env.example`) ke URL Prometheus tersebut — default `http://prometheus:9090` mengasumsikan sebuah service bernama `prometheus` ada di Docker network yang sama, yang **tidak** disediakan oleh `docker-compose.yml` repo ini.
4. (Opsional) Alertmanager, jika ada, arahkan webhook receiver-nya ke `http://<host-infrawatch>:5000/webhook` dengan header `X-Webhook-Secret`. Tanpa Alertmanager, InfraWatch tetap berfungsi penuh lewat Synthetic Alert Poller bawaannya.

Setelah container InfraWatch jalan, cek `GET /health` — field `prometheus.ok` memberi tahu langsung apakah Prometheus berhasil dijangkau atau belum, tanpa perlu menebak dari dashboard.

---

# Persyaratan System

Sistem operasi yang didukung (Linux Server):

- **Ubuntu Server 22.04 LTS / 24.04 LTS** (Sangat Direkomendasikan)
- Debian 12+
- WSL2 (Ubuntu)

Spesifikasi Hardware Minimal:
- CPU: 1-2 Core
- RAM: 1-2 GB
- Disk: 5 GB SSD
- Software: Git, Docker Engine, Docker Compose v2

---

# Cara Menjalankan di Ubuntu Server

### 1. Instalasi Docker & Docker Compose

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

---

### 2. Clone Repository & Jalankan Service

```bash
git clone https://github.com/malvin1205/infra-monitoring-stack-v3.git
cd infra-monitoring-stack-v3
docker compose up -d
```

> **Zero Manual Key Setup**:
> Pada startup pertama, InfraWatch secara otomatis men-generate kredensial 64-karakter hex yang aman (`secrets.token_hex(32)`) dan menyimpannya ke `alarm/.api_key` serta `alarm/.webhook_secret`. Anda **tidak perlu menginstal OpenSSL** atau membuat API key secara manual.
>
> - **Dashboard tetap read-only tanpa key**: Melihat wallboard (`/`, `/instances`, `/api/availability`, dst) tidak pernah butuh API key — key **tidak** dikirim ke browser secara otomatis, sehingga siapa pun yang bisa membuka wallboard di LAN tidak otomatis mendapat kredensial mutasi.
> - **Operator diminta key saat pertama kali mutasi**: Begitu operator melakukan aksi yang mengubah state (tambah/hapus target, buat maintenance window, ganti endpoint Prometheus, ubah config Telegram), browser akan menampilkan prompt untuk memasukkan API key. Key tersebut lalu disimpan di `localStorage` browser itu saja — tidak dikirim ke viewer lain.
> - **Melihat Kredensial**: Jalankan `cat alarm/.api_key` atau `cat alarm/.webhook_secret`.
> - **Override Kustom (Opsional)**: Jika ingin menggunakan key khusus, salin `.env.example` ke `.env` dan tentukan `INFRAWATCH_API_KEY` atau `WEBHOOK_SECRET`.
> - **Rotasi Key**: Hapus file `alarm/.api_key` dan restart service untuk men-generate key baru — operator akan diminta memasukkan key baru pada mutasi berikutnya (browser lama akan mendapat 401 dan otomatis diminta ulang).

Verifikasi status container:

```bash
docker compose ps
```

---

### 3. Pengaturan Layanan di TV Display NOC

1. Buka browser pada TV / Display PC di ruang NOC.
2. Akses URL `http://<IP-SERVER-UBUNTU>:5000`.
3. Klik tombol **"Masuk & Aktifkan Audio Alarm"** pada layar Splash Screen agar browser mengizinkan sirine audio terputar otomatis saat insiden terjadi.

---

# Pengujian & Simulasi Operasional

### 1. Pengujian Simulasi Maintenance Mode
1. Masuk ke Web Console `http://<IP-SERVER-UBUNTU>:5000`.
2. Buka menu Maintenance dan tambahkan target `http://webapp`.
3. Matikan container target (`docker stop webapp`).
4. **Hasil**: Target ditandai sedang maintenance, badge visual menjadi kuning, dan sirine suara **tidak akan berbunyi**.

### 2. Pengujian Simulasi Website Down (Sirine Alarm)
1. Matikan container target tanpa status maintenance:
   ```bash
   docker stop webapp
   ```
2. Dalam kurun waktu 5–10 detik, indikator target berubah menjadi merah (`CRITICAL`), sirine suara MP3 berbunyi, dan log insiden tercatat secara real-time.
3. Hidupkan kembali container:
   ```bash
   docker start webapp
   ```
4. Status otomatis pulih (`NORMAL`) dan sirine berhenti.

---

# Maintenance & Troubleshooting

### Perintah Operasional Docker:
- **Lihat Log Container**: `docker compose logs -f`
- **Restart Container**: `docker compose restart`
- **Menghentikan Service**: `docker compose down`

### Troubleshooting Umum:

1. **Sirine Suara Tidak Berbunyi di Browser TV**:
   Browser memblokir *autoplay audio*. Pastikan Anda telah mengklik sekali pada layar atau mengklik tombol *Unmute/Audio Permission* di bagian atas dashboard.

2. **Mengubah Sumber IP Prometheus**:
   Jika Prometheus berjalan di lokasi lain, ubah environment variable pada `docker-compose.yml`:
   ```yaml
   environment:
     - PROMETHEUS_URL=http://192.168.x.x:9090
   ```
   Lalu restart container dengan `docker compose restart`.

---

## 👥 Tim & Kontributor

- **dimi** ([@malvin1205](https://github.com/malvin1205)) - Maintainer & Core Architect
- **Fachriyusuf** ([@Fachriyusuf](https://github.com/Fachriyusuf)) - Contributor: Telegram Alerting Engine, REST APIs, & Changelog v3.2.0

