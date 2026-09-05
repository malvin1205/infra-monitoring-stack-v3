# Changelog

Semua perubahan penting pada proyek **InfraWatch - Infrastructure Monitoring Stack v3** akan dicatat di dalam dokumen ini.

Format changelog ini mengacu pada standar [Keep a Changelog](https://keepachangelog.com/id/1.0.0/) dan mematuhi prinsip [Semantic Versioning](https://semver.org/).

---

## [3.6.6] - 2026-09-05

### Data Quality & Telemetry Audit — Wiring yang Hilang Dibangun

**Tujuan**
Follow-up dari temuan audit 3.6.5: tab "Data Quality & Telemetry Audit" + gear-icon
settings popover di modal Availability Breakdown punya markup+CSS lengkap tapi NOL
JavaScript. Bangun wiring-nya penuh.

**Hasil**
- Sub-nav Overview & Ranking <-> Data Quality & Telemetry Audit sekarang beneran
  switch pane, termasuk tombol "Back to Overview" dan link dari warning "Limited data"
  di card Fleet Availability (yang sebelumnya diperbaiki di 3.6.5 tapi tombolnya sendiri
  belum bisa diklik ke mana-mana).
- Gear-icon popover (pola persis di-reuse dari popover "Default Job" yang sudah ada:
  buka/tutup, klik-luar, Escape) — checkbox Node Exporter correlation baca/tulis lewat
  `GET`/`POST /api/settings/availability` yang sudah ada di backend, revert optimis
  kalau save gagal (mis. viewer tanpa permission `availability.write`).
- Seluruh pane Audit (kartu SLA Availability, Downtime Budget, status cakupan,
  Observed/Unmonitored/Planned-maintenance, tabel per-host dengan filter chip +
  search) dirender dari `this.availabilityBreakdown` — response `/api/availability`
  YANG SAMA yang sudah di-fetch buat pane Ranking (`telemetry_audit`, `sla`,
  `entries[].sla_budget` semua sudah ada di situ), jadi tidak ada request baru sama
  sekali.
- **Divalidasi terhadap server nyala + data Prometheus asli** (bukan cuma baca kode):
  jalanin `alarm/app.py` lokal, curl `/api/availability` sungguhan, cocokkan tiap
  field (`sla.window.*`, `telemetry_audit.*`, `entries[].sla_budget.*`) satu-satu
  sama yang dipakai di JS — semua match persis.
- Ketemu 1 bug pas nulis wiring ini sendiri sebelum sempat kepakai: `#auditSlaMaint`
  pakai atribut native `hidden` (bukan class), sempat mau ditoggle pakai
  `classList.toggle('hidden', ...)` yang cuma kerja kalau ada rule CSS `.hidden` buat
  elemen itu — dicek dulu ke stylesheet, dibetulin ke `el.hidden = bool` sebelum masuk.

**Kendala**
- Tidak ada browser Chrome ter-koneksi di sesi ini buat verifikasi visual
  klik-per-klik — verifikasi dilakukan via server nyala + curl + tracing manual
  field-per-field terhadap response API asli, bukan screenshot. Kalau ada quirk
  visual/CSS yang cuma kelihatan di browser sungguhan, tolong laporkan.

---

## [3.6.5] - 2026-09-05

### Full-Codebase Audit (`/code-review alarm/ max`) — 8 Bug Dibenerin

**Tujuan**
Audit menyeluruh seluruh `alarm/` (bukan cuma Incident History/Live Alert Log) buat
nyari conditional bug & race condition. Dijalankan via code-review agent effort max,
10 temuan — 8 di-fix + test, 1 di-tolak (regresi ganda dari fix sebelumnya sendiri),
1 dilaporkan tapi sengaja tidak diubah (butuh keputusan produk).

**Hasil**
1. **Fix ack-race sebelumnya (3.6.3) ternyata kebablasan**: `DELETE FROM
   alert_acknowledgments WHERE instance=?` jalan di SETIAP transisi (fire dan resolve),
   ga peduli host itu punya alert lain yang masih firing dan sudah di-ack. Host dengan
   TargetDown + alert lain (mis. dari Alertmanager) sekaligus — begitu alert LAIN itu
   resolve, ack buat TargetDown yang masih down ikut kehapus. Fix: DELETE cuma jalan di
   resolve, dan cuma kalau `NOT EXISTS` incident lain yang masih firing buat instance itu.
2. **Telegram `min_severity` — field sudah ada dari awal tapi TIDAK PERNAH ditegakkan
   di manapun** (dead config). Gate `severity != "warning"` yang saya taruh di
   `record_alert_event()` minggu lalu ikut kena dampak: dia global buat SEMUA pemanggil
   (webhook Alertmanager + poller), bukan cuma SlowResponse — alert `severity: warning`
   asli dari Alertmanager manapun bakal ikut ke-silent-block tanpa log. Fix: pindah ke
   `_async_send_worker` (telegram_notifier.py) yang sudah baca config di situ, betulan
   menegakkan `min_severity`, default diubah ke `"critical"` (`telegram_config.json`
   lokal ikut di-update) — sama-sama nahan SlowResponse seperti diminta, tapi lewat
   setting asli yang bisa diubah balik via `PUT /api/telegram`, bukan hardcode.
3. **SlowResponse stuck "Ongoing" selamanya setelah recover pas maintenance** — analisa
   saya sebelumnya salah: `_slow_poller_state.pop(inst, None)` bikin poller LUPA kalau
   DB masih nyatet firing, jadi debounce ga akan pernah nemu transisi firing→resolved
   (butuh state awal `firing=True` biar bisa turun). Fix: seed state dari DB (query
   active incidents), bukan pop kosong.
4. **`maintenance_windows_by_instance()` job-scope match `None == None`**: window
   dengan target kosong/rusak (record lama) match SEMUA instance yang ga ada di
   `job_map` — downtime asli ke-anggap "maintenance", ke-exclude diam-diam dari SLA.
   Fix: skip window kalau target kosong (samakan sama guard yang sudah ada di
   `get_active_maintenance`).
5. **`avail_rate` fallback ke 1.0 (100% up) kalau Prometheus balikin avail% yang ga
   bisa di-parse** — kontradiksi langsung sama komentar function sendiri ("jangan
   default ke fully-up kalau ga ada sinyal rate asli"). Bisa nutupin outage beneran.
   Fix: `avail_rate = None` (unattributed), bukan 1.0.
6. **`#metricFleetDataWarning` — `.textContent =` di JS nimpa SELURUH isi tombol**,
   padahal tombolnya sekarang punya icon span + text span + "View Telemetry Audit ➔"
   action span. Tiap refresh, icon dan tombol aksi ilang, jadi teks polos. Fix: cuma
   set `.textContent` span teksnya (`#metricFleetDataWarningText`).
7. **`entry.get("sla_target_pct") or sla_threshold`** — override SLA target 0% (nilai
   valid, `PUT /api/sla-targets` izinin 0-100) ke-anggap falsy, diam-diam diganti default
   fleet. Fix: cek `is not None`, bukan truthiness.
8. **`_annotate_logs_with_acknowledgment` nempelin ack ke SEMUA baris `firing` historis
   satu instance**, padahal `event_logs` itu append-only (baris `firing` lama dari
   outage yang SUDAH lama resolve tetap ber-`event='firing'` selamanya). Host yang
   pernah flapping 5x lalu down lagi — ack buat outage SEKARANG nempel juga ke 5
   outage lama yang udah beres. Fix: scan newest-first, cuma baris firing PERTAMA per
   instance (sebelum ketemu baris resolved yang lebih baru) yang di-anotasi.

**Ditolak**: fleet_aggregate sekarang pakai coverage yang exclude-maintenance
(`sla_total_uptime/sla_total_coverage`) alih-alih raw — ini BUKAN regresi tak sengaja:
`total_coverage`/`total_uptime` raw masih dihitung dan bisa direkonstruksi lewat
`maintenance_excluded_minutes` yang sudah diekspos, dan dokumentasi function sudah
nyebut `fleet_aggregate` sebagai "metrik SLA enterprise" — konsisten sama exclude
maintenance secara sengaja. Bukan bug, hanya flagged reviewer karena ga ada field
"raw fleet-wide" terpisah buat komparasi langsung.

**Dilaporkan, TIDAK diubah**: Data Quality & Telemetry Audit tab + gear-icon settings
popover (Node Exporter correlation toggle) di Availability Breakdown modal — backend-nya
(`get_availability_settings`/`save_availability_settings`, `/api/availability/settings`)
sudah lengkap, tapi NOL wiring JS (klik gear/tab/tombol ga ngapa-ngapain). Ini gap fitur
besar dari kerjaan sesi lain, bukan bug kecil — butuh konfirmasi user sebelum saya bangun
seluruh wiring-nya.

**Test baru**: `MaintenanceWindowsByInstanceTests` (3), assertion `total_down_seconds`
+ garbled-avail test di `test_hybrid_availability.py`, SLA-0%-override test, ack-leak
test (2 arah: alert lain ga ke-unack, occurrence baru ga mewarisi ack), SlowResponse
maintenance-recovery-via-debounce test, min_severity gate test (3, di
`test_telegram_alert.py`), log-annotate closed-episode test. Total 266 test + 45
subtests pass (dijalankan per-batch, hindari OOM lokal).

---

## [3.6.4] - 2026-09-05

### Node Exporter Infrastructure Correlation & NOC UI Refinement

**Tujuan**
Membedakan kegagalan probe level aplikasi/service vs infrastruktur host secara otomatis menggunakan metrik Node Exporter sebagai secondary signal, tanpa mengganggu kalkulasi SLA/availability eksisting. Sekaligus perapihan modal & form UI NOC Wallboard serta penguatan suite pengujian preaggregation.

**Hasil**
- `classify_probe_failure()` (`alarm/fleet_availability.py`): pure function tanpa I/O yang mengklasifikasikan probe yang DOWN menjadi `service_issue` (jika Node Exporter sehat) atau `infrastructure_issue` (jika Node Exporter down/unhealthy), dan `no_node_exporter` jika tidak ada telemetri node exporter. Tidak pernah mengubah ketersediaan atau SLA uptime/downtime.
- Pengaturan korelasi configurable (`alarm/availability_settings.json`, endpoint `/api/availability/settings` GET/POST dengan permission RBAC `availability.read` dan `availability.write`). Default: `false` (opt-in).
- Integrasi live target loop di `alarm/app.py`: jika fitur aktif, memetakan status `up{job="node_exporter"}` per target host untuk menyematkan correlation metadata pada target status.
- Template file `alarm/availability_settings.json.example` dan update `.gitignore` untuk melindungi config lokal.
- Refactor CSS (`alarm/static/css/noc.css`) dan modal/form HTML (`alarm/templates/alarm.html`) untuk konsistensi design tokens, perbaikan layout modal (User Management, Login, dll), dan standardisasi class `.form-input`, `.form-select`, `.modal-actions`.
- Suite pengujian baru `tests/test_availability_node_exporter.py` (21 unit & integration tests) dan optimasi mock `fetch_prom_range_map` di `tests/test_availability_preaggregation.py`.

---

## [3.6.3] - 2026-09-05

### "Acknowledged By" — Live Alert Log & Incident History

**Tujuan**
Tampilkan siapa yang acknowledge suatu alert dan kapan, di kedua tempat (Live Alert Log
dan Incident History). Sistem ack (`AcknowledgmentRepository`, `/api/acknowledge`) sudah
ada sebelumnya untuk live dashboard/alarm silencing, tapi datanya cuma live-state (hilang
begitu instance recover) dan tidak pernah disurface ke log/history.

**Hasil**
- Kolom baru `acknowledged_by`/`acknowledged_at` di tabel `incidents` — durable, tidak
  hilang seperti `alert_acknowledgments` (yang di-`clear_resolved()` begitu instance
  pulih). `AcknowledgmentRepository.acknowledge_instances()`/`unacknowledge_instance()`
  sekarang juga sinkron langsung ke row `incidents` yang lagi firing, jadi Incident
  History (Ongoing maupun sudah Resolved) tetap bisa nunjukin siapa yang ack.
- Occurrence baru (re-fire setelah resolve) otomatis reset ack ke kosong — outage baru
  butuh ack baru, tidak mewarisi ack dari occurrence sebelumnya.
- Live Alert Log: ack di-join live dari `alert_acknowledgments` (bukan kolom permanen di
  `event_logs`, karena ack itu properti "status outage SEKARANG", bukan properti satu
  baris log historis) — cuma baris `firing` yang masih relevan yang dapat badge ack.
- Frontend: baris "✓ Acked by {user} · {waktu}" muncul di bawah nama host, warna accent
  biar kebeda dari sub-text biasa (job/error) — tampil di History (chip) maupun Live
  Alert Log (chip) kalau ada datanya.
- Test baru `AcknowledgmentPersistenceTests` (5 test): ack persist ke row firing, ack
  bertahan setelah resolve + live record dihapus, unacknowledge bersihin row, occurrence
  baru mulai unacknowledged, dan `_annotate_logs_with_acknowledgment` cuma nandain baris
  firing yang cocok.

**Kendala**
- Tidak ada UI baru untuk melakukan acknowledge dari Live Alert Log/Incident History
  langsung — tombol ack yang sudah ada tetap di alarm/dashboard utama (per-instance).
  Kalau mau bisa ack langsung dari kedua panel ini, itu scope terpisah.

---

## [3.6.2] - 2026-09-05

### Audit Akurasi — Live Alert Log & Incident History

**Tujuan**
Permintaan eksplisit: pastikan tidak ada bug tersisa dan data 100% akurat di kedua fitur
ini. Audit menyeluruh kode backend (`storage.py`, `app.py`) dan frontend (`alarm.js`)
menemukan 4 bug nyata — semua sudah diperbaiki + diuji.

**Hasil (bug ditemukan & diperbaiki)**
1. **"Total Down" bukan aggregate, cuma occurrence terakhir**: task #3 minta kolom ini
   jadi "durasi agregat", tapi `duration_seconds` di-overwrite tiap re-fire (`started_at`
   ikut ter-reset), jadi host yang flapping 5x cuma nampilin durasi down yang PALING
   TERAKHIR, bukan total. Tambah kolom `total_down_seconds` yang di-akumulasi
   (`+= duration_seconds`) tiap kali resolve — dites: 5x occurrence @60s = 300s total,
   bukan 60s.
2. **Incident yang recover diam-diam saat maintenance stuck "Ongoing" selamanya**: poller
   sengaja skip evaluasi instance yang lagi maintenance, lalu paksa state ke 'up' pas
   window berakhir supaya "kalau masih down, transisi baru muncul" — tapi kalau
   ternyata SUDAH recover selama maintenance, `compute_state_transitions` melihat
   forced-'up' == actual-'up' dan TIDAK PERNAH emit resolve event. Row `incidents` di
   SQLite nyangkut status='firing' permanen — sebelumnya tersembunyi (History cuma
   nampilin resolved), sekarang jadi kelihatan sebagai "Ongoing" palsu karena task #2
   round sebelumnya. Fix: begitu window maintenance berakhir, cek state asli langsung,
   kalau sudah up maka resolve eksplisit (aman, no-op kalau memang tidak ada yang
   firing).
3. **`get_history()` bisa diam-diam buang incident Ongoing yang jarang update**: query
   lama `ORDER BY updated_at DESC LIMIT N` bisa nge-geser incident yang masih firing
   tapi sudah lama tidak re-trigger keluar dari window kalau ada banyak incident lain
   yang lebih baru resolve. Fix: fetch semua baris `firing` tanpa limit + baris
   `resolved` dengan limit terpisah, digabung — incident Ongoing sekarang dijamin
   selalu muncul berapapun volume incident lain.
4. **Filter date-range bisa nyembunyiin incident yang lagi Ongoing**: filter lama pakai
   waktu MULAI incident (`r.time >= since`) — outage yang sudah jalan 2 bulan (mulai
   sebelum "This Month") hilang dari tampilan default padahal masih aktif SEKARANG.
   Fix: filter berdasar waktu AKHIR aktivitas — incident Ongoing pakai "sekarang"
   (selalu masuk window manapun karena semua preset berakhir di "sekarang"), incident
   resolved pakai `resolved_time`.
5. **Live Alert Log tidak nampilin severity sama sekali** (regresi dari insiden
   restore `alarm.js` round sebelumnya + baru kerasa sekarang ada SlowResponse):
   operator tidak bisa bedain event critical (TargetDown) vs warning (SlowResponse)
   di Live Alert Log. Tambah badge severity (reuse `.history-sev`/`.history-sev-*`
   yang sudah ada) di tiap row.

**Test baru**: `test_ongoing_incident_survives_the_limit_even_when_stale`,
`MaintenanceRecoveryReconciliationTests` (2 test, termasuk skenario "masih down
setelah maintenance" untuk pastikan fix #2 tidak bikin duplikat incident), plus
assertion `total_down_seconds` ditambahkan ke test occurrence-dedup yang sudah ada.
Semua ~246 test + subtests lolos (dijalankan per-batch untuk hindari OOM di environment
lokal — bukan indikasi masalah pada test itu sendiri).

**Kendala**
- Backfill `total_down_seconds` untuk baris resolved lama (pre-migrasi) cuma pakai
  `duration_seconds` yang ada (durasi occurrence terakhir) sebagai pendekatan terbaik —
  durasi occurrence-occurrence sebelumnya sebelum migrasi ini memang tidak pernah
  disimpan di mana pun, tidak ada sumber lebih akurat untuk backfill.
- Tidak menemukan bug lain di jalur SlowResponse debounce, occurrences/first_seen,
  atau severity filter/status filter — sudah diverifikasi lewat unit test yang ada,
  bukan cuma review manual.

---

## [3.6.1] - 2026-09-05

### SlowResponse — Alert Warning Baru (Follow-up Task #9)

**Tujuan**
Isi gap arsitektur yang ditemukan di task #9 round sebelumnya ("0 Warning" karena
`TargetDown` di poller di-hardcode `severity="critical"` dan tidak ada jalur lain yang
pernah mengisi `"warning"`) — tanpa asal ubah threshold TargetDown, sesuai 3 syarat yang
dikonfirmasi user: debounce N=3 poll berturut-turut, belum notify Telegram, threshold
per-target configurable.

**Hasil**
- `compute_slow_response_transitions()` (`app.py`, pure function, pola sama seperti
  `compute_state_transitions()`): fire perlu N=3 sample **berturut-turut** di atas
  threshold, resolve juga perlu N=3 sample berturut-turut normal — 1 sample noise tidak
  memicu apapun (persis kelas bug yang sama dengan task #1, dicegah dari awal di alert
  type baru ini). Target yang DOWN men-supersede slow: streak direset dan SlowResponse
  yang lagi firing langsung di-resolve, tidak nunggu 3x "normal" yang toh tidak akan
  pernah datang selama down.
- Dievaluasi tiap poll tick (15 detik) untuk semua instance yang sedang di-scope
  (bukan cuma yang lagi transisi TargetDown), lewat `_poll_targets_once` →
  `record_alert_event(name="SlowResponse", severity="warning", ...)` — pipeline
  dedupe/history/telegram-gate yang sama dipakai ulang, tidak ada kode baru di situ.
- **Telegram sengaja di-skip untuk severity="warning"**: gate ditaruh satu tempat di
  `record_alert_event()` (`if severity != "warning": dispatch_alert_async(...)`) —
  dashboard/Incident History tetap dapat datanya penuh, cuma notifikasi push yang
  ditahan sampai keputusan lanjut diambil setelah lihat data riil beberapa hari.
- **Threshold per-target** (`slow_thresholds` table, default 500ms) via
  `SlowThresholdRepository` + `/api/slow-thresholds` (GET/PUT/DELETE), pola persis
  meniru `SlaTargetRepository`/`/api/sla-targets` yang sudah ada — host yang naturally
  lambat (mis. endpoint luar negeri) bisa di-override tanpa numpang di threshold global.
- Test baru `ComputeSlowResponseTransitionsTests` (7 test kasus, termasuk skenario
  persis yang diminta: 2x lambat → 1x normal → 2x lambat = belum fire; 3x lambat
  berturut-turut = fire 1x bukan 3x) dan `TelegramSeverityGateTests` (3 test,
  membuktikan warning tidak dispatch Telegram, critical tetap dispatch, warning tetap
  masuk history).
- `TargetDown` tidak disentuh sama sekali — tetap hardcode `critical` seperti sebelumnya.

**Kendala**
- Belum ada UI admin buat set/lihat `slow_thresholds` per-target — baru tersedia lewat
  API (`/api/slow-thresholds`). Ditambahkan kalau dibutuhkan dari dashboard.
- Belum ada test end-to-end untuk `_poll_targets_once` (fungsi poller penuh) — mengikuti
  konvensi test suite yang sudah ada, cuma pure function (`compute_state_transitions`,
  sekarang juga `compute_slow_response_transitions`) yang di-unit-test langsung; jalur
  poller penuh butuh mock jaringan Prometheus, di luar scope test ini.

---

## [3.6.0] - 2026-09-05

### Incident History — Perbaikan & Fitur Baru

**Tujuan**
Perbaikan bug data (occurrences selalu ×1), penambahan kolom Status/First Seen/root-cause,
klik-row-buka-drawer, filter tanggal/job, sorting, pagination, dan pembersihan angka
redundan di panel Incident History — sesuai task list Round ini. Acknowledgment/assignment
sengaja TIDAK termasuk scope.

**Hasil**
- **Root cause bug #1 (occurrences ×1) ditemukan & diperbaiki**: tabel `incidents` di SQLite
  menyimpan satu baris per `key` (host+alert) dan meng-*update in place* setiap transisi
  (`storage.py: IncidentRepository.record_alert_event`) — tapi baris itu tidak pernah
  menghitung berapa kali key yang sama sempat resolve lalu fire lagi. Kolom baru
  `occurrences` (increment tiap DOWN→UP→DOWN, gap UP = occurrence baru, definisi ini
  sama dengan dedupe transisi yang sudah ada) dan `first_seen` (waktu fire pertama,
  dipertahankan lintas re-fire) ditambahkan lewat migrasi `ALTER TABLE`. Test baru:
  `IncidentOccurrenceDedupTests` (`tests/test_alert_transitions.py`) membuktikan host
  flapping 5× dalam satu window menghasilkan 1 baris dengan `occurrences=5`, bukan 5 baris.
- **Kolom Status (Ongoing/Resolved)**: `get_history()` sekarang mengikutkan incident yang
  masih firing (sebelumnya query difilter `status='resolved'` saja — incident aktif tidak
  pernah muncul di History). Badge dot pulsing merah untuk Ongoing, ring abu-abu untuk
  Resolved. Filter chip All/Ongoing/Resolved digabung ke toolbar filter severity yang sudah
  ada (bukan baris filter baru).
- **Kolom First Seen** berdampingan dengan Last Seen; Total Down tetap ada sebagai durasi agregat.
- **Root cause sub-text**: `http_status_code` dan `last_error` di-snapshot ke tabel
  `incidents` saat alert fire (dari `classify_scrape_failure()` yang sudah ada di poller),
  ditampilkan sebagai sub-text di bawah nama host.
- **Klik row → drawer detail** yang sudah ada (bukan expand inline) — reuse
  `InstancesPage._openDrawer()`, fallback ke objek minimal kalau host sudah tidak
  termonitor lagi.
- **Filter tanggal** (This Month default / 7 Hari / 30 Hari / All Time) dan **filter Job**
  (dropdown, opsi dari data yang ada) ditambahkan ke toolbar. Stat card ikut ter-update
  sesuai range yang dipilih (sebelumnya hardcode "this month").
- **Sorting** (Last Seen default / Total Down / Occurrences) — dropdown kecil dekat
  Export CSV.
- **Redundant count dibersihkan**: teks di atas tabel sekarang reaktif terhadap SEMUA
  filter aktif ("N incidents (filtered)"); stat card "Total Incidents" jadi angka tetap
  (hanya bereaksi ke date range, bukan ke severity/status/job/search) — sebelumnya
  keduanya menghitung himpunan yang sama sehingga selalu tampil identik.
- **Pagination**: hanya `PAGE_SIZE=40` baris pertama yang dirender ke DOM, tombol
  "Show more" menambah batch berikutnya — bukan render semua row sekaligus.

**Kendala**
- **Investigasi "0 Warning" (task #9) — dilaporkan, TIDAK diubah** tanpa konfirmasi lebih
  dulu sesuai instruksi. Temuan: severity bukan bug klasifikasi/threshold yang salah kalibrasi
  — `severity` untuk `TargetDown` di poller (`app.py: _poll_targets_once`) di-hardcode
  `"critical"` selalu. Jalur satu-satunya yang bisa mengisi `"warning"` adalah label
  Alertmanager lewat `/webhook`, dan stack ini memang tidak menjalankan Alertmanager
  (lihat komentar di `app.py` dekat `_poll_targets_once`) — jadi `/webhook` tidak pernah
  dipanggil di jalur produksi. "0 Warning" adalah konsekuensi arsitektur (tidak ada
  produser severity warning), bukan alert yang gagal naik tingkat dari ambang batas.
  Perlu keputusan produk: tambah tier warning di poller (mis. latency tinggi tapi masih up),
  atau biarkan begitu — belum diimplementasikan menunggu konfirmasi.
- **Insiden kerja selama pengerjaan round ini**: draft awal `HistoryPage` sempat ditulis
  memakai tool yang overwrite seluruh file (`alarm.js`), menghapus ~823 baris pekerjaan
  belum-commit yang sudah ada sebelum round ini dimulai (grouping "Concept A" untuk Live
  Alert Log + hardening race-condition alarm audio yang sudah py CSS/HTML-nya tapi belum
  di-commit). Bagian yang hilang tidak bisa dipulihkan (tidak ada stash/commit/history
  editor). Atas persetujuan user: `alarm.js` di-restore ke commit terakhir, lalu (a) Live
  Alert Log direskin ulang ke class `.al-*` yang sudah ada di CSS (tanpa fitur grouping,
  ditandai `ponytail:` untuk dikerjakan terpisah), dan (b) hardening audio (`playAlarm`
  token guard, single-play-site policy) direkonstruksi ulang mengikuti spec lengkap di
  `tests/test_alarm_audio.js` (test ini sekarang lolos lagi). Kerja Incident History di
  round ini sendiri tidak memakai tool overwrite lagi setelah insiden ini (semua lewat
  edit bertarget) dan tidak terdampak.
- Semua 234 test Python + subtests dan self-check `tests/test_alarm_audio.js` lolos
  setelah pemulihan; tidak ada regresi pada suite yang ada.

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
