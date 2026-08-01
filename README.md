# Infrastructure Monitoring Stack v3 (NOC TV Display Ready)

Sistem monitoring ketersediaan website, server, dan jaringan real-time berbasis Docker yang berfokus pada **Blackbox Probe (Status UP/DOWN, Response Time, & HTTP Code)** untuk kemudahan visualisasi pada **TV Monitoring NOC (Network Operation Center)**.

---

<img width="1917" height="902" alt="image" src="https://github.com/user-attachments/assets/5964b70d-3f60-426e-b583-1b4514878889" />
<img width="1917" height="896" alt="image" src="https://github.com/user-attachments/assets/8ac6f617-a9cf-4b19-8d34-b248685a4c5f" />
<img width="1917" height="892" alt="image" src="https://github.com/user-attachments/assets/eb3f91b8-c381-461e-83f4-39d78e533a9e" />
<img width="1917" height="897" alt="image" src="https://github.com/user-attachments/assets/266e6b4c-dcc2-4ef8-84b5-8f394ed8fcb6" />
<img width="1917" height="900" alt="image" src="https://github.com/user-attachments/assets/845c7df4-3088-4d96-b0bf-db35596b034d" />

---

## 🎯 Perbedaan Utama: Transisi v2 ke v3

Mengapa versi 3 menyederhanakan arsitektur dari versi sebelumnya?

| Aspek | Versi 2 (v2) | Versi 3 (v3) - Current |
| --- | --- | --- |
| **Fokus Monitoring** | Dual-Monitoring (Node Exporter internal hardware + Blackbox Probe) | **Focused Blackbox Availability (Status UP/DOWN, Ping, HTTP Code, Latency)** |
| **Penyebab Perubahan** | Menampilkan metrik internal (CPU/RAM/Disk) membuat dashboard rumit & berat untuk pemantauan cepat | Dioptimalkan agar **lebih simpel, cepat, dan sangat aksesibel** untuk ditayangkan di **TV Monitor NOC** |
| **Penggunaan Ideal** | Analisis detail metrik internal server oleh Admin SysAdmin | **Display TV Dashboard 24/7 di Ruang IT / NOC (Network Operation Center)** |
| **Tampilan Visual** | Banyak grafik metrik teknis | **Beacon visual status (Hijau/Merah), Sirine Audio, & Indikator Responsif** |
| **Manajemen Perawatan** | Alert tetap membunyikan sirine saat server diperbaiki | **Maintenance Windows Mode** (Sirine ditahan khusus target yang sedang diperbaiki) |
| **Manajemen Badai Alert** | Menampilkan seluruh alert turunan saat gateway mati | **Alert Correlation & Dependency Tree** (Menekan alert turunan saat parent node down) |

---

## 💡 Mengapa v3 Berfokus pada Blackbox Rule UP / DOWN?

1. **Aksesibel & Optimal untuk TV Display NOC**:
   Tampilan dashboard dirancang agar dapat dibaca dengan jelas dari jarak jauh pada layar TV wall / monitor ruangan tanpa *clutter* grafik CPU/RAM yang membingungkan tim operasional non-sysadmin.
2. **Deteksi Insiden Tercepat (Live Probe)**:
   Fokus pada kondisi nyata layanan dari perspektif pengguna (*Is the service accessible?*). Jika website/ping down, sistem langsung menyalakan sirine dan beacon merah dalam kurun waktu 5–10 detik.
3. **Ringan & Hemat Resource**:
   Dengan mengeliminasi akumulasi metrik internal Node Exporter yang kompleks, `v3` berjalan sangat efisien dan responsif bahkan saat memantau ratusan hingga ribuan target layanan sekaligus.

---

## 🛠️ Alur Kerja Sistem v3

```text
Server / Network / Website Targets
       ↓
Blackbox Exporter Probe (HTTP/HTTPS, ICMP Ping, DNS)
       ↓
Prometheus Engine (Evaluasi Rule Status UP/DOWN 5s)
       ↓
Alertmanager (Webhook Dispatcher & Inhibition)
       ↓
InfraWatch Console v3 (Web Dashboard - TV Display Ready)
├── Live Visual Beacon (Hijau = Normal, Merah = Down)
├── Auto Audio Siren / Alarm MP3
├── Maintenance Window Toggle (Mute Alarm Per-Target)
├── Dependency Correlation Tree (Suppress Cascade Alerts)
└── Advanced SLA & Fleet Availability Calculator
```

---

# Fitur Utama (v3 TV-Display Enterprise Edition)

- **🖥️ TV Monitoring Display Ready**: Interface ultra-clean khusus untuk ditayangkan 24/7 di TV Wall ruang IT / NOC.
- **⚡ Ultra-Responsive Probe Evaluation**: Deteksi perubahan status UP/DOWN dalam **5–10 detik**.
- **🔊 Auto Visual & Audio Siren Alarm**: Mengaktifkan sirine suara MP3 otomatis saat ada target yang mengalami down.
- **🛠️ Maintenance Window Mode**: Fitur per-target untuk menahan sirine audio & menyembunyikan status DOWN saat jadwal perawatan rutin.
- **🌳 Alert Correlation & Dependency Mapping**: Otomatis menekan alert turunan (*Child*) apabila node utama (*Parent/Gateway*) sedang down.
- **📈 Advanced SLA & Fleet Availability Engine**: Mengkalkulasi 4 jenis metrik ketersediaan:
  - `per_server`: Percent Uptime masing-masing target.
  - `fleet_average`: Rata-rata Uptime unweighted.
  - `fleet_aggregate`: Weighted SLA availability berdasarkan total waktu monitoring.
  - `health_ratio`: Persentase target yang 100% bebas dari insiden (zero downtime).
- **🌐 Dynamic Target Management**: Tambah dan hapus target website/server secara langsung dari UI Web Console (`http://localhost:5000`).

---

# Tech Stack

| Komponen | Versi | Deskripsi |
| --- | --- | --- |
| Docker Compose | v2.x | Orchestration container |
| InfraWatch Console | v3.0 | TV Display Dashboard, SLA Engine, Maintenance & Correlation Manager (Python/Flask) |
| Prometheus | v2.54.1 | Metrics collection & rule evaluation engine (5s evaluation) |
| Alertmanager | v0.27.0 | Routing & webhook notification engine |
| Blackbox Exporter | v0.25.0 | Dynamic HTTP/HTTPS & ICMP availability probe |

---

# Persyaratan System

Sistem operasi yang didukung (Ubuntu Server / Debian / Linux):

- **Ubuntu Server 22.04 LTS / 24.04 LTS** (Sangat Direkomendasikan)
- Debian 12+
- WSL2 (Ubuntu)
- Linux Server Apapun (Docker Ready)

Spesifikasi Perangkat Minimal:
- CPU: 1-2 Core
- RAM: 1-2 GB
- Disk: 5 GB SSD
- Software: Git, Docker Engine, Docker Compose v2

---

# Cara Menjalankan di Ubuntu Server

### 1. Install Docker (Jika Belum Terpasang)

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

---

### 2. Clone Repository & Jalankan Container

```bash
git clone https://github.com/malvin1205/infra-monitoring-stack-v3.git
cd infra-monitoring-stack-v3
docker compose up -d
```

---

### 3. Akses Layanan

- **InfraWatch TV Web Console**: `http://<IP-SERVER-UBUNTU>:5000`
- **Prometheus Metric Endpoint**: `http://<IP-SERVER-UBUNTU>:9090`

---

# Operasional & Penggunaan TV Display NOC

### 1. Menampilkan di TV Monitoring NOC
1. Buka browser di TV / Smart TV / PC Display NOC.
2. Akses `http://<IP-SERVER-UBUNTU>:5000`.
3. Klik tombol **Enable Audio / Unmute** pada browser agar alarm sirine suara dapat berbunyi otomatis saat ada insiden.

### 2. Memasang Maintenance Window Saat Perawatan
Saat server/website akan di-restart atau diperbaiki:
1. Buka InfraWatch Console (`http://<IP-SERVER-UBUNTU>:5000`).
2. Aktifkan **Maintenance** pada target yang bersangkutan.
3. Sirine suara tidak akan membingungkan tim di ruang NOC selama proses perbaikan berlangsung.

---

# Changelog v3

### v3.0 Major Focus Shift Release
- 🎯 **Focused Blackbox Architecture**: Penghapusan ketergantungan pada Node Exporter agar dashboard berfokus 100% pada ketersediaan layanan (UP/DOWN) & latensi respons, dioptimalkan khusus untuk ditayangkan di **TV Monitoring NOC**.
- 🛠️ **Maintenance Mode**: Penangguhan alarm suara per-target saat perawatan berkala.
- 🌳 **Dependency Correlation Engine**: Penekanan alert turunan saat node upstream/gateway mengalami insiden.
- 📊 **Comprehensive SLA Analytics**: Perhitungan otomatis 4 variabel availability (`per_server`, `fleet_average`, `fleet_aggregate`, `health_ratio`).
- ⚡ **Multi-Prometheus Failover & GZIP Payload**: Auto-failover endpoint Prometheus dan kompresi data untuk performa visual tanpa lag.

---

# Troubleshooting

## Suara alarm tidak berbunyi di browser TV
Kebanyakan browser modern memblokir autoplay audio secara default. Cukup klik sekali di area dashboard atau klik tombol **Enable Sound** pada UI InfraWatch Console.

## Mengubah URL Prometheus Sumber
Jika Prometheus berjalan terpisah di jaringan Anda, ubah environment variable pada `docker-compose.yml`:

```yaml
environment:
  - PROMETHEUS_URL=http://<IP-PROMETHEUS-ANDA>:9090
```

Kemudian restart container:

```bash
docker compose restart
```
