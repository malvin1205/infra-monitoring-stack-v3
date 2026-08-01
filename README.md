# Infrastructure Monitoring Stack v3 (NOC TV Display Ready)

Sistem monitoring ketersediaan infrastruktur enterprise berbasis Docker yang memantau ketersediaan server, jaringan, dan website secara **real-time** dengan pengujian probe ultra-responsif (**v3.0**).

---

<img width="1917" height="902" alt="image" src="https://github.com/user-attachments/assets/5964b70d-3f60-426e-b583-1b4514878889" />
<img width="1917" height="896" alt="image" src="https://github.com/user-attachments/assets/8ac6f617-a9cf-4b19-8d34-b248685a4c5f" />
<img width="1917" height="892" alt="image" src="https://github.com/user-attachments/assets/eb3f91b8-c381-461e-83f4-39d78e533a9e" />
<img width="1917" height="897" alt="image" src="https://github.com/user-attachments/assets/266e6b4c-dcc2-4ef8-84b5-8f394ed8fcb6" />
<img width="1917" height="900" alt="image" src="https://github.com/user-attachments/assets/845c7df4-3088-4d96-b0bf-db35596b034d" />

---

## 🔬 Hasil Analisis Arsitektur & Transisi v2 ke v3

Berdasarkan analisis mendalam terhadap arsitektur sistem pada versi 3 (v3), berikut adalah filosofi perubahan arsitektur utama yang diterapkan:

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
Target Infrastructure (HTTP/HTTPS, ICMP Ping, Ports)
       │
       ▼
Blackbox Exporter Probe Engine (v0.25.0)
       │
       ▼
Prometheus Time-Series Engine (Scrape: 2s, Query Window: 5s)
       │
       ├──────────────────────────────────────────┐
       ▼                                          ▼
Synthetic Alert Poller (15s)          Alertmanager Webhook (Option)
(Prometheus Probe Fallback)           (External Receiver /webhook)
       │                                          │
       └────────────────────┬─────────────────────┘
                            ▼
      InfraWatch Engine Backend (Python Flask - app.py)
      ├── Single-Flight Query Lock (_FETCH_LOCKS)
      ├── Failover Prometheus Pool (PROMETHEUS_CANDIDATES)
      ├── Maintenance Window Suppression (/api/maintenance)
      ├── Alert Correlation Engine (/api/dependencies)
      ├── Fleet SLA & Availability Engine (fleet_availability.py)
      └── Thread-Safe State Retention (_WEBHOOK_LOCK)
                            │
                            ▼
      InfraWatch TV Wallboard Console (http://localhost:5000)
      ├── Audio Consent Splash Overlay & Autoplay Permission
      ├── High-Contrast Visual Beacon (Healthy / Critical)
      ├── Live Response Time (ms) & HTTP Status Code
      ├── 1-Click Acknowledge Alarm & Audio Mute
      └── CSV Incident Report Exporter
```

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
- **Failover Prometheus Endpoint (`/api/endpoints`)**: Backend menyimpan daftar kandidat URL Prometheus (`PROMETHEUS_CANDIDATES`). Jika endpoint utama offline, backend otomatis melakukan failover ke endpoint cadangan secara transparan.
- **Single-Flight Lock (`_FETCH_LOCKS`)**: Mencegah query Prometheus berulang saat banyak layar TV / pengguna mengakses dashboard bersamaan (*thundering herd protection*).
- **Backend Query Caching (`PROMETHEUS_CACHE`)**: Caching hasil query PromQL dengan mekanisme auto-pruning berkala (`_maybe_prune_cache`).

### 7. 🤖 Synthetic Alert Poller (Autonomy Layer)
- Berjalan di background thread (`start_alert_poller`), memantau `probe_success` setiap 15 detik.
- Apabila sistem digunakan tanpa Alertmanager, poller ini secara otomatis mensintesis event `TargetDown` dan pemulihannya (*resolved*) langsung ke `status.json`, `logs.json`, dan `history.json`.
- **Authoritative Webhook Backoff**: Jika webhooks eksternal dari Alertmanager masuk ke `/webhook`, poller otomatis *back-off* agar tidak terjadi ganda notifikasi.

### 8. 🛡️ System Self-Health Diagnostics (`/health`)
- Melakukan verifikasi kesehatan 4 komponen internal:
  1. **Prometheus Engine Connection**: Status konektivitas dan keaktifan Prometheus.
  2. **Storage Filesystem**: Ketersediaan akses write pada file `status.json`, `logs.json`, dan `history.json`.
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
| `/api/targets` | `GET / POST / DELETE` | CRUD manajemen target secara dinamis (sinkronisasi ke `targets/websites.yml`) |
| `/api/maintenance` | `GET / POST` | Menampilkan dan membuat jadwal Maintenance Window baru |
| `/api/maintenance/<id>` | `DELETE` | Menghapus jadwal Maintenance Window |
| `/api/dependencies` | `GET / POST` | Menampilkan dan membuat hirarki ketergantungan Parent-Child |
| `/api/dependencies/<id>` | `DELETE` | Menghapus hirarki ketergantungan |
| `/api/endpoints` | `GET / POST / DELETE` | Manajemen & failover kandidat endpoint Prometheus |
| `/api/endpoints/select` | `POST` | Memilih endpoint Prometheus aktif secara manual |
| `/status` | `GET` | Mengambil data status global (`NORMAL`, `WARNING`, `CRITICAL`) & active alerts |
| `/logs` | `GET` | Mengambil log kejadian insiden terkini (limit hingga 200 log) |
| `/history` | `GET` | Mengambil riwayat insiden berdurasi lengkap |
| `/webhook` | `POST` | Receiver webhook resmi dari Alertmanager |
| `/health` | `GET` | Self-health diagnostics 4 komponen internal InfraWatch |

---

# Tech Stack

| Komponen | Versi | Deskripsi |
| --- | --- | --- |
| Docker Compose | v2.x | Container Orchestration Engine |
| InfraWatch Console | v3.0 | Dashboard NOC TV Display, SLA Engine, Maintenance & Correlation Manager (Python/Flask) |
| Prometheus | v2.54.1 | Time-series metrics collection & rule evaluation engine (5s evaluation) |
| Alertmanager | v0.27.0 | Routing & webhook notification engine |
| Blackbox Exporter | v0.25.0 | Dynamic HTTP/HTTPS & ICMP Ping availability probe |

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
2. Buka menu Maintenance dan tambahkan target `http://nginx`.
3. Matikan container nginx (`docker stop nginx`).
4. **Hasil**: Target ditandai sedang maintenance, badge visual menjadi kuning, dan sirine suara **tidak akan berbunyi**.

### 2. Pengujian Simulasi Website Down (Sirine Alarm)
1. Matikan container nginx tanpa status maintenance:
   ```bash
   docker stop nginx
   ```
2. Dalam kurun waktu 5–10 detik, indikator target berubah menjadi merah (`CRITICAL`), sirine suara MP3 berbunyi, dan log insiden tercatat secara real-time.
3. Hidupkan kembali container:
   ```bash
   docker start nginx
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
