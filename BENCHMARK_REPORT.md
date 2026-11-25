# Benchmark & Observability Report — Day 14

**Họ tên:** Phan Ba An
**Ngày chạy:** 2026-11-25 (giờ UTC trong log: 12:58 → 13:20)
**Môi trường:** Laptop Windows 11 Home, CPU AMD Ryzen 12 luồng (AMD64 Family 25 Model 116), Docker Desktop 29.9.3 (12 CPU, ~15 GB RAM cho Docker). Part 7 develop chạy bằng Python 3.12 (venv, đúng version trong `requirements.txt`); load generator chạy cùng máy với agent.

Nguồn số liệu: `reports/*.json` (load test + eval), `benchmark-results/scale_*_stats.csv` (Locust), Prometheus API (`/api/v1/query`, `/api/v1/alerts`) và Jaeger API (`/api/v3/traces`) trong lúc stack chạy.

---

## 1. SLO của agent

| SLI | SLO | Lý do chọn ngưỡng này |
|---|---|---|
| p95 latency `/ask` | < 500 ms | Mock LLM tốn 120–180 ms + retrieval ~20 ms + thêm theo độ dài prompt → request bình thường ~200 ms. Ở tải thấp (c ≤ 10) p95 đo được 235–286 ms, nên 500 ms chừa ~2× headroom cho jitter. Trùng ngưỡng mặc định của `load_test.py` (`--slo-p95-ms 500`). Alert production đặt lỏng hơn (p95 > 1 s trong 2 phút) để tránh báo động giả. |
| Error rate `/ask` | < 2 % | Upstream LLM giả lập lỗi 1 % (`MOCK_ERROR_RATE=0.01`), đo được 0.67–1.33 %. Ngưỡng 2 % cho phép đúng mức lỗi nền của provider, vượt mức đó là có sự cố thật. Alert critical đặt ở 5 % / 1 phút. |
| Accuracy trên golden dataset | ≥ 85 % (CI gate), mục tiêu 100 % | 85 % là gate trong CI (`.github/workflows/quality-gate.yml`) và luôn kèm điều kiện **không regression** so với baseline. Bản v2 sau khi sửa KB đạt 23/23. |

---

## 2. Benchmark hiệu năng (develop, `load_test.py`)

### 2.1 Concurrency vs latency (agent v2)

300 request mỗi mức, `MOCK_SLOW_RATE=0.03` (mặc định). File: `reports/load_v2_c{1,10,50,100}.json`.

| Concurrency | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | SLO |
|---|---|---|---|---|---|---|
| 1 | 4.2 | 203.1 | 235.1 | 1707.1 | 1.33 % | ✅ PASS |
| 10 | 36.94 | 201.8 | 286.0 | 1698.9 | 1.33 % | ✅ PASS |
| 50 | 105.54 | 203.3 | 519.9 | 1686.2 | 0.67 % | ❌ FAIL (p95) |
| 100 | 71.34 | 521.6 | 2523.6 | 2718.9 | 1.33 % | ❌ FAIL (p95) |

**Nhận xét:** RPS chỉ tăng gần tuyến tính ở tải thấp. Theo Little's law, RPS ≈ concurrency / latency: c=1 → 1/0.24 s ≈ 4.2 (khớp), c=10 → lý thuyết ~43, đo được 36.9. Từ c=50 bắt đầu bão hoà: lý thuyết ~180 req/s nhưng chỉ đạt 105.5, p95 vượt 500 ms. Ở c=100 RPS còn **giảm** xuống 71.3 và p50 tăng từ 203 lên 522 ms. Mock LLM là `asyncio.sleep` nên không phải LLM nghẽn. Nghẽn nằm ở **một process uvicorn duy nhất** (1 event loop, 1 core) phải lo parse JSON, validate pydantic, tính metrics và ghi log cho mọi request. Thêm vào đó, client load test chạy cùng máy để giành CPU. Request phải xếp hàng trong event loop nên latency tăng, còn throughput giảm vì phải chuyển ngữ cảnh liên tục. Muốn tăng RPS phải scale ngang (nhiều worker/container), không thể chỉ tăng concurrency.

### 2.2 Tail latency

`--requests 300 --concurrency 20`, khởi động lại v2 với từng giá trị. File: `reports/load_v2_slow_{0,0.03,0.1}.json`.

| `MOCK_SLOW_RATE` | avg (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| 0 | 202.0 | 427.9 | 488.1 |
| 0.03 (mặc định) | 235.7 | 475.5 | 1700.0 |
| 0.1 | 358.5 | 1687.7 | 1735.8 |

**Nhận xét:** Khi slow rate tăng 0 → 0.03, avg chỉ tăng 17 % (202 → 236 ms) nhưng **p99 tăng 3,5 lần** (488 → 1700 ms). Lý do: 3 % request chậm thêm 1,5 s nằm trọn trong top 1 % nên kéo p99 lên đúng mức "chậm", còn avg bị 97 % request nhanh pha loãng. Lên 0.1 thì 10 % > 5 % nên **p95** cũng nhảy lên 1688 ms và SLO p95 fail, trong khi avg vẫn chỉ 358 ms, trông như "ổn". Chỉ số nhạy nhất là percentile cao (p99, rồi p95). SLO không nên đặt theo average vì average che mất đúng nhóm người dùng bị ảnh hưởng: 1 trên 10 người chờ gần 2 s mà avg vẫn dưới 400 ms. SLO đặt theo p95/p99 mới phản ánh trải nghiệm của người dùng tệ nhất.

---

## 3. Benchmark chất lượng (develop, `eval/run_eval.py`)

Golden dataset 23 câu (`eval/golden_dataset.jsonl`, không sửa). File: `reports/eval_v1.json`, `reports/eval_v2.json`, `reports/eval_v2_fixed.json`.

| Chỉ số | Agent v1 | Agent v2 | Agent v2 (sau khi sửa KB) |
|---|---|---|---|
| Accuracy | 4.35 % (1/23) | 91.3 % (21/23) | 100 % (23/23) |
| Avg tokens / câu | 55.9 | 109.0 | 117.0 |
| Tổng cost (USD) | 0.000388 | 0.000760 | 0.000819 |
| Cost / câu đúng (USD) | 0.00038775 | 0.00003619 | 0.00003560 |
| Quality gate | FAIL (gate 80 %) | PASS (gate 80 %, 0 regression so với v1) | PASS (gate 100 %, 0 regression so với v2) |

**Những câu v2 trả lời sai ban đầu và cách bạn sửa:**

| ID | Keyword bị thiếu | Bạn đã sửa gì |
|---|---|---|
| security-02 ("JWT khác gì API key…") | `token`, `hết hạn`, `chữ ký` | Không có tài liệu nào khớp nên v2 trả câu generic. Thêm vào `KNOWLEDGE_BASE` entry `["jwt", "json web token"]`: so sánh API key (chuỗi cố định, không tự hết hạn) với JWT (token có chữ ký số, mang `exp` hết hạn, server chỉ verify chữ ký). |
| scaling-03 ("Kubernetes HPA scale dựa trên…") | `cpu`, `memory`, `custom metrics` | Thêm entry `["hpa", "horizontal pod autoscaler", "autoscal"]`: HPA scale theo CPU, memory hoặc custom metrics (RPS, queue length). |

Keyword được chọn hẹp. Trước khi sửa tôi đã grep golden dataset: chỉ đúng 2 câu này chứa `jwt`/`hpa`, nên retrieval của 21 câu còn lại không đổi (kết quả: 0 regression). Đã chép cùng thay đổi sang `production/utils/mock_llm.py`.

**Nhận xét về trade-off chất lượng / chi phí / latency giữa v1 và v2:**

- v2 tốn gần **gấp đôi token mỗi câu** (55.9 → 109.0) vì prompt kèm tài liệu retrieve được, nên tổng cost cũng gần gấp đôi (0.000388 → 0.000760 USD). Latency p95 tăng nhẹ (188.5 → 217.6 ms) do có thêm bước retrieval (~20 ms) và prompt dài hơn.
- Nhưng tính theo **cost / câu đúng**, v2 rẻ hơn v1 khoảng **10,7 lần** (0.000388 → 0.0000362 USD). v1 rẻ mỗi câu nhưng gần như toàn sai, nên tiền bỏ ra gần như phí. Vì vậy khi so hai phiên bản, "cost / câu đúng" là chỉ số đúng; "cost / câu" sẽ khuyến khích chọn nhầm bản rẻ mà vô dụng.
- Sửa KB thêm 8 token/câu (+7,8 % cost) để có thêm 2 câu đúng; cost / câu đúng vẫn giảm (0.0000362 → 0.0000356).
- p95 của lần eval cuối (1566 ms) cao hơn hẳn là do ngẫu nhiên: 2/23 request rơi vào nhóm `MOCK_SLOW_RATE=3%` (mỗi request +1.5 s). Với 23 mẫu thì p95 ≈ request chậm thứ 2, nên đây chỉ là nhiễu thống kê, không do KB. Eval nên dùng accuracy làm gate, không dùng latency.

**Chặn merge bằng exit code:** `run_eval.py` `sys.exit(1)` khi accuracy < `--min-accuracy` hoặc có câu regression so với `--baseline`. Job CI `.github/workflows/quality-gate.yml` chạy agent v2 (`MOCK_ERROR_RATE=0` để kết quả ổn định), sau đó chạy `run_eval.py --min-accuracy 0.85 --baseline reports/eval_v2_fixed.json`. Exit code ≠ 0 làm step fail, job fail, check của PR đỏ. Bật branch protection "Require status checks to pass" thì PR không merge được.

---

## 4. Observability stack (production)

Stack: `docker compose -p k5-day14-observability`, 2 agent (mặc định), Locust headless 30 users / spawn 5 chạy nền suốt bài.

> Ghi chú: `production/Dockerfile` của đề có lỗi làm container agent crash-loop (`ModuleNotFoundError: uvicorn`). User `agent` được tạo với `-d /app` nên `$HOME/.local` = `/app/.local`, trong khi thư viện lại được copy vào `/home/agent/.local`. Tôi sửa bằng cách thêm `ENV PYTHONUSERBASE=/home/agent/.local`.

### 4.1 Screenshot

- [x] Dashboard Grafana khi đang có tải (thấy rõ Golden Signals): [screenshots/grafana-dashboard.png](screenshots/grafana-dashboard.png). Chụp lúc 13:12 UTC, đang chạy thí nghiệm A+B: Error rate 20.38 %, p95 1.89 s, Instances up 2.
- [x] Một trace chậm trong Jaeger: [screenshots/jaeger-slow-trace.png](screenshots/jaeger-slow-trace.png)
- [x] Một trace lỗi trong Jaeger (có event `exception`): [screenshots/jaeger-error-trace.png](screenshots/jaeger-error-trace.png)
- [x] Trang Alerts của Prometheus với 2 alert cùng **firing** (`AgentHighErrorRate` + `AgentHighLatencyP95`): [screenshots/prometheus-alert-firing.png](screenshots/prometheus-alert-firing.png)

**Exercise 7.4 (đo bằng PromQL lúc 30 users, cấu hình mặc định, 2 instance):**

1. p95 = **294 ms**, avg = **236 ms**, p50 = 170 ms. p95 chỉ cao hơn avg ~58 ms nghĩa là phân bố khá gọn, tail ngắn: phần lớn request mất 150–250 ms, chỉ ~3 % là request chậm 1,7 s. Request chậm kéo avg lên trên p50 (236 so với 170), nhưng chưa đủ nhiều để đẩy p95 lên. Khi chạy thí nghiệm B, khoảng cách này giãn ra: avg 0.55 s, còn p95 1.89 s.
2. Panel "Requests/s theo instance": **9.2 req/s mỗi instance** (172.19.0.5 và 172.19.0.8), Nginx chia đều theo round-robin.
3. Input tokens/s = **1432**, output tokens/s = **759**. Input lớn hơn vì prompt = system prompt + tài liệu retrieve được (v2) + câu hỏi, còn câu trả lời chỉ là một đoạn tài liệu ngắn. Đây cũng là lý do v2 đắt hơn v1.
4. `curl http://localhost:8080/metrics` trả **HTTP 404**. Nginx chặn `location = /metrics` vì metrics chỉ dành cho Prometheus scrape trực tiếp `agent:8000` trong mạng nội bộ Docker. Lộ `/metrics` ra ngoài sẽ làm lộ thông tin nội bộ (route, model, chi phí, số instance) và tạo thêm bề mặt để tấn công.

### 4.2 Thí nghiệm "phá hệ thống"

Thời gian đo bằng script poll `GET /api/v1/alerts` mỗi 2 s. T0 = lúc `docker compose up -d` tạo lại xong container agent.

| # | Thay đổi | Alert nào firing | Thời gian từ lúc đổi → firing | Panel nào phản ánh rõ nhất |
|---|---|---|---|---|
| A | `MOCK_ERROR_RATE=0.2` | `AgentHighErrorRate` (giá trị 20.5 %) | pending **22 s**, firing **82 s** (= pending + `for: 1m`) | Error rate (stat đỏ 20.38 %), "Requests/s theo status code" (đường 503), "LLM calls theo kết quả" |
| B | `MOCK_SLOW_RATE=0.3` (giữ error 0.2) | `AgentHighLatencyP95` (p95 1.89 s) | pending **16 s**, firing **136 s** (`for: 2m`) | "Latency /ask — p50/p95/p99": p95 1.89 s, p99 1.98 s nhưng avg chỉ 0.55 s, p50 không đổi |
| C | `LLM_MODEL=mock-llm-large` | `LLMCostBurnRateHigh` ($16.3/giờ theo cửa sổ 5m) | pending **18 s**, firing **138 s** | "Cost burn rate (USD/giờ)": cửa sổ 1m đạt $35.9/giờ so với ~$2/giờ lúc bình thường (≈16×); p50 tăng 0.17 → 0.41 s |
| D | `docker pause k5-day14-observability-agent-1` | `AgentInstanceDown` (`up{instance="172.19.0.5:8000"} == 0`) | pending **9 s**, firing **39 s** (`for: 30s`) | "Instances up" giảm 2 → 1. Nginx log ghi **28 lỗi 504** trong 2 phút nhưng error rate của agent vẫn 0.56 % (xanh) |
| E | `AGENT_VERSION=v1` | **Không có** (theo dõi 180 s) | — | Retrieval hit ratio mất dữ liệu (v1 không retrieve), tokens/s giảm (input 1129 → 679/s, output 598 → 369/s), bảng "Phiên bản đang chạy" đổi sang v1; error rate 1.06 %, p95 0.20 s vẫn xanh |

Ghi chú khi phân tích:
- `LLMCostBurnRateHigh` **đã firing ngay từ cấu hình mặc định** trong lúc benchmark 100 users (~66 req/s, ~$5/giờ > ngưỡng $2/giờ), tức là ngưỡng tuyệt đối theo $/giờ phụ thuộc vào traffic. Trong thí nghiệm E alert này vẫn firing nhưng chỉ là phần còn sót của C, vì cửa sổ `rate(...[5m])` còn chứa chi phí model large (giá trị giảm dần 17.6 → 6.1). E không gây alert mới. Cải tiến: alert theo cost / request, hoặc theo % ngân sách ngày đã tiêu.
- Thời gian firing ≈ thời gian chờ để `rate()` vượt ngưỡng (16–22 s, do scrape 5 s cộng cửa sổ 1 m) cộng với `for`. `for` giúp tránh báo động vì spike ngắn nhưng làm chậm phát hiện.
- D cho thấy metric của agent chỉ thấy những gì agent nhận được. Request kẹt ở Nginx (504 timeout) không bao giờ được agent đếm, nên cần thêm metric/alert ở tầng load balancer hoặc synthetic check từ bên ngoài.

**Thí nghiệm E không làm alert nào firing. Bạn sẽ phát hiện loại sự cố này trong production bằng cách nào?**

Lỗi chất lượng không làm metric hạ tầng (latency, error, up) đổi màu. Phải đo chất lượng trực tiếp:
1. **Chặn trước khi deploy**: quality gate eval trên golden dataset trong CI. v1 chỉ đạt 4.35 % nên gate fail và không thể merge hoặc deploy.
2. **Eval liên tục trong production**: định kỳ (vd 15 phút) chạy golden dataset vào endpoint thật, xuất metric `agent_eval_accuracy` rồi đặt alert khi < 85 %. Có thể chấm thêm một mẫu traffic thật bằng LLM-as-judge.
3. **Proxy metric có alert**: retrieval hit ratio giảm đột ngột (từ ~100 % về "không có dữ liệu"), tokens/s giảm ~40 % khi RPS không đổi, `agent_info{version}` đổi ngoài lịch deploy.
4. **Tín hiệu từ người dùng**: nút 👍/👎, tỉ lệ hỏi lại hoặc rephrase.

### 4.3 Debug bằng trace

Trace chậm nhất trong 200 trace > 1 s (truy vấn Jaeger API, sắp xếp theo duration):

- `trace_id`: `a9eff9537574254fc13b0b56c79cc95b`
- Tổng thời gian: **1727.5 ms** (`POST /ask`, HTTP 200)
- Span chiếm nhiều thời gian nhất: **`chat mock-llm-small` (1668.5 ms, 96.6 %)**, gồm `gen_ai.request.model=mock-llm-small`, `input_tokens=81`, `output_tokens=46`. Span `agent.retrieve_context` chỉ 19.3 ms (1.1 %); phần còn lại (~40 ms) là overhead HTTP/agent. Kết luận: chậm do upstream LLM (request rơi vào nhóm `MOCK_SLOW`), không phải do code của agent.
- Dòng log tương ứng (`docker compose -p k5-day14-observability logs agent | grep a9eff953...`):

```json
{"ts": "2026-11-25T13:12:47.957585+00:00", "level": "INFO", "service": "observable-agent", "instance": "f5e78fd47927", "event": "llm_call", "trace_id": "a9eff9537574254fc13b0b56c79cc95b", "span_id": "8197e14f6adcd816", "request_id": "3a90b6e1388b", "model": "mock-llm-small", "agent_version": "v2", "input_tokens": 81, "output_tokens": 46, "cost_usd": 4e-05, "llm_ms": 1668.8}
{"ts": "2026-11-25T13:12:47.957989+00:00", "level": "INFO", "service": "observable-agent", "instance": "f5e78fd47927", "event": "http_request", "trace_id": "a9eff9537574254fc13b0b56c79cc95b", "span_id": "8197e14f6adcd816", "request_id": "3a90b6e1388b", "method": "POST", "route": "/ask", "status": 200, "ms": 1727.0}
```

Trace lỗi (ảnh `jaeger-error-trace.png`): `trace_id` `246da79df87b77891c352f30d21dab65`, 115.5 ms, HTTP 503. Span `chat mock-llm-small` (94.1 ms) và `agent.run` có status ERROR, event `exception` với `exception.type=utils.mock_llm.LLMError`, `exception.message="Upstream LLM error: 503 Service Unavailable (mock)"`. Log khớp:

```json
{"ts": "2026-11-25T13:13:08.264902+00:00", "level": "INFO", "service": "observable-agent", "instance": "f5e78fd47927", "event": "llm_error", "trace_id": "246da79df87b77891c352f30d21dab65", "span_id": "5cccb84dc84c0ab8", "request_id": "93eea96678fd", "error": "Upstream LLM error: 503 Service Unavailable (mock)"}
```

---

## 5. Benchmark scaling (production, Locust headless, 100 users, 2 phút)

`-u 100 -r 10 -t 2m`, cấu hình mặc định. File: `benchmark-results/scale_1_stats.csv`, `benchmark-results/scale_3_stats.csv` (số liệu của dòng `POST /ask`; RPS tổng gồm cả `/health` ghi trong ngoặc).

| Số instance | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---|---|---|---|---|---|
| 1 | 60.43 (66.29) | 190 | 220 | 1700 | 70 / 7215 (0.97 %, đều là 503 từ mock LLM) |
| 3 | 60.32 (66.21) | 190 | 220 | 1700 | 68 / 7203 (0.94 %, đều là 503 từ mock LLM) |

**Giải thích kết quả:** Scale 1 → 3 **không cải thiện** gì cả: RPS, p50, p95, p99 gần như trùng nhau. Có hai lý do:

1. **Throughput bị giới hạn ở phía client, không phải server.** Locust là closed workload: 100 user, mỗi user nghỉ `between(0.5, 2)` s (trung bình 1.25 s) giữa các lần gọi. Throughput tối đa ≈ 100 / (1.25 + 0.21) ≈ **68 req/s**, khớp với 66.3 req/s đo được. Một instance đã phục vụ hết mức tải này nên thêm instance không có thêm việc để làm.
2. **Latency nằm ở upstream LLM.** Trace ở mục 4.3 cho thấy span LLM chiếm ~97 % thời gian. p50 190 ms ≈ latency mock (120 ms + jitter 0–60 ms + token), và p99 1700 ms = request `MOCK_SLOW` (+1500 ms). Scale ngang agent không làm LLM nhanh hơn. Mock dùng `asyncio.sleep` nên một event loop chờ song song được nhiều request mà không tốn CPU; instance chưa bão hoà (in-flight ~10).

Nút thắt nằm ở **latency của LLM** (cùng lắm là quota hay rate limit của provider), không phải ở số instance agent. Scale chỉ có ích khi agent bão hoà CPU hoặc kết nối, như ở mục 2.1 với c=100 trên 1 process. Muốn giảm p95/p99 phải tác động vào LLM: cache câu trả lời, model nhỏ hoặc nhanh hơn, streaming, timeout + retry/hedging cho request chậm.

---

## 6. Kết luận

- 3 điều bạn học được về benchmark & observability:
  1. **Average nói dối, percentile nói thật.** Slow rate 3 % gần như không làm avg đổi (+17 %) nhưng đẩy p99 lên 3,5 lần; 10 % làm vỡ SLO p95 mà avg vẫn "đẹp". SLO và alert phải đặt theo p95/p99.
  2. **Metric hạ tầng không thấy lỗi chất lượng và lỗi ở tầng khác.** Thí nghiệm E (v1, accuracy 4 %) không gây alert nào; thí nghiệm D có 28 lỗi 504 ở Nginx mà error rate agent vẫn xanh. Cần eval/quality gate và giám sát cả từ phía client hoặc load balancer.
  3. **Trace + log có chung `trace_id` rút ngắn thời gian debug.** Từ một dòng log `llm_error` nhảy thẳng tới trace thấy ngay span LLM chiếm 96,6 % thời gian, không cần đoán. Ngoài ra, muốn đọc đúng một benchmark (scale 1 vs 3) phải hiểu mô hình tải: closed workload có think-time sẽ giới hạn RPS từ phía client.
- Nếu đưa agent này lên production thật, bạn sẽ thêm metric / alert nào nữa?
  - **Chất lượng:** `agent_eval_accuracy` từ eval định kỳ (alert < 85 %), `agent_retrieval_total{result="miss"}` ratio, feedback 👎 rate.
  - **Cost:** `llm_cost_per_request` và % ngân sách ngày đã dùng (alert 80 % / 100 %) thay cho ngưỡng $/giờ tuyệt đối, vì ngưỡng tuyệt đối đã báo động giả ở tải 100 users.
  - **Tầng ngoài:** metric 5xx/504 của Nginx (nginx-exporter) và synthetic probe từ bên ngoài (blackbox exporter) gọi `/ask` mỗi phút.
  - **Saturation:** `agent_in_flight_requests` theo instance và CPU throttling của container (alert trước khi p95 vỡ như ở c=100); rate limit/429 từ LLM provider.
  - **SLO burn-rate alert** nhiều cửa sổ (1h/6h) thay cho ngưỡng cố định để giảm báo động giả và vẫn bắt được sự cố chậm.
