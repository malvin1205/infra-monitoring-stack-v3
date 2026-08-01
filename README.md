# Infrastructure Monitoring Stack v3

Sistem monitoring infrastruktur enterprise berbasis Docker yang memantau kondisi server, jaringan, dan website secara **real-time** dengan pengujian alert ultra-responsif (**v3.0**).

Melanjutkan kesuksesan versi v2, **Infrastructure Monitoring Stack v3** menghadirkan pembaruan besar pada tingkat keandalan enterprise, manajemen insiden yang cerdas melalui **Alert Correlation (Dependency Mapping)**, **Maintenance Windows Mode**, serta **Advanced Fleet Availability & SLA Engine**.

---

<img width="1917" height="902" alt="image" src="https://github.com/user-attachments/assets/5964b70d-3f60-426e-b583-1b4514878889" />
<img width="1917" height="896" alt="image" src="https://github.com/user-attachments/assets/8ac6f617-a9cf-4b19-8d34-b248685a4c5f" />
<img width="1917" height="892" alt="image" src="https://github.com/user-attachments/assets/eb3f91b8-c381-461e-83f4-39d78e533a9e" />
<img width="1917" height="897" alt="image" src="https://github.com/user-attachments/assets/266e6b4c-dcc2-4ef8-84b5-8f394ed8fcb6" />
<img width="1917" height="900" alt="image" src="https://github.com/user-attachments/assets/845c7df4-3088-4d96-b0bf-db35596b034d" />

---

## 🔄 Transisi & Pembaruan Utama (v2 ke v3)

Pembaruan dari **v2** ke **v3** fokus pada efisiensi operasional dan eliminasi false positive saat insiden besar atau perawatan rutin:

1. **🛠️ Maintenance Windows Engine**:
   - **v2**: Saat server sedang diperbaiki, alarm dan alert tetap membunyikan sirine dan memenuhi log insiden.
   - **v3**: Fitur penentuan jadwal maintenance per-target. Saat target berada dalam window maintenance, status ditandai secara visual di console, dan audio alarm ditahan otomatis tanpa mengganggu monitoring target lain.

2. **🌳 Alert Correlation & Dependency Mapping (Parent-Child Tree)**:
   - **v2**: Jika perangkat jaringan utama (misal: Gateway/Switch) mati, seluruh server di bawahnya akan menembakkan puluhan alert secara simultan (*alert storm*).
   - **v3**: Dukungan hirarki dependensi. Jika host *Parent* mengalami down, alert pada host *Child* otomatis di-suppress dan ditandai sebagai dampak korrelasi (*dependency cascade*).

3. **📊 Enterprise Fleet Availability & SLA Engine**:
   - **v2**: Kalkulasi ketersediaan sederhana berbasis rata-rata persentase.
   - **v3**: Kalkulasi 4 metrik availability terpisah:
     - `per_server`: Availability individual tiap server/website.
     - `fleet_average`: Rata-rata unweighted seluruh armada.
     - `fleet_aggregate`: Weighted SLA availability berdasarkan durasi total monitoring.
     - `health_ratio`: Persentase server yang 100% pernah bebas dari insiden (zero downtime).

4. **⚡ Multi-Endpoint Failover & High Performance Payload**:
   - **v2**: Terikat pada satu URL Prometheus statis.
   - **v3**: Penggunaan kandidat endpoint Prometheus otomatis (failover/failback), proteksi *thread-lock* webhook (`_WEBHOOK_LOCK`), kompresi GZIP untuk transmisi ribuan target, serta busting cache aset statis otomatis (`?v=<mtime>`).

---

## 🛠️ Alur Kerja Sistem

```text
Server & Website Target (Host / HTTP / ICMP)
       ↓
Pengumpul Data (Node Exporter + Blackbox Exporter)
       ↓
Prometheus Engine (Scrape 2s / Rule Evaluation 5s)
       ↓
Alertmanager (Routing, Grouping, & Global Inhibition)
       ↓
InfraWatch Console v3 (Web Dashboard) ↔ Grafana
(Maintenance Control, Dependency Suppression, SLA Engine, Audio Siren)
```

---

# Fitur Utama (v3 Enterprise Edition)

- **⚡ Ultra-Responsive Alerting**: Evaluasi rule interval 5 detik. Deteksi gangguan terjadi dalam **10–30 detik**.
- **🚨 Multi-Tier Severity (Warning vs Critical)**:
  - **CPU**: `CPUUsageWarning` (>80%), `HighCPU` (>95%), `CPUSaturationLoadHigh`.
  - **RAM**: `MemoryUsageWarning` (>80%), `HighMemoryUsage` (>92%), `SwapUsageHigh` (>80%).
  - **Disk**: `DiskSpaceWarning` (<15%), `DiskSpaceLow` (<5%), `DiskFillPredictive24h` (prediktif disk habis 24j).
  - **Network & Target**: `NodeExporterDown` (15s), `PrometheusTargetDown` (15s), `NetworkErrors`.
- **🛠️ Maintenance Mode**: Menangguhkan audio alarm dan log insiden untuk host yang sedang dalam jadwal perbaikan.
- **🌳 Dependency Tree correlation**: Menekan notifikasi beruntun (*alert fatigue*) saat node upstream mati.
- **📈 Comprehensive SLA & Fleet Analytics**: Menyajikan data ketersediaan aktual untuk laporan manajemen dan SLA audit.
- **🖥️ InfraWatch Web Console v3 (`http://localhost:5000`)**: Dashboard interaktif berbasis Flask untuk monitoring live status, target CRUD, maintenance window management, dependency setup, dan ekspor CSV/JSON.

---

# Daftar Isi

- [Tech Stack](#tech-stack)
- [Persyaratan System](#persyaratan-system)
- [Instalasi Docker di Ubuntu Server](#instalasi-docker-di-ubuntu-server)
- [Clone Repository](#clone-repository)
- [Menjalankan Project](#menjalankan-project)
- [Akses Layanan](#akses-layanan)
- [Login Default](#login-default)
- [Import Dashboard Grafana](#import-dashboard-grafana)
- [Pengujian Monitoring](#pengujian-monitoring)
- [Maintenance & Operasional](#maintenance--operasional)
- [Changelog v3](#changelog-v3)
- [Troubleshooting](#troubleshooting)

---

# Tech Stack

| Komponen | Versi | Deskripsi |
| --- | --- | --- |
| Docker Compose | v2.x | Orchestration container |
| InfraWatch Console | v3.0 | Dashboard monitoring, SLA engine, maintenance & correlation receiver (Python/Flask) |
| Prometheus | v2.54.1 | Time-series metrics collection & rule engine (5s evaluation) |
| Alertmanager | v0.27.0 | Routing, grouping & alert inhibition engine |
| Grafana | v11.1.0 | Dashboard visualisasi grafik infrastruktur |
| Node Exporter | v1.8.2 | Host hardware metric collector |
| Blackbox Exporter | v0.25.0 | Dynamic HTTP/HTTPS & ICMP availability probe |
| Nginx | Stable | Web server contoh yang dimonitor |

---

# Persyaratan System

Sistem operasi yang didukung (Linux Server / Local Environment):

- **Ubuntu Server 22.04 LTS / 24.04 LTS** (Direkomendasikan)
- Debian 12+
- WSL2 (Ubuntu)
- RHEL / AlmaLinux / Rocky Linux 9+

Spesifikasi Perangkat Minimal:

- CPU: 2 Core
- RAM: 2 GB (Rekomendasi 4 GB untuk data historis panjang)
- Disk: 10 GB SSD
- Software: Git, Docker Engine, Docker Compose v2, Koneksi Internet

---

# Instalasi Docker di Ubuntu Server

### 1. Update Package Index

```bash
sudo apt update && sudo apt upgrade -y
```

---

### 2. Install Docker Engine

Gunakan script instalasi resmi dari Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

---

### 3. Konfigurasi User Permission

Agar perintah Docker dapat dijalankan tanpa `sudo`:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

Verifikasi instalasi Docker:

```bash
docker --version
docker compose version
```

---

# Clone Repository

Clone repository versi v3 ke Ubuntu Server Anda:

```bash
git clone https://github.com/malvin1205/infra-monitoring-stack-v3.git
```

Masuk ke direktori project:

```bash
cd infra-monitoring-stack-v3
```

---

# Menjalankan Project

Jalankan seluruh stack service secara *detached* (`-d`):

```bash
docker compose up -d
```

Periksa status container:

```bash
docker compose ps
```

Status normal akan menampilkan container berjalan (`Up`):

- `alarm` (InfraWatch Console v3)
- `grafana` (opsional jika dikombinasikan dengan stack monitoring eksternal/internal)

> **Catatan:** Container `alarm` dapat dikonfigurasikan untuk terhubung ke instance Prometheus yang sudah berjalan di server infrastruktur Anda melalui environment variable `PROMETHEUS_URL`.

---

# Akses Layanan

| Layanan | URL / Port Default | Deskripsi |
| --- | --- | --- |
| **InfraWatch Web Console v3** | `http://<IP-SERVER-UBUNTU>:5000` | Dashboard utama, SLA calculation, target CRUD, maintenance & alarm control |
| **Grafana** | `http://<IP-SERVER-UBUNTU>:3000` | Dashboard visualisasi grafik metrics & tren historis |
| **Prometheus** | `http://<IP-SERVER-UBUNTU>:9090` | Query Prometheus & status firing alert rules |
| **Alertmanager** | `http://<IP-SERVER-UBUNTU>:9093` | Routing & inhibition status |
| **Node Exporter** | `http://<IP-SERVER-UBUNTU>:9100/metrics` | Endpoint metrik hardware host |
| **Blackbox Exporter** | `http://<IP-SERVER-UBUNTU>:9115` | Endpoint HTTP/ICMP availability probe |

---

# Login Default

| Layanan | Username | Password |
| --- | --- | --- |
| **Grafana** | `admin` | `admin` |
| **InfraWatch Console** | *(Tidak ada autentikasi default / Tanpa Login)* |

> **Security Tip:** Untuk penggunaan di lingkungan produksi, disarankan memasang Reverse Proxy (Nginx/Traefik) dengan Basic Auth atau OAuth2 Proxy di depan port 5000 dan 3000.

---

# Import Dashboard Grafana

Akses Grafana (`http://<IP-SERVER-UBUNTU>:3000`), lalu navigasi ke:

```
Dashboards → Import
```

Masukkan ID Dashboard komunitas berikut:

| Nama Dashboard | ID | Kegunaan |
| --- | --- | --- |
| **Node Exporter Full** | `1860` | Monitoring detail CPU, RAM, Disk, & Network Host |
| **Blackbox Exporter** | `7587` | Monitoring status uptime & latensi HTTP/Ping |

---

# Pengujian Monitoring

### 1. Pengujian Maintenance Mode (v3 Feature)
1. Buka InfraWatch Console di `http://<IP-SERVER-UBUNTU>:5000`.
2. Aktifkan **Maintenance Mode** pada salah satu target server/website.
3. Matikan target tersebut (misal: matikan service Nginx).
4. **Hasil**: Target ditandai sedang dalam perawatan, log insiden dicatat khusus, dan sirine audio **tidak berbunyi**.

---

### 2. Simulasi Website Down (Respon ~10 Detik)
Hentikan container web yang dimonitor:

```bash
docker stop nginx
```

Dalam kurun waktu ~10 detik, alert **WebsiteDown** akan aktif di Alertmanager dan InfraWatch Web Console akan menyalakan sirine audio serta indikator visual merah.

Untuk mengembalikan kondisi normal:

```bash
docker start nginx
```

---

### 3. Simulasi Load CPU Tinggi
Install `stress-ng` pada Ubuntu Server:

```bash
sudo apt install stress-ng -y
stress-ng --cpu 4 --timeout 60
```

Dalam 15–30 detik, alert **CPUUsageWarning** (>80%) atau **HighCPU** (>95%) akan terdeteksi di dashboard.

---

# Maintenance & Operasional

### Perintah Penting Docker Compose:

- **Cek Status Container**:
  ```bash
  docker compose ps
  ```
- **Melihat Log Real-time**:
  ```bash
  docker compose logs -f
  ```
- **Restart Seluruh Service**:
  ```bash
  docker compose restart
  ```
- **Menghentikan Service**:
  ```bash
  docker compose down
  ```

---

# Changelog v3

### v3.0 Major Enterprise Release
- 🛠️ **Maintenance Window Management**: Dukungan penangguhan alarm & penandaan visual jadwal perawatan rutin per-target.
- 🌳 **Alert Correlation Engine (Dependency Tree)**: Penekanan notifikasi berantai (*alert cascade suppression*) berbasis pemetaan Parent-Child host.
- 📊 **4-Metric Fleet Availability (SLA Engine)**: Menghitung ketersediaan `per_server`, `fleet_average`, `fleet_aggregate` (weighted SLA), dan `health_ratio`.
- ⚡ **Multi-Prometheus Failover**: Auto-detection & failover secara dinamis jika endpoint Prometheus utama tidak merespons.
- 🔒 **Webhook Race-Condition Protection**: Implementasi `_WEBHOOK_LOCK` untuk penanganan data webhook Alertmanager secara thread-safe.
- 🚀 **Performance Optimization**: Kompresi respon GZIP untuk fleet skala besar dan dynamic asset cache-busting (`?v=<mtime>`).

---

# Troubleshooting

## InfraWatch Console tidak dapat terhubung ke Prometheus
Pastikan environment variable `PROMETHEUS_URL` pada `docker-compose.yml` mengarah ke IP / hostname Prometheus yang benar. Jika Prometheus berjalan di host yang sama di luar Docker, gunakan IP interface (misal `http://172.17.0.1:9090` atau IP lokal server).

## File Rule Prometheus Permission Denied

```bash
sudo find . -type f \( -name "*.yml" -o -name "*.yaml" -o -name "*.conf" \) -exec chmod 644 {} \;
sudo find . -type d -exec chmod 755 {} \;
sudo chown -R $USER:$USER .
docker compose restart
```

## Docker Permission Denied pada Ubuntu Server

```bash
sudo usermod -aG docker $USER
newgrp docker
```
