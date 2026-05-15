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
data/
  rules.json
  sample_conversations.json
  training_modules.json
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
tests/
  test_evaluator.py
  test_grading.py
  test_rule_engine.py
  test_training.py
  test_employee_scorecard.py
```

## Thành phần chính

- `app/evaluator.py`: chấm điểm một hội thoại.
- `app/rule_engine.py`: xử lý từng loại rule.
- `app/training.py`: map failed findings sang skill gap và gợi ý training.
- `app/employee_scorecard.py`: gom nhiều evaluation để tạo scorecard theo nhân viên.
- `app/viewer.py`: local dashboard để xem trực quan case, scorecard và training recommendation.

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
