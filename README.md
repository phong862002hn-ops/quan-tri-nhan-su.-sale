# Message QA Local

Local scoring engine để chấm chất lượng hội thoại sale/CSKH theo bảng tiêu chuẩn 100 điểm. Bản này chạy hoàn toàn local bằng JSON + Python, không dùng API ngoài, không dùng database và không dùng AI.

## Mục tiêu project

- Mã hóa bảng tiêu chuẩn thành `data/rules.json`.
- Tạo bộ hội thoại mẫu để test.
- Chấm điểm hội thoại theo rule deterministic.
- Trả ra tổng điểm, grade, findings chi tiết, blacklist findings và evidence.
- Tạo training/coaching recommendation theo skill gap.
- Gom kết quả theo nhân viên để tạo employee scorecard local.
- Có CLI runner, viewer local và unit test để kiểm tra nhanh.

## Cấu trúc folder

```txt
README.md
requirements.txt
data/
  rules.json
  sample_conversations.json
  training_modules.json
  nhanh_config.example.json
  nhanh_cache.db          (sinh ra runtime, gitignored)
app/
  __init__.py
  schemas.py
  evaluator.py
  rule_engine.py
  grading.py
  test_runner.py
  viewer.py
  training.py
  employee_scorecard.py
  nhanh_client.py
  nhanh_adapter.py
  cache_db.py             (SQLite cache layer)
  sync_worker.py          (LightSyncWorker + HeavySyncWorker)
  sla_monitor.py          (SLA violation detection)
  business_hours.py       (business minutes calculator)
tests/
  test_evaluator.py
  test_grading.py
  test_rule_engine.py
  test_training.py
  test_employee_scorecard.py
  test_cache_db.py
  test_sync_worker.py
  test_business_hours.py
  test_sla_monitor.py
```

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `NHANH_LIGHT_SYNC_MINUTES` | `10` | Light worker cadence |
| `NHANH_HEAVY_SYNC_HOURS` | `4` | Heavy worker cadence |
| `QA_IDLE_THRESHOLD_HOURS` | `24` | How long a conv must be idle before the grading guard accepts it |
| `QA_SLA_THRESHOLD_MINUTES` | `60` | SLA business minutes before alert. Recommend `180` (3h) during the first deploy week to observe pattern, then drop to `60`. |
| `QA_BUSINESS_HOUR_START` | `8` | First hour of the SLA window (inclusive) |
| `QA_BUSINESS_HOUR_END` | `23` | First hour outside the SLA window (exclusive) |

## Thành phần chính

- `app/evaluator.py`: chấm điểm một hội thoại.
- `app/rule_engine.py`: xử lý từng loại rule.
- `app/training.py`: map failed findings sang skill gap và gợi ý training.
- `app/employee_scorecard.py`: gom nhiều evaluation để tạo scorecard theo nhân viên.
- `app/viewer.py`: local dashboard để xem trực quan case, scorecard và training recommendation.
- `app/cache_db.py` + `app/sync_worker.py`: cache hội thoại Nhanh trong SQLite + worker đồng bộ nền (xem "Caching architecture" bên dưới).

## Cài đặt

```bash
pip install -r requirements.txt
python -m app.viewer
# Mở http://127.0.0.1:8080/dashboard
```

## Kiến trúc Sync 2 tốc độ

Lần đầu mở dashboard không còn gọi sang Nhanh API trực tiếp (cũ ~45 giây/request). Thay vào đó có **2 background sync worker** chạy song song:

| Worker | Cadence | Mục đích |
|---|---|---|
| **Light** (`LightSyncWorker`) | **10 phút** (env `NHANH_LIGHT_SYNC_MINUTES`) | 1 call `list_conversations` ở top, chỉ fetch messages cho conv có `updatedAt` đổi. Chạy SLA detection + grading guard cho conv đó. Trễ tối đa 10 phút để phát hiện tin mới. |
| **Heavy** (`HeavySyncWorker`) | **4 giờ** (env `NHANH_HEAVY_SYNC_HOURS`) | Full crawl per-pageId để không sót conv. Backfill 7 ngày khi DB trống. |

- Cả 2 worker viết vào cùng `data/nhanh_cache.db` (SQLite + WAL mode).
- **Dashboard 100% đọc từ DB** → response <200ms với 1000+ conversations, kể cả khi 5+ user truy cập đồng thời.
- **Lần đầu khởi động** (DB trống): heavy worker tự backfill ngay, banner hiển thị progress (X/Y), JS auto-poll `/api/sync-status` mỗi 5s rồi reload khi xong.
- **Click chi tiết 1 conversation** (`?live_conversation_id=...`) → refresh live cho riêng conv đó (stale-while-revalidate). Fail → fallback cache.
- **Nút "Đồng bộ ngay"** trên banner → POST `/api/refresh` trigger CẢ light lẫn heavy ngoài lịch. Trùng → reject với `{ok: false, reason: "sync_in_progress"}`.
- **Đổi `data/rules.json`** → `ruleset_version` hash đổi → conv được re-evaluate trong tick kế tiếp.

Nhanh **không có webhook cho conversation/message** ([apidocs.nhanh.vn](https://apidocs.nhanh.vn/v3/webhooks/webhooks) chỉ có order/product/inventory) → polling là bắt buộc.

## Guard chấm điểm (Grading guard)

Hội thoại chỉ được chấm khi **(1)** idle ≥ 24h kể từ tin cuối VÀ **(2)** tin cuối là từ khách. Lý do:

- **Idle < 24h** = hội thoại còn đang diễn ra, chưa kết thúc → chấm sớm sẽ oan cho sale.
- **Tin cuối từ sale** = sale đã làm phần của mình, khách không quay lại → coi như hoàn tất, không cần chấm.
- Chỉ khi **khách nhắn xong, sale có cơ hội phản hồi, và conv im 24h+** thì mới đủ điều kiện đánh giá.

Threshold idle override: `QA_IDLE_THRESHOLD_HOURS=12` cho môi trường test. Hàm `is_conversation_ready_for_grading` trong [app/evaluator.py](app/evaluator.py).

## SLA monitoring (Real-time alert)

- Khi tin cuối của conv là từ khách (chuỗi customer message chưa được reply), worker tính **business minutes** trôi qua trong giờ làm việc.
- Nếu `business_minutes > QA_SLA_THRESHOLD_MINUTES` (default 60) → flag vi phạm vào bảng `sla_violations`.
- Tab **🔴 Alert SLA** trên dashboard hiển thị tất cả vi phạm `status='open'`. Sale reply → light worker tick kế tiếp tự `resolve` violation.
- **Giờ làm việc** mặc định **8h-23h** (VN tz), override `QA_BUSINESS_HOUR_START` / `QA_BUSINESS_HOUR_END`.
- Edge case: khách nhắn ngoài giờ làm → SLA bắt đầu tính từ 8h sáng hôm sau. Khách nhắn 22:50 → SLA chỉ tính 22:50-23:00 (10') + tiếp tục từ 8:00 sáng hôm sau.
- Logic chi tiết: [app/sla_monitor.py](app/sla_monitor.py), business hour math: [app/business_hours.py](app/business_hours.py).

## 3 tabs dashboard

- 🟡 **Đang diễn ra** — conv chưa qua guard (idle < 24h hoặc tin cuối từ sale). KHÔNG có điểm.
- ✅ **Đã chấm** — conv đã pass guard và có evaluation. Có scorecard.
- 🔴 **Alert SLA** — vi phạm SLA đang `open`. Click "Xem & Phản hồi" mở conv chi tiết.

## Bảng điểm

- Thái độ và Tác phong: 20 điểm
- Kiến thức Chuyên môn và Tư vấn: 30 điểm
- Quy trình SOP: 30 điểm
- Xử lý Phản hồi và Khiếu nại: 20 điểm
- Blacklist: nếu vi phạm thì hội thoại bị đánh giá `Không đạt`

Xếp loại:

- `90 - 100`: Xuất sắc
- `75 - 89`: Tốt
- `50 - 74`: Trung bình
- `< 50` hoặc dính blacklist: Không đạt

## Chạy local

Chấm toàn bộ hội thoại mẫu:

```powershell
python -m app.test_runner
```

Chấm một hội thoại:

```powershell
python -m app.test_runner --conversation-id hair-pass-001
```

Xuất JSON:

```powershell
python -m app.test_runner --conversation-id hair-pass-001 --json
```

Chạy unit test:

```powershell
python -m unittest discover -s tests -v
```

Mở local viewer:

```powershell
python -m app.viewer
```

Sau đó mở:

```txt
http://127.0.0.1:8080/dashboard
```

Dashboard hiện có:

- Employee Scorecards
- Top nhân viên cần training
- Top kỹ năng yếu nhất
- Training recommendations
- Transcript và điểm chi tiết theo từng hội thoại

## Dữ liệu hội thoại mẫu

File [data/sample_conversations.json](/C:/Users/Tran%20Phong/Documents/New%20project/data/sample_conversations.json) dùng format:

```json
{
  "external_id": "hair-pass-001",
  "channel": "pancake",
  "employee": {
    "id": "emp_001",
    "name": "Nhân viên A"
  },
  "metadata": {
    "has_complaint": false,
    "flags": {
      "incorrect_product_info": false
    }
  },
  "messages": [
    {
      "id": "m1",
      "sender_type": "employee",
      "text": "Dạ chào bạn, Buddy có thể hỗ trợ gì cho mình ạ?",
      "attachments": [],
      "sent_at": "2026-05-04T09:00:00+07:00"
    }
  ]
}
```

## Rules

File [data/rules.json](/C:/Users/Tran%20Phong/Documents/New%20project/data/rules.json) có 2 phần:

- `categories`: rule chấm điểm bình thường
- `blacklist`: rule loại trực tiếp

Rule types hiện support:

- `keyword_any`
- `keyword_all`
- `keyword_count_min`
- `forbidden_keyword`
- `max_emoji_per_message`
- `employee_last_message`
- `customer_not_left_unanswered`
- `attachment_required`
- `complaint_flow`
- `missing_required_before_advice`
- `metadata_flag`

## Training modules

File [data/training_modules.json](/C:/Users/Tran%20Phong/Documents/New%20project/data/training_modules.json) định nghĩa nội dung coaching cho từng skill.

Mỗi module gồm:

- `skill`
- `title`
- `description`
- `lessons`
- `sample_phrases`
- `practice_tasks`

Anh có thể chỉnh file này để đổi nội dung training mà không cần sửa code Python.

## Output chính

Evaluation result vẫn giữ format tương thích với bản cũ:

```json
{
  "conversation_id": "hair-pass-001",
  "total_score": 92,
  "max_score": 100,
  "grade": "Xuất sắc",
  "passed": true,
  "blacklist_triggered": false,
  "category_scores": [],
  "findings": [],
  "blacklist_findings": []
}
```

Ngoài ra viewer còn build:

- employee scorecard
- top failed skills
- training recommendations theo hội thoại và theo nhân viên

## Ghi chú

- Bản này chưa gọi API ngoài.
- Bản này chưa dùng AI/LLM.
- Bản này chưa có database.
- Bản này chưa có web app production hay backend online.
- Viewer chỉ đọc file local và render kết quả chấm tại chỗ.
- Training recommendation và employee scorecard cũng chạy local từ JSON + Python.
## Nhanh API local

File config local:

- [data/nhanh_config.local.json](/C:/Users/Tran%20Phong/Documents/New%20project/data/nhanh_config.local.json)

CLI helper:

- [app/nhanh_client.py](/C:/Users/Tran%20Phong/Documents/New%20project/app/nhanh_client.py)

Các lệnh cơ bản:

```powershell
python -m app.nhanh_client check-token
python -m app.nhanh_client list-conversations --size 5 --type 2
python -m app.nhanh_client list-messages --conversation-id YOUR_CONVERSATION_ID --size 20
```

Ghi chú:

- `check-token` cần `secret_key` trong config local.
- `list-conversations --type 2` là lấy hội thoại tin nhắn.
- `list-conversations --type 1` là lấy hội thoại bình luận.
- File `data/nhanh_config.local.json` đã được đưa vào `.gitignore`.
## Nhanh live review

- File local config: `data/nhanh_config.local.json`
- CLI test hội thoại live:
  - `python -m app.nhanh_client list-conversations --size 5 --type 2`
  - `python -m app.nhanh_client list-messages --conversation-id YOUR_CONVERSATION_ID --size 20`
- Viewer live:
  - mở `http://127.0.0.1:8080/dashboard`
  - nhập `conversationId` trong khối `Nhanh Live Review`
  - viewer sẽ gọi trực tiếp Nhanh API, map dữ liệu sang format nội bộ và chấm ngay trên dashboard

Ghi chú:

- `app/nhanh_adapter.py` chịu trách nhiệm map response Nhanh sang `Conversation/messages`.
- Nếu máy local bị lỗi SSL của Python trên Windows, config local có thể để `verify_ssl = false` để test nội bộ.
