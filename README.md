# Message QA Local

Hệ thống chấm chất lượng hội thoại sale/CSKH theo bảng tiêu chuẩn 100 điểm, kèm dashboard quản lý nhân sự — chạy hoàn toàn local bằng JSON + Python, không cần API ngoài, không database.

## Tính năng chính

### Chấm điểm & gợi ý training
- Rule engine deterministic chấm hội thoại theo categories + blacklist
- Skill gap mapping → gợi ý training module phù hợp
- Employee scorecard tổng hợp

### Quản lý nhân sự (Phase 1-12)
- **Trend tracking**: lưu lịch sử evaluation theo ngày, sparkline điểm theo thời gian
- **Import dữ liệu thực**: CSV/JSON batch import từ Pancake, Zalo...
- **Filter & search**: lọc hội thoại theo nhân viên, ngày, grade, blacklist, kênh, full-text search
- **Export báo cáo**: xuất CSV cho kết quả + scorecard
- **Response time analysis**: đo thời gian phản hồi nhân viên
- **Supervisor review**: ghi chú + override điểm khi rule chấm sai
- **Alert system**: tự động cảnh báo nhân viên vi phạm Blacklist hoặc tụt dốc
- **Rules versioning**: snapshot rules.json theo version
- **Weekly comparison**: so sánh tuần này vs tuần trước, đánh xu hướng cải thiện/tụt dốc
- **Custom timeline**: chọn khoảng thời gian phân tích bất kỳ (7 ngày, 30 ngày, tháng, custom)
- **Department overview**: KPI tổng phòng ban, top/bottom performer, phân phối điểm

## Cài đặt & chạy

```bash
# Yêu cầu: Python 3.9+
python3 -m app.viewer
```

Mở [http://127.0.0.1:8080/dashboard](http://127.0.0.1:8080/dashboard).

### Production mode (không kèm demo data)

```bash
QA_INCLUDE_DEMO=0 python3 -m app.viewer
```

## CLI commands

### Chấm điểm

```bash
# Chấm tất cả hội thoại trong sample_conversations.json
python3 -m app.test_runner

# Chấm 1 hội thoại
python3 -m app.test_runner --conversation-id hair-pass-001

# Output JSON
python3 -m app.test_runner --conversation-id hair-pass-001 --json

# Chỉ định file input khác
python3 -m app.test_runner --input path/to/conversations.json

# Không lưu vào history
python3 -m app.test_runner --no-save
```

### Import hội thoại

```bash
# Từ CSV — định dạng: conversation_id, channel, employee_id, employee_name, sender_type, text, sent_at[, attachment_type, attachment_name]
python3 -m app.importer --file path/to/data.csv

# Từ JSON batch
python3 -m app.importer --file path/to/data.json

# Ghi đè thay vì merge
python3 -m app.importer --file path/to/data.csv --overwrite
```

### Export CSV

```bash
# Export kết quả từng hội thoại
python3 -m app.exporter --type results --output report.csv

# Export employee scorecard
python3 -m app.exporter --type scorecard --output scorecard.csv
```

### Test

```bash
python3 -m unittest discover -s tests -v
```

## API endpoints

| Method | Path | Mô tả |
|---|---|---|
| GET | `/dashboard` | Dashboard chính (hỗ trợ `?preset=...&tab=...`) |
| GET | `/api/results` | JSON: evaluation results + scorecards |
| GET | `/api/weekly-analysis` | JSON: so sánh 2 kỳ thời gian |
| GET | `/api/department-overview` | JSON: KPI tổng phòng ban |
| GET | `/api/export/csv` | Tải CSV kết quả |
| GET | `/api/export/scorecard-csv` | Tải CSV scorecard |
| POST | `/api/snapshot` | Chấm tất cả hội thoại + lưu vào history |
| POST | `/api/review` | Ghi review note cho hội thoại |

### Query params cho timeline analysis

- `preset=this_vs_last_week` (mặc định)
- `preset=last_7_days`
- `preset=last_30_days`
- `preset=this_vs_last_month`
- `preset=custom&from_a=YYYY-MM-DD&to_a=YYYY-MM-DD&from_b=YYYY-MM-DD&to_b=YYYY-MM-DD`

## Cấu trúc folder

```
.
├── README.md
├── PLAN.md                          # Plan triển khai 13 phases
├── app/
│   ├── alert_engine.py              # Phát hiện nhân viên vi phạm vượt ngưỡng
│   ├── department_overview.py       # KPI tổng phòng ban
│   ├── employee_scorecard.py        # Scorecard nhân viên
│   ├── evaluator.py                 # Chấm điểm hội thoại
│   ├── exporter.py                  # Export CSV
│   ├── grading.py                   # Quy đổi điểm → grade
│   ├── history.py                   # Lưu/đọc lịch sử evaluation
│   ├── importer.py                  # Import CSV/JSON batch
│   ├── response_time.py             # Tính thời gian phản hồi
│   ├── review_store.py              # Lưu manual review notes
│   ├── rule_engine.py               # Logic chấm rule
│   ├── schemas.py                   # Dataclasses chính
│   ├── test_runner.py               # CLI runner
│   ├── training.py                  # Skill gap → training module
│   ├── viewer.py                    # HTTP server + dashboard HTML
│   └── weekly_analysis.py           # So sánh 2 kỳ thời gian
├── data/
│   ├── alert_config.json            # Ngưỡng cảnh báo
│   ├── history/                     # Lịch sử evaluation (data thật)
│   ├── history_demo/                # Lịch sử demo (chỉ load khi QA_INCLUDE_DEMO=1)
│   ├── reviews/                     # Manual review notes
│   ├── rules.json                   # Bộ rule chấm điểm
│   ├── rules_history/               # Snapshot rules theo version
│   ├── sample_conversations.json    # Hội thoại mẫu
│   └── training_modules.json        # Nội dung training
└── tests/                           # 63 unit tests
```

## Bảng điểm

- Thái độ và Tác phong: 20 điểm
- Kiến thức Chuyên môn và Tư vấn: 30 điểm
- Quy trình SOP: 30 điểm
- Xử lý Phản hồi và Khiếu nại: 20 điểm
- Blacklist: vi phạm → đánh giá `Không đạt`

Xếp loại:
- 90 - 100: Xuất sắc
- 75 - 89: Tốt
- 50 - 74: Trung bình
- < 50 hoặc dính blacklist: Không đạt

## Cấu hình ngưỡng cảnh báo

File `data/alert_config.json`:

```json
{
  "blacklist_per_week": 2,
  "blacklist_per_month": 4,
  "low_score_threshold": 50.0,
  "low_score_streak": 3,
  "avg_score_below": 60.0,
  "min_conversations": 2
}
```

## Ghi chú

- Không gọi API ngoài, không dùng AI/LLM
- Không có database — toàn bộ là file JSON local
- Dashboard là server HTTP đơn giản dùng stdlib `http.server`
- Demo data trong `data/history_demo/` không phải dữ liệu thật
