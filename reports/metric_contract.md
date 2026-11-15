# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)


| Mục | Khai báo của nhóm |
|---|---|
| Track | T06 · Dataset Semantic Integrity & Provenance Gate |
| Primary metric (tên đúng như evaluator in ra) | **Silent-defect Detection F1** (`eval_t06` trong `src/metrics/tracks.py`) |
| Tăng tốt hay giảm tốt? | higher (F1 càng cao càng tốt) |
| Đơn vị đánh giá (image / region / pair / release / batch / case …) | release (mỗi release ứng viên là một mẫu) |
| Công thức + numerator / denominator | Positive = release có lỗi (`is_defective = 1`). Release bị "chặn" khi `decision` khác GO (HOLD hoặc REWORK; PASS được xem là GO). Precision = TP / (TP + FP), Recall = TP / (TP + FN), F1 = 2·P·R / (P + R). TP = release lỗi bị chặn; FP = release sạch bị chặn; FN = release lỗi nhưng được PASS. |
| Cách ghép prediction ↔ GT (IoU? id? tolerance?) | Ghép theo `release_id` (khóa duy nhất; evaluator từ chối id trùng). Release thiếu trong file dự đoán coi như được PASS (lọt lỗi). Không có IoU hay tolerance ở mức đánh giá; IoU chỉ dùng bên trong pipeline để so box với parent. |
| Candidate pool cố định | 20 release ứng viên `r01`–`r20` (con của parent `v1.0`); tách 10 `dev` (r01–r10) và 10 `eval` (r11–r20) theo `releases.csv`. |
| Review budget B / K | B = 10 release trong `review_queue.csv` (param `budget`, mặc định 10, tức một nửa pool). |
| Seed | 42 |
| Threshold policy (chọn trên dev như thế nào) | Ngưỡng được đặt vào khoảng giữa mức thay đổi lành tính và mức lỗi quan sát được, sau đó **đóng băng** trong `DEFAULTS` của `src/pipeline.py` (không tune lại theo kết quả eval): `iou_match = 0.5`; `shift_frac = 0.05` (> 5% box không còn khớp parent thì là `bbox_shift`; thay đổi lành tính chỉ chạm < 1%); `swap_frac = 0.05`; `dropped_min = 3` (thiếu ≥ 3 frame so với parent thì là `dropped_frames`; thay đổi lành tính chỉ chạm ≤ 1 frame); mọi lỗi `frame_index` đứt quãng đều bị tính. Trùng id annotation/ảnh, `categories` lệch label map khai báo, hoặc `parent_sha256` sai đều bị chặn ngay (invariant cứng, không có ngưỡng). |
| Eval split (không dùng để tune) | `eval` (r11–r20). Chỉ `dev` (r01–r10) được dùng để lập luận về ngưỡng. |
| Chỉ số lỗi xấu (miss / false alarm / false reject) | Miss = release lỗi lọt (FN, nghiêm trọng nhất: release hỏng được đưa ra); false block = release sạch bị chặn (FP, đo bằng false-block rate trên 10 release sạch). |
| Slices sẽ báo cáo (≥2) | (1) theo loại lỗi / `root_cause`: class_swap, stale_map, id_collision, bbox_shift, dropped_frames (mỗi loại 2 release: 1 dev + 1 eval hoặc 2 dev); (2) lỗi "parse được" (class_swap, stale_map, bbox_shift: JSON vẫn hợp lệ) so với lỗi lộ rõ (id_collision, dropped_frames); (3) release sạch có thay đổi lành tính (thêm 1 frame) để đo false block. |
| Cách báo độ bất định khi N nhỏ (Wilson CI / bootstrap) | N = 10 release eval nên mọi tỉ lệ rất thô (mỗi release = 10 điểm phần trăm). Báo số đếm (TP/FP/FN) kèm Wilson 95% CI cho recall và false-block rate; không kết luận khác biệt nhỏ. |
| Ngày giờ chốt + người chốt | 2026-11-14 · Pham Tien Dung (owner trong `configs/lab.yaml`). Ghi chú trung thực: để hiểu dữ liệu, người chốt đã xem thống kê đầu vào của cả `dev` lẫn `eval` (không có GT) trước khi đóng băng ngưỡng; ngưỡng nằm giữa mức lành tính và mức lỗi, không dò sát từng release. |

## Metric phụ (đã cài trong evaluator)

- False-block rate trên release sạch (mục tiêu: 0 trên 10 release sạch).
- Root-cause accuracy so với `corruption_type`. Pipeline xuất cột `root_cause` ∈ {class_swap, bbox_shift, id_collision, dropped_frames, stale_map, clean}.
- Runtime trên 100 mẫu.

## Baseline

Baseline (`src/baseline_t06.py`) chỉ kiểm JSON parse, schema và file ảnh tồn tại. Baseline không chặn release nào (cả 20 PASS), nên **baseline = 0** (Recall = 0, F1 = 0). Khi báo cáo, ghi "baseline = 0" thay vì phần trăm cải thiện.
