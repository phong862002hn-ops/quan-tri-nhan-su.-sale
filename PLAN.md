# Kế hoạch nâng cấp — Message QA Local

Mỗi bước hoàn thành sẽ được đánh dấu `[x]`.  
Bước đang làm: `[~]` · Chưa làm: `[ ]`

---

## Phase 1 — Trend Tracking (Lưu lịch sử đánh giá)
> Mục tiêu: Biết nhân viên đang tiến bộ hay tụt dốc theo thời gian.

- [x] 1.1 Tạo `app/history.py` — lưu kết quả evaluation vào `data/history/YYYY-MM-DD.json`
- [x] 1.2 Cập nhật `app/test_runner.py` — auto-save sau mỗi lần chấm
- [x] 1.3 Cập nhật `app/employee_scorecard.py` — thêm hàm đọc lịch sử nhiều ngày
- [x] 1.4 Cập nhật `app/viewer.py` — hiển thị bảng trend (avg score theo ngày) cho từng nhân viên

---

## Phase 2 — Import hội thoại thực
> Mục tiêu: Đưa hội thoại thực từ Pancake/Zalo vào hệ thống thay vì chỉ dùng sample.

- [x] 2.1 Tạo `app/importer.py` — đọc file CSV/JSON batch, validate và convert sang schema chuẩn
- [x] 2.2 Thêm CLI: `python -m app.importer --file path/to/file.csv [--append]`
- [x] 2.3 Cập nhật `app/test_runner.py` — nhận tham số `--input-file` để chấm file tùy chọn

---

## Phase 3 — Filter & Search trên Dashboard
> Mục tiêu: Khi có 100+ hội thoại, quản lý cần lọc nhanh để xem đúng ca cần review.

- [x] 3.1 Thêm filter bar trên dashboard (lọc theo nhân viên, grade, blacklist, kênh)
- [x] 3.2 Thêm filter theo khoảng ngày (từ ngày — đến ngày)
- [x] 3.3 Thêm ô tìm kiếm full-text theo conversation ID hoặc tên nhân viên
- [x] 3.4 Thêm sort cho bảng Employee Scorecards (sort theo avg score, passed rate, blacklist count)

---

## Phase 4 — Export Báo cáo Excel/CSV
> Mục tiêu: Quản lý có thể gửi báo cáo tuần/tháng cho ban giám đốc.

- [x] 4.1 Tạo `app/exporter.py` — export scorecards và evaluation results ra CSV
- [x] 4.2 Thêm endpoint `/api/export/csv` và `/api/export/scorecard-csv` trên viewer
- [x] 4.3 Thêm nút "Export CSV" trên dashboard

---

## Phase 5 — Response Time Analysis
> Mục tiêu: Đo thời gian phản hồi của nhân viên — KPI quan trọng không kém điểm nội dung.

- [x] 5.1 Tạo `app/response_time.py` — tính avg/max response time từ timestamps trong hội thoại
- [x] 5.2 Cập nhật `app/evaluator.py` — đính kèm response time vào EvaluationResult
- [x] 5.3 Cập nhật dashboard — hiển thị response time trên từng case và scorecard

---

## Phase 6 — Supervisor Review & Manual Override
> Mục tiêu: QA leader có thể ghi chú, đánh dấu đã review, hoặc override điểm khi rule chấm sai.

- [x] 6.1 Tạo schema `ReviewNote` trong `app/schemas.py`
- [x] 6.2 Tạo `app/review_store.py` — lưu/đọc manual reviews vào `data/reviews/`
- [x] 6.3 Thêm endpoint `POST /api/review` để ghi review note qua dashboard
- [x] 6.4 Cập nhật dashboard — hiển thị nút "Mark Reviewed", ô ghi note, badge "Reviewed"

---

## Phase 7 — Alert System (Cảnh báo vi phạm nhiều lần)
> Mục tiêu: Tự động flag nhân viên vi phạm blacklist vượt ngưỡng trong khoảng thời gian.

- [x] 7.1 Tạo `app/alert_engine.py` — đọc lịch sử, so với threshold config, tạo danh sách alert
- [x] 7.2 Tạo `data/alert_config.json` — file cấu hình ngưỡng độc lập (blacklist/tuần, blacklist/tháng, streak điểm thấp)
- [x] 7.3 Cập nhật dashboard — hiển thị panel "Cảnh báo cần xử lý" nếu có alert

---

## Phase 8 — Rules Versioning
> Mục tiêu: Khi cập nhật rules.json, điểm lịch sử không bị thay đổi do chấm lại theo rules mới.

- [x] 8.1 `data/rules.json` đã có field `version: 1.1.0`
- [x] 8.2 Cập nhật `app/evaluator.py` — lưu `ruleset_version` vào EvaluationResult
- [x] 8.3 Tạo `data/rules_history/` — auto snapshot khi version chưa có trong history
- [x] 8.4 Cập nhật dashboard — hiển thị version rules trên hero và từng case card

---

---

## Phase 9 — Phân tích nhân sự theo tuần (Weekly Comparison)
> Mục tiêu: So sánh hiệu suất tuần này vs tuần trước cho từng nhân viên — KPI quan trọng nhất với quản lý.

- [x] 9.1 Tạo `app/weekly_analysis.py` — tính avg score, passed rate, blacklist, số ca cho từng tuần
- [x] 9.2 Tạo hàm so sánh hai tuần (delta, % thay đổi, xu hướng tăng/giảm)
- [x] 9.3 Thêm panel "Phân tích nhân sự theo tuần" trên dashboard với bảng so sánh
- [x] 9.4 Sinh dữ liệu lịch sử mẫu cho tuần trước để demo tính năng
- [x] 9.5 Thêm endpoint `/api/weekly-analysis` trả JSON

---

---

## Phase 10 — Custom Timeline Analysis (Chọn khoảng thời gian tùy ý)
> Mục tiêu: Quản lý có thể chọn bất kỳ 2 khoảng thời gian để so sánh, không chỉ "tuần này vs tuần trước".

- [x] 10.1 Mở rộng `app/weekly_analysis.py` — `analyze_period_comparison(period_a, period_b)`
- [x] 10.2 Thêm preset timeline: "Tuần này vs Tuần trước", "7 ngày qua", "30 ngày qua", "Tháng này vs tháng trước", "Tùy chọn"
- [x] 10.3 Thêm date range picker trên dashboard, GET form với preset/from_a/to_a/from_b/to_b
- [x] 10.4 Endpoint `/api/weekly-analysis?preset=...` hoặc `?from_a=&to_a=&from_b=&to_b=`

---

## Phase 11 — Department Overview Dashboard (Dashboard tổng phòng ban)
> Mục tiêu: God-view cho quản lý — toàn bộ KPI quan trọng trên 1 panel duy nhất ở đầu trang.

- [x] 11.1 Tạo `app/department_overview.py` — tính KPI tổng: total ca, total nhân viên, team avg, pass rate, blacklist, response time TB
- [x] 11.2 Tính phân phối điểm: số nhân viên Xuất sắc/Tốt/Trung bình/Không đạt
- [x] 11.3 Best & worst performer của kỳ + danh sách critical (BL hoặc avg <50)
- [x] 11.4 Trend chart team avg theo ngày (sparkline SVG)
- [x] 11.5 Render hero panel "Tổng quan phòng ban" ở đầu dashboard với các KPI card lớn
- [x] 11.6 Endpoint `/api/department-overview` (bonus)

---

---

## Phase 12 — UI Redesign theo style admin platform
> Mục tiêu: Sửa bug label "tuần này/tuần trước" hardcode, redesign UI theo style screenshot (tabs + clean filter pills).

- [x] 12.1 Fix bug: nhãn period động theo preset (Tuần này / Tháng này / 7 ngày qua...) thay vì hardcode "Tuần này"
- [x] 12.2 Tab navigation ở đầu trang: Tổng quan, Phân tích nhân sự, Cảnh báo, Training, Hội thoại
- [x] 12.3 Redesign filter row thành dropdown pill (Khung Thời Gian + popup panel chọn preset/custom)
- [x] 12.4 JS toggle hiển thị section theo tab active + lưu state vào URL `?tab=`

---

---

## Phase 13 — Polish & Production-readiness
> Mục tiêu: Sửa các UX rough edges, dọn dead code, viết test, cập nhật docs.

### 13.A — Fix UX issues
- [x] 13.1 Empty state cho tab Cảnh báo khi không có alert
- [x] 13.2 Bỏ pill "Loại đánh giá" fake
- [x] 13.3 Fix Cases tab — ẩn timeline pill khi vào tab Cases (chỉ hiển thị khi tab Tổng quan / Phân tích nhân sự)
- [x] 13.4 Color consistency — accent cam #ee4d2d toàn hệ thống

### 13.B — Code cleanup
- [x] 13.5 Tách seed data ra `data/history_demo/` + env var `QA_INCLUDE_DEMO`
- [x] 13.6 Xóa dead code `_render_timeline_picker` + CSS không dùng
- [x] 13.7 Refactor: bỏ auto-save history khi GET `/dashboard`, thay bằng `POST /api/snapshot` + nút "Chấm & Lưu" trên topbar

### 13.C — Tests
- [x] 13.8 Unit tests cho: `history`, `weekly_analysis`, `department_overview`, `alert_engine` (37 tests mới)
- [x] 13.9 Unit tests cho: `importer`, `exporter`, `response_time`, `review_store` (đã bao gồm trong 37 tests)

### 13.D — Documentation
- [x] 13.10 Cập nhật README.md với tất cả tính năng + API endpoints + CLI mới

---

## Tiến độ tổng thể

| Phase | Tên | Trạng thái |
|---|---|---|
| 1 | Trend Tracking | ✅ Hoàn thành |
| 2 | Import hội thoại thực | ✅ Hoàn thành |
| 3 | Filter & Search Dashboard | ✅ Hoàn thành |
| 4 | Export Excel/CSV | ✅ Hoàn thành |
| 5 | Response Time Analysis | ✅ Hoàn thành |
| 6 | Supervisor Review | ✅ Hoàn thành |
| 7 | Alert System | ✅ Hoàn thành |
| 8 | Rules Versioning | ✅ Hoàn thành |
| 9 | Weekly Comparison | ✅ Hoàn thành |
| 10 | Custom Timeline Analysis | ✅ Hoàn thành |
| 11 | Department Overview Dashboard | ✅ Hoàn thành |
| 12 | UI Redesign + Fix label bug | ✅ Hoàn thành |
| 13 | Polish & Production-readiness | ✅ Hoàn thành |
