# 🤖 Solana Meme Coin Trading Bot

Bot trading meme coin otomatis di jaringan **Solana** dengan arsitektur modular 7-lapis, **Security Screening Gate Keras**, **Risk Control yang Tidak Dapat Di-override**, serta **Self-Learning Terkontrol dengan Walk-Forward Validation**.

---

## 🏗️ Arsitektur 7 Modul

| Modul | Folder | Fungsi Utama |
|---|---|---|
| **Core** | `src/core/` | Loader `config.yaml` + `.env`, model Pydantic, skema & CRUD asinkron SQLite (8 tabel) |
| **Modul 1 — Deteksi Token** | `src/detection/` | Polling DexScreener & Birdeye + streaming Helius WebSocket (`logsSubscribe` Raydium & Pump.fun), simpan snapshot awal |
| **Modul 2 — Screening Keamanan** | `src/screening/` | **Gate Keras** (gagal satu = skip): Mint & Freeze Authority revoked, Top 10 Holder $< 50\%$, Likuiditas & LP Burned/Locked, Simulasi Jual Honeypot & Pajak, RugCheck API, Riwayat Deployer |
| **Modul 3 — Scoring & Entry** | `src/scoring/`, `src/execution/` | Normalisasi 5 fitur $\in [0,1]$, skor komposit berbobot, eksekusi lewat Jupiter API + opsi Jito Bundle & dynamic priority fee |
| **Modul 4 — Exit & Manajemen Posisi** | `src/position/` | Position sizing (% modal & batas SOL), Take-Profit bertahap (TP1/TP2/TP3), Hard Stop-Loss, Trailing Stop, Time-Stop, Emergency Liquidity/Rug Exit |
| **Modul 5 — Risk Control** | `src/risk/` | Batas rugi harian, drawdown maksimum dari ekuitas puncak, Circuit Breaker, Kill Switch (`./KILL_SWITCH` & `/kill` Telegram), validasi saldo wallet khusus bot |
| **Modul 6 — Logging & Notifikasi** | `src/logging_/` | Pencatatan audit setiap keputusan (termasuk token yang di-skip), sensor otomatis private key/API key (`SecretRedactingFilter`), notifikasi Telegram |
| **Modul 7 — Belajar dari Kesalahan** | `src/learning/` | Post-trade loss pattern analyzer, Walk-Forward + Out-of-Sample Backtester, usulan perubahan maksimal 1 parameter ($\le 10\%$/siklus), versioning & auto-rollback |

---

## 🚀 Cara Instalasi & Menjalankan

### 1. Persiapan Environment
Disarankan menggunakan **Python 3.11+** dalam virtual environment:

```powershell
# Buat dan aktifkan virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 2. Konfigurasi `.env` dan `config.yaml`
Salin template `.env.example` menjadi `.env`:
```powershell
Copy-Item .env.example .env
```
- **PENTING**: `WALLET_PRIVATE_KEY` dan API keys **HANYA** boleh diisi di `.env`. File `.env` sudah masuk di `.gitignore` dan disensor otomatis oleh `SecretRedactingFilter` agar tidak pernah tercatat ke log.
- Semua parameter strategi berada di `config.yaml`. Mode default adalah `PAPER`.

### 3. Inisialisasi Database SQLite
```powershell
python scripts/migrate_db.py
```

### 4. Menjalankan Unit & Integration Test
```powershell
python -m pytest tests/ -v
```

### 5. Menjalankan Backtester / Paper Simulation Offline
Menjalankan simulasi walk-forward backtest, menghitung **Win Rate, Expectancy, Profit Factor, Max Drawdown**, serta menganalisis pola kerugian:
```powershell
python scripts/backtest.py
```

### 6. Mencetak Laporan Performa & Statistik Skip
Menampilkan performa dari database `./data/bot.db`, rincian alasan token di-skip oleh screening/scoring, versi konfigurasi aktif, dan daftar usulan optimasi yang menunggu persetujuan:
```powershell
python scripts/report.py
```

### 7. Menjalankan Bot (Mode Default: PAPER)
```powershell
python -m src.bot --config config.yaml --capital 10.0
```

### 8. Mengaktifkan Kill Switch Darurat
Anda dapat menghentikan bot dan menutup semua posisi terbuka kapan saja dengan salah satu cara berikut:
1. **Via File Flag**: Buat file kosong bernama `KILL_SWITCH` di root proyek:
   ```powershell
   New-Item -ItemType File -Path .\KILL_SWITCH
   ```
2. **Via Telegram**: Kirim perintah `/kill <alasan>` dari akun Telegram yang terdaftar di `TELEGRAM_CHAT_ID`.

---

## ✅ Checklist Wajib Sebelum Pindah ke Mode `LIVE`

> [!CAUTION]
> **Jangan pernah mengaktifkan mode `LIVE` sebelum seluruh poin pada checklist di bawah ini terpenuhi.** Saat ini jalur transaksi on-chain di `src/execution/executor.py` sengaja disetel **fail-closed** (menolak eksekusi nyata) sampai Anda mengintegrasikan penandatanganan transaksi `solders.transaction.VersionedTransaction` dan mengujinya penuh.

- [ ] **1. Riwayat Paper Trading Memadai**: Bot sudah berjalan di mode `PAPER` dengan data pasar nyata minimal **100+ transaksi selesai** dan menunjukkan *Expectancy* positif, *Profit Factor* $> 1.3$, serta *Max Drawdown* di bawah batas `max_drawdown_pct`.
- [ ] **2. Evaluasi Rasio Skip Screening & Scoring**: Periksa output `python scripts/report.py` untuk memastikan gate keamanan memblokir token honeypot/rug tanpa melewatkan seluruh kandidat likuid.
- [ ] **3. Wallet Khusus Bot (Burner / Hot Wallet Kecil)**:
  - Buat wallet Solana baru yang **terpisah** dari wallet utama/cold storage Anda.
  - Isi saldo hanya sebesar modal eksperimen kecil ($\le 2\text{--}5\text{ SOL}$, dan wajib $< 20\text{ SOL}$ sesuai batas keras `HARD_RISK_LIMITS.max_bot_wallet_sol`).
- [ ] **4. Keamanan Rahasia (`.env`)**:
  - Pastikan `git status` tidak pernah melacak `.env`, `data/*.db`, atau `logs/`.
  - Pastikan `WALLET_PRIVATE_KEY` hanya dibaca dari environment variable.
- [ ] **5. RPC & Proteksi MEV (Jito Bundle)**:
  - Gunakan RPC berbayar/dedicated berlatensi rendah (misal Helius/Triton), bukan RPC publik gratis yang mudah terkena rate-limit saat pasar volatil.
  - Aktifkan `execution.use_jito_bundle: true` untuk mencegah serangan *sandwich* pada transaksi entry maupun exit.
- [ ] **6. Implementasi & Audit Penandatanganan Transaksi**:
  - Lengkapi penandatanganan `VersionedTransaction` (`solders.keypair.Keypair.from_base58_string`) dan konfirmasi blok (`confirmTransaction`) pada `TransactionExecutor._execute_live_buy` & `_execute_live_sell`.
  - Uji coba alur penandatanganan dan pengiriman dengan ukuran transaksi mikro ($0.01\text{ SOL}$) terlebih dahulu.
- [ ] **7. Verifikasi Kill Switch & Notifikasi Telegram**:
  - Pastikan notifikasi Telegram (`entry`, `exit`, `error`, `risk_limit`) masuk secara real-time.
  - Uji perintah `/kill` via Telegram dan pastikan bot langsung berhenti serta membuat file `./KILL_SWITCH`.
- [ ] **8. Kunci Modul Belajar di Mode LIVE**:
  - Pastikan `learning.require_manual_approval: true` tetap aktif sehingga tidak ada parameter yang berubah tanpa peninjauan manual Anda.
- [ ] **9. Konfirmasi Eksplisit**:
  - Set `BOT_MODE=LIVE` di `.env`, ubah `bot.live_confirm_required: false` di `config.yaml`, dan ketik frasa konfirmasi manual saat startup.

