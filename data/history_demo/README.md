# Demo history (không phải dữ liệu thật)

Folder này chứa data evaluation giả để demo các tính năng:
- Trend tracking nhiều ngày
- Weekly comparison (tuần này vs tuần trước)
- Department overview với history dài

## Cách sử dụng

- **Bật demo data** (mặc định): `QA_INCLUDE_DEMO=1` → load cả `history/` và `history_demo/`
- **Tắt demo data** (production): `QA_INCLUDE_DEMO=0` → chỉ load `history/`

```bash
# Production mode — chỉ data thật
QA_INCLUDE_DEMO=0 python -m app.viewer

# Demo mode — kèm data demo
python -m app.viewer
```

## Khi đưa lên production

Xóa toàn bộ folder `history_demo/` hoặc set `QA_INCLUDE_DEMO=0`.
