# InfraWatch — Web Status & Alarm Console

Aplikasi dashboard monitoring status website real-time dan alarm suara insiden yang terhubung langsung dengan Prometheus (`http://192.168.9.16:9090`).

---

## 🚀 Cara Menjalankan

### Opsi 1: Menggunakan Python (Tanpa Docker)
```bash
cd alarm
pip install -r requirements.txt
python app.py
```

### Opsi 2: Menggunakan Docker Compose
```bash
docker compose up -d
```

---

## 🌐 Akses Layanan

- **InfraWatch Web Console**: [http://localhost:5000](http://localhost:5000)
- **Sumber Data Prometheus**: `http://192.168.9.16:9090`

---

## 📑 Fitur Utama

- **Real-time Probe Monitoring**: Menampilkan health status (UP/DOWN), response time (latency), dan HTTP status code.
- **SLA Uptime Calculation**: Mengkalkulasi ketersediaan historis (availability %) berbasis data `probe_success` dari Prometheus.
- **Visual & Audio Alarm**: Membunyikan suara alarm mp3/siren secara otomatis di browser saat ada target yang mengalami down.
- **Dynamic Target Management**: Tambah/Hapus target di UI Web Console (`http://localhost:5000`).
