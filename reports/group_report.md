# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Mọi số dưới đây lấy từ file output do script sinh ra (`out/poc/`, `out/baseline/`).

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | G03 |
| Challenge | T06 · Dataset Semantic Integrity & Provenance Gate |
| Thành viên (5–6) + vai trò | 1. Pham Tien Dung (Lead / Integrator, Core algorithm, Review + report) |
| Git URL | https://github.com/lamsondongbua/K5-Day6-L2-PhamTienDung-2A202603061-DataGovernanceAndPrivacy |
| Tag `demo-180m` (commit) | `2a06c72` |
| Tag `final-24h` (commit) | `2a06c72` |
| Deadline (giờ VN) | ____ |

## 01 · Problem statement

- **Ai bị ảnh hưởng khi hệ thống sai?** Đội huấn luyện model (train trên nhãn sai), đội đánh giá (số liệu sai nhưng trông đúng) và người dùng cuối của model. Một release dataset hỏng im lặng, ví dụ class `car` và `pedestrian` bị đảo, tạo ra model nhận nhầm người đi bộ thành xe mà không ai phát hiện từ file.
- **Điểm nghẽn cụ thể:** các lỗi "im lặng" (JSON vẫn hợp lệ) vượt qua kiểm tra schema và kiểm tra kích thước file; con người không thể duyệt tay từng bbox của 700 annotation.
- **Đầu vào → đầu ra:** parent release `v1.0` + 20 release ứng viên (`annotations.json`, `release_meta.json`) → `release_decisions.csv` (`release_id`, `decision` ∈ PASS/HOLD/REWORK, `root_cause`) + `review_queue.csv` + `rework_report.md` + `decision.json`.
- **Tiêu chí GO / HOLD / REWORK (chốt trước khi chạy eval):**
  - PASS: mọi invariant đều thỏa (thay đổi lành tính như thêm 1 frame hoặc jitter vài box được phép).
  - HOLD: lỗi ngữ nghĩa làm hỏng nhãn hoặc nguồn gốc (`class_swap`, `stale_map`, `id_collision`). Phải điều tra trước khi đi tiếp.
  - REWORK: lỗi dữ liệu có thể làm lại (`bbox_shift`, `dropped_frames`). Trả lại vendor.
- **Giả định của nhóm (cần DPO / legal / domain owner xác nhận):** parent `v1.0` được coi là chuẩn (không kiểm định lại); label map khai báo trong `release_meta.json` là nguồn sự thật; ngưỡng 5% box lệch và ≥ 3 frame thiếu phản ánh đúng mức "đáng chặn" của chủ sở hữu dữ liệu. Tình huống mô phỏng, không phải tư vấn pháp lý.

## 02 · Data & provenance

| Split | N | Nguồn / link | License | Synthetic? |
|---|---|---|---|---|
| dev | 10 release (r01–r10) | `fixtures/t06_release_gate/` (`tools/make_fixtures.py`, seed 42) | CC0-1.0 | có |
| eval / heldout | 10 release (r11–r20) | `fixtures/t06_release_gate/` | CC0-1.0 | có |

- **Schema & class mapping:** COCO (`images`, `annotations`, `categories`) + `release_meta.json`. Label map `lm-3`: 1 car, 2 pedestrian, 3 motorbike, 4 sign, 5 truck (`lm-2` cũ không có truck). Parent: 100 ảnh, 697 annotation.
- **Seed:** 42 · Cách chia split: theo release (mỗi release thuộc đúng một split), mỗi loại lỗi có 2 release.
- **`manifest.csv` verified (sha256):** có; `tools.preflight` báo 0 FAIL. Lệnh: `python -m tools.verify_manifests`. Lưu ý trên Windows phải để `core.autocrlf=false`, nếu không file bị đổi CRLF và hash sai.
- **Dữ liệu thật:** không dùng (`data/real/` không có).

## 03 · Research & design

- **Tham chiếu và lý do chọn:** *(điền URL + ngày xem)*. Hướng dùng: invariant-based validation (kiểu Great Expectations: id duy nhất, category set so với label map, bbox trong ảnh, `frame_index` liên tục, parent sha256). Chọn invariants thủ công thay vì ML vì lỗi có cấu trúc rõ, N nhỏ (20 release), và cần giải thích được cho người ký.
- **Sơ đồ data flow:** `releases.csv` + parent → `load` (kiểm manifest sha256, đọc parent, label map) → `process` (7 invariant / release) → `rank` (top-B theo `score`, contact sheet xanh = box parent, đỏ = box con) → `review` (người duyệt `approve/edit/reject/escalate`, audit log) → `decide` (`decision.json`, `rework_report.md`).
- **Thành phần offline:** không dùng model hay trọng số. Chỉ numpy/pandas/PIL, chạy hoàn toàn offline.
- **Trade-offs:** (1) tín hiệu "file_name ảnh trùng" bị bỏ vì release lành tính cũng tái dùng file cho frame thêm vào (xem Failure case 2); (2) ngưỡng 5% đặt xa mức lành tính (< 1%) để hạn chế chặn nhầm, đổi lại có thể bỏ lọt bbox_shift nhẹ; (3) chọn HOLD cho lỗi ngữ nghĩa, REWORK cho lỗi sửa được.

## 04 · Baseline vs PoC

- **Baseline** (`src/baseline_t06.py`): chỉ parse JSON, kiểm key bắt buộc và file ảnh tồn tại; không so semantic invariants. Kết quả: cả 20 release PASS (`out/baseline/release_decisions.csv`).
- **PoC** (`src/pipeline.py`): 5 nhóm invariant so với parent (Mục 03). Kết quả: 10 release bị chặn, 10 PASS (`out/poc/release_decisions.csv`): HOLD r01, r07, r08, r09, r18, r20; REWORK r02, r05, r10, r16.
- **Cùng budget / cùng heldout / cùng seed:** xác nhận, cùng pool 20 release, cùng split, `--seed 42`, `--offline`.

**Bảng 1 · Baseline vs PoC & hard slices**
| Metric (unit) | Baseline | PoC | Δ & chú thích |
|---|---|---|---|
| Runtime / 100 items | 0.09 s / 20 release | 0.45 s / 20 release | ≈ 2.2 s / 100 release; CPU AMD64 (AMD, Family 23), Windows 11 |

## 05 · Evaluation protocol

- **Threshold / budget B / K và lúc chốt:** các ngưỡng nằm trong `DEFAULTS` của `src/pipeline.py`; B = 10; chốt 2026-11-14.
- **Độ bất định khi N nhỏ:** N = 10 release eval, mỗi release chiếm 10 điểm phần trăm. Không kết luận từ chênh lệch nhỏ.
- **Lệnh evaluate chính xác:**
  ```
  python -m src.evaluate --track t06 --pred out/baseline/release_decisions.csv --run-name baseline --out reports/metrics_baseline.json
  python -m src.evaluate --track t06 --pred out/poc/release_decisions.csv --run-name poc --out reports/metrics_poc.json
  python -m src.compare reports/metrics_baseline.json reports/metrics_poc.json --out reports/table1_baseline_vs_poc.md
  ```

## 06 · Result slices

| Slice (root cause) | N | Baseline | PoC | Ghi chú |
|---|---|---|---|---|
| class_swap | 2 (r07 dev, r20 eval) | PASS cả 2 | HOLD cả 2 | JSON hợp lệ, id↔name đảo |
| stale_map | 2 (r01, r09, đều dev) | PASS | HOLD | thiếu class `truck`, nhưng meta ghi `lm-3` |
| id_collision | 2 (r08 dev, r18 eval) | PASS | HOLD | 12 và 11 annotation id trùng |
| bbox_shift | 2 (r05, r10, đều dev) | PASS | REWORK | ≈ 29% box lệch ~ (20, 10) px |
| dropped_frames | 2 (r02 dev, r16 eval) | PASS | REWORK | thiếu 9 và 7 frame; `frame_index` đứt quãng |
| release lành tính (thêm 1 frame) | 6 (r03, r04, r06, r11, r15, r19) | PASS | PASS | không bị chặn nhầm |
| release sạch giống hệt parent | 4 (r12, r13, r14, r17) | PASS | PASS | |

Kết quả slice của PoC ở trên là hành vi quan sát được trên đầu ra của pipeline.

- **Failure case 1 (bắt buộc): JSON hợp lệ nhưng class mapping đã đảo (r07 và r20).**
  - *Hiện tượng:* `annotations.json` của r07 (dev) và r20 (eval) parse bình thường, đủ 5 category, đủ ảnh, kích thước và bbox giữ nguyên. Nhưng khối `categories` ghi `{id 2: "car", id 1: "pedestrian"}`, ngược với `lm-3` (`1 = car`, `2 = pedestrian`). Các annotation vẫn giữ `category_id` cũ, nên mọi box từng là `car` giờ mang nhãn `pedestrian` và ngược lại.
  - *Bằng chứng:* PoC báo `id->name differs from lm-3: {1: 'pedestrian (expected car)', 2: 'car (expected pedestrian)'}` và `453/697 matched boxes changed class name vs parent` (≈ 65% box đổi tên class); frame minh họa: `images/frame_042.jpg` (r07), `images/frame_084.jpg` (r20) trong contact sheet. Số lượng class theo id không đổi (256 / 197 / 100 / 100 / 44), nên kiểm tra phân bố class đếm theo id không bắt được lỗi này.
  - *Vì sao baseline bỏ lọt:* baseline chỉ kiểm schema và file tồn tại; một kiểm tra "kích thước file / số annotation" cũng không thấy gì vì tổng số annotation và size không đổi.
  - *Root cause:* label map bị đảo ở bước export (vendor hoặc script đổi thứ tự `categories` mà không đổi `category_id` trong annotation). Cách chặn: so `categories` với label map khai báo trong `release_meta.json`, và so tên class của box với box cùng vị trí ở parent.
  - *Hành động:* HOLD (`reason_code` = `SILENT_DEFECTS_BLOCKED`), chủ sở hữu điều tra nguồn export trước khi release.
- **Failure case 2 (lỗi do chính PoC, đã sửa): chặn nhầm release lành tính.** Phiên bản đầu coi "trùng `file_name` ảnh" là `id_collision`, nên chặn nhầm 6 release lành tính (r03, r04, r06, r11, r15, r19) vì frame được thêm vào tái dùng `frame_000.jpg`. Phát hiện khi đọc đầu ra và đối chiếu với thay đổi lành tính; đã bỏ tín hiệu này và thêm test (`tests/test_t06_pipeline.py`: release có thay đổi lành tính phải PASS).
- **Latency / compute:** PoC 0.45 s cho 20 release (~ 22 ms/release), baseline 0.09 s; CPU AMD64 Family 23, Windows 11, Python 3.13.1.

## 07 · Human & decision

- **Review queue:** `out/poc/review_queue.csv` và `out/poc/contact_sheet.html`, B = 10 release xếp theo `score` (tổng trọng số các invariant vi phạm + độ lệch liên tục so với parent). Mỗi thẻ hiển thị frame bằng chứng với box parent (xanh) và box con (đỏ), nguyên nhân và hành động đề xuất.
- **Cách override + audit log:** người duyệt điền `reviewer`, `reviewer_decision` (`approve/skip` giữ quyết định; `escalate` ép HOLD; `edit/reject` cần `override_value` ∈ PASS/HOLD/REWORK) vào `out/poc/review_queue_reviewed.csv`, rồi chạy lại PoC. Mọi thay đổi ghi vào `out/poc/audit_log.jsonl` (ai, khi nào, trước/sau, lý do).
- **Kết quả review thật:** Phạm Tiến Dũng review cả 10 release trong hàng đợi (B = 10) vào 2026-11-14, bằng contact sheet và frame bằng chứng. Kết quả: 10/10 `approve`, 0 `edit/reject/escalate`, tức tỉ lệ override = 0%. Các quyết định tự động được giữ nguyên: HOLD r07, r20, r08, r18, r01, r09; REWORK r10, r05, r02, r16. Bằng chứng: `out/poc/review_queue_reviewed.csv` (10 dòng, có `reviewer` và `reviewed_at`) và `out/poc/audit_log.jsonl` (10 sự kiện `review:approve`).
- **Decision policy + `reason_code` + owner:** `decision.json`: HOLD / `SILENT_DEFECTS_BLOCKED` (nếu còn release bị chặn), GO / `ALL_INVARIANTS_HOLD` (nếu không); owner = Pham Tien Dung - reviewer; kèm `rework_report.md` liệt kê release, nguyên nhân và frame bằng chứng.
- **Khi nào HOLD/REWORK thay vì tự động release:** bất kỳ invariant nào vi phạm. File không đọc được cũng HOLD (không bao giờ PASS khi lỗi). Không bao giờ release tự động khi chưa có chữ ký owner.

## 08 · Reproduce & tests

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
python -m src.main --track t06 --pipeline src.baseline_t06 --output out/baseline --run-name baseline --offline --seed 42
python -m src.main --config configs/lab.yaml --track t06 --output out/poc --run-name poc --offline --seed 42
python -m src.evaluate --track t06 --pred out/poc/release_decisions.csv --out reports/metrics_poc.json
pytest -q
```

- **Output của `pytest -q`:** 54 passed, 39 skipped, 0 failed (39 test bị bỏ); gồm 7 test của nhóm trong `tests/test_t06_pipeline.py` (1 thành công, 5 lỗi theo từng loại, 1 trường hợp biên).
- **Máy:** Windows 11 · AMD64 (AMD Family 23) · Python 3.13.1 · git sha (tag `final-24h`) `2a06c72`.

## 09 · Limitations & ethics

- **Rủi ro pháp lý / PII / license:** dữ liệu hoàn toàn synthetic (CC0-1.0), không có người hay biển số thật. PoC không xử lý PII.
- **Điều chưa biết / false alarms / scale 10×:** (1) ngưỡng được đặt khi đã xem thống kê đầu vào của cả dev và eval (không có GT), nên có rủi ro quá khớp; (2) chỉ có 10 release eval nên kết quả rất thô; (3) PoC giả định parent đúng và các lỗi dạng có cấu trúc; lỗi tinh vi hơn (ví dụ xoay nhẹ một số ít class, bbox lệch < 5% box) sẽ lọt; (4) khi scale 10×, so khớp IoU theo frame sẽ cần chỉ mục không gian thay vì quét tuyến tính; (5) release có thay đổi lành tính lớn hơn mức quan sát (nhiều frame mới) có thể bị chặn nhầm.
- **Không tuyên bố "đạt GDPR / luật Việt Nam"** từ checklist tự động; mọi giả định về quy trình release và chủ sở hữu cần domain owner / DPO xác nhận.
- **Kế hoạch cải tiến:** thêm so sánh phân bố class và kích thước bbox có kiểm định thống kê; round-trip COCO ↔ YOLO và đối chiếu `yolo_labels/`; kiểm tra hash từng file ảnh; xuất metadata Croissant (bonus); thêm vòng review thật để đo tỉ lệ override.

## 10 · Team contribution

**Bảng 2 · Individual contribution evidence** *(đối chiếu `git shortlog -sne --all` và `reports/team_contributions.csv`)*

| Học viên | Git author/email | Module / vai trò | Commit SHA / diff chứng minh | Trình bày? |
|---|---|---|---|---|
| Pham Tien Dung | lamsondongbua <dungxoi18072004@gmail.com> | `src/pipeline.py`, `src/baseline_t06.py`, `tests/test_t06_pipeline.py`, reports | `cdeb071` (PoC, baseline, tests, report), `3ca979b` (report: SHA, kết quả review) | Có |

## 11 · References

| URL | Ngày xem | 1–2 dòng ý nghĩa (không chép nguyên văn) |
|---|---|---|
| `docs/tracks/T06.md` (repo lab) | 2026-11-14 | Đề bài, output contract, gợi ý invariants. |

---

**Bảng 3 · Checklist chốt khi nộp**

- [ ] Link repo và tag cuối cùng trong README; input mẫu không chứa PII thật.
- [ ] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [ ] Có baseline/PoC metrics, ≥1 failure case, reviewer evidence, decision log.
- [ ] Có license/source provenance, data splits, seed, offline environment, GT/eval N.
- [ ] Mỗi thành viên có ≥1 commit thực chất và được nêu minh bạch trong bảng trên.
