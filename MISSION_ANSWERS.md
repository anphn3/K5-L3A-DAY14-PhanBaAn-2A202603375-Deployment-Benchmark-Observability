# Day 14 Lab — Mission Answers

> Mọi output dưới đây lấy từ lệnh đã chạy trên máy tôi ngày 2026-11-25 (Windows 11 + Docker Desktop 29.9.3; phần cần SIGTERM chạy trong WSL2 Ubuntu). Token và API key đã được che.

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns found (`01-localhost-vs-production/develop/app.py`)

1. **Hardcode secret trong code** (dòng 17–18): `OPENAI_API_KEY = "sk-hardcoded-..."` và `DATABASE_URL = "postgresql://admin:password123@..."`. Push lên GitHub là lộ key và mật khẩu DB ngay, và chúng nằm vĩnh viễn trong lịch sử git.
2. **Log lộ secret + dùng `print()`** (dòng 33–38): `print(f"[DEBUG] Using key: {OPENAI_API_KEY}")`. Secret đi thẳng vào log; `print` không có level/timestamp/định dạng nên log aggregator không parse được. Khi chạy, log in ra đúng `[DEBUG] Using key: sk-hardcoded-fake-key-never-do-this`.
3. **Không có health check** (`/health` trả **404** khi tôi gọi): platform không biết app còn sống để restart, load balancer không biết khi nào route traffic.
4. **Bind `host="localhost"`** (dòng 51): trong container, app chỉ nhận kết nối từ chính container, nên từ bên ngoài không gọi được.
5. **Port cố định 8000** (dòng 52): Railway/Render cấp port qua biến `PORT`, cố định port thì platform không route được.
6. **`reload=True` / `DEBUG = True` cố định** (dòng 21, 53): reloader theo dõi file, tốn tài nguyên, chạy thêm process con, không hợp với production; debug mode có thể lộ thông tin lỗi.
7. **Không có config management**: `DEBUG`, `MAX_TOKENS` hardcode, muốn đổi theo môi trường phải sửa code.
8. **Không xử lý graceful shutdown**: không có lifespan/SIGTERM handler, không đóng kết nối, request đang chạy có thể bị cắt.
9. **Không validate input**: `question: str` nhận qua query string, không giới hạn độ dài.

### Exercise 1.2: Chạy bản develop

```text
$ curl -s -i -X POST "http://localhost:8000/ask?question=Hello"
HTTP/1.1 200 OK
{"answer":"Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic."}

$ curl -s -i http://localhost:8000/health
HTTP/1.1 404 Not Found
{"detail":"Not Found"}

$ curl -s -i -X POST http://localhost:8000/ask -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 422 Unprocessable Entity      # bản develop chỉ nhận question qua query
```

Log server khi gọi `/ask`, cho thấy secret bị in ra:

```text
[DEBUG] Got question: Hello
[DEBUG] Using key: sk-hardcoded-fake-key-never-do-this
```

Nó chạy, nhưng chưa production-ready: lộ secret, không health check, không chạy được trong container hay trên cloud.

### Exercise 1.3: Comparison table

Bản production (`production/app.py`) khi chạy: `/health` → 200 `{"status":"ok","uptime_seconds":0.7,...}`, `/ready` → 200 `{"ready":true}`, `/ask` JSON → 200, body thiếu `question` → 422 `{"detail":"question field is required"}`.

| Feature | Develop | Production | Tại sao quan trọng? |
|---------|---------|------------|---------------------|
| Config | Hardcode trong code (`OPENAI_API_KEY`, `DEBUG`, port) | Biến môi trường qua `config.py` (`Settings` + `os.getenv`), có `.env.example`, `validate()` fail-fast nếu thiếu `AGENT_API_KEY` ở production | Cùng một image chạy được ở dev/staging/prod; secret không nằm trong git; đổi cấu hình không cần build lại |
| Secrets | Hardcode + in ra log | Đọc từ env, không log secret (chỉ log `question_length`, `client_ip`) | Lộ key = mất tiền và mất dữ liệu; log thường được nhiều người và hệ thống đọc |
| Health check | Không có (404) | `/health` (liveness, uptime/version) và `/ready` (readiness, 503 khi chưa sẵn sàng) | Platform tự restart container chết; load balancer chỉ route tới instance sẵn sàng |
| Logging | `print()` | Logging có level, định dạng JSON | Tìm kiếm/lọc/alert được trong Loki, Datadog…; có level để lọc nhiễu |
| Shutdown | Đột ngột | Graceful: lifespan đặt `is_ready=False`, chờ request in-flight, handler SIGTERM | Deploy/scale down không cắt ngang request của người dùng |
| Host/Port | `localhost:8000` cố định | `0.0.0.0` + `PORT` từ env | Chạy được trong container và trên cloud (platform cấp PORT) |
| Reload/Debug | `reload=True` luôn bật | Chỉ reload khi `DEBUG=true` | Production không cần file watcher; tránh lộ thông tin debug |
| CORS | Không cấu hình | `ALLOWED_ORIGINS` từ env | Chỉ frontend được phép mới gọi được API từ trình duyệt |

**Phát hiện thêm khi chạy:** bản production **không in ra được dòng log JSON nào**, log chỉ có `WARNING:root:OPENAI_API_KEY not set — using mock LLM` theo format mặc định. Nguyên nhân: `config.py` gọi `settings.validate()` → `logging.warning(...)` **trước** `logging.basicConfig(...)` trong `app.py`. Lần gọi `logging.warning` đầu tiên tự cấu hình root logger (level WARNING, format mặc định), nên lần `basicConfig` sau không còn tác dụng và mọi `logger.info(json.dumps(...))` bị bỏ qua. Cách sửa: cấu hình logging trước khi import `config`, hoặc dùng `basicConfig(..., force=True)`. Ngoài ra format `'{"msg":"%(message)s"}'` bọc chuỗi JSON trong chuỗi, không escape dấu ngoặc kép, nên ra JSON không hợp lệ (thấy rõ ở log container Part 2: `"msg":"{"event": "request", ...}"`). Ở Part 6 tôi đã tự viết `JsonFormatter` dùng `json.dumps` cho cả dòng log.

## Part 2: Docker

### Exercise 2.1: Dockerfile questions (`02-docker/develop/Dockerfile`)

1. **Base image:** `python:3.11`, bản Python đầy đủ (Debian + build tools), rất nặng (~1 GB+).
2. **Working directory:** `/app` (`WORKDIR /app`). Các lệnh `COPY`/`RUN`/`CMD` sau đó chạy tương đối với thư mục này.
3. **Vì sao COPY requirements.txt trước:** để tận dụng **layer cache**. Docker cache từng layer theo nội dung. Copy riêng `requirements.txt` rồi `pip install` thì khi chỉ sửa code, layer cài dependencies (bước chậm nhất) vẫn được dùng lại; chỉ layer `COPY app.py` phía sau phải build lại. Nếu `COPY . .` trước thì mỗi lần sửa một dòng code là cài lại toàn bộ thư viện.
4. **CMD vs ENTRYPOINT:** `ENTRYPOINT` là lệnh cố định của container, tham số của `docker run <image> args` được **nối thêm** vào sau. `CMD` là lệnh/tham số **mặc định**, bị thay thế hoàn toàn nếu `docker run` có truyền lệnh. Thường dùng `ENTRYPOINT` cho executable và `CMD` cho tham số mặc định. Nên dùng exec form (`["python","app.py"]`) để process là PID 1 và nhận được SIGTERM; shell form (`CMD python app.py`) chạy qua `/bin/sh -c`, signal không tới app.

### Exercise 2.2: Build và run

```text
$ docker build -f 02-docker/develop/Dockerfile -t my-agent:develop .
$ docker run -d -p 8000:8000 my-agent:develop
$ curl -s -i -X POST "http://localhost:8000/ask?question=What%20is%20Docker%3F"
HTTP/1.1 200 OK
{"answer":"Container là cách đóng gói app để chạy ở mọi nơi. Build once, run anywhere!"}
$ curl -s http://localhost:8000/health
{"status":"ok","uptime_seconds":0.6,"container":true}
```

### Exercise 2.3: Multi-stage build và so sánh kích thước

- **Stage 1 (builder)** `python:3.11-slim`: cài `gcc`, `libpq-dev`, rồi `pip install --user -r requirements.txt` vào `/root/.local`. Stage này có đủ build tools nhưng **không được deploy**.
- **Stage 2 (runtime)** `python:3.11-slim`: tạo user non-root `appuser`, chỉ `COPY --from=builder /root/.local` (thư viện đã cài, 40.8 MB theo `docker history`) và source code, có `HEALTHCHECK`, chạy `uvicorn ... --workers 2`.
- **Vì sao nhỏ hơn:** image cuối không chứa gcc, header, apt cache hay pip cache của builder; base là `slim` thay cho bản full; chỉ copy kết quả cần để chạy.

```text
$ docker images my-agent --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}'
REPOSITORY:TAG      SIZE
my-agent:advanced   240MB
my-agent:develop    1.67GB
$ docker image inspect my-agent:develop my-agent:advanced --format '{{index .RepoTags 0}} {{.Size}}'
my-agent:develop 1667780202
my-agent:advanced 239815368
```

- Develop: **1667.8 MB** (1 667 780 202 byte)
- Production: **239.8 MB** (239 815 368 byte)
- Chênh lệch: **giảm 85.6 %** ((1667.8 − 239.8) / 1667.8), image production nhỏ hơn ~7 lần.

### Exercise 2.4: Docker Compose stack

```
                     host :80 / :443
                           │
                   ┌───────▼────────┐
                   │ nginx (alpine) │  reverse proxy + LB, rate limit 10r/s/IP,
                   └───────┬────────┘  security headers, /health không rate limit
                           │ http://agent:8000   (mạng bridge "internal")
                   ┌───────▼────────┐
                   │ agent (FastAPI)│  build từ 02-docker/production/Dockerfile (target runtime)
                   └───┬────────┬───┘  không expose port ra host
         redis://redis:6379   http://qdrant:6333
              ┌────────▼──┐  ┌──▼──────────┐
              │ redis:7   │  │ qdrant:v1.9 │   volumes: redis_data, qdrant_data
              └───────────┘  └─────────────┘
```

Services được start: `redis`, `qdrant` (chờ healthy), rồi `agent` (`depends_on` với `condition: service_healthy`), cuối cùng `nginx`. Các service giao tiếp qua mạng bridge `internal` bằng **tên service làm DNS** (`agent`, `redis`, `qdrant`). Chỉ Nginx publish port ra host. Chạy bằng `docker compose -p k5-day14-docker up -d --build` (có `touch .env.local`):

```text
SERVICE   IMAGE                   STATUS                           PORTS
agent     k5-day14-docker-agent   Up 1 second (health: starting)   8000/tcp
nginx     nginx:alpine            Up 1 second                      0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
qdrant    qdrant/qdrant:v1.9.0    Up 12 seconds (healthy)          6333-6334/tcp
redis     redis:7-alpine          Up 12 seconds (healthy)          6379/tcp

$ curl -s -i http://localhost/health
HTTP/1.1 200 OK
Server: nginx
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
{"status":"ok","uptime_seconds":0.8,"version":"2.0.0","timestamp":"2026-11-25T12:49:48.295225"}

$ curl -s -i http://localhost/ask -X POST -H "Content-Type: application/json" -d '{"question": "Explain microservices"}'
HTTP/1.1 200 OK
Server: nginx
{"answer":"Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận."}
```

Response có header `Server: nginx` + security headers, chứng tỏ request đi qua Nginx rồi mới tới agent. Debug container: `docker compose -p k5-day14-docker logs agent`, `docker exec -it <container> /bin/sh`.

## Part 3: Cloud Deployment

### Exercise 3.1/3.2: Deploy agent Part 6

- Nền tảng: **Railway** (deploy thư mục `06-lab-complete/` bằng `railway.toml`, builder `DOCKERFILE`, `healthcheckPath = "/health"`), có thêm service Redis trên Railway (agent kết nối qua `redis.railway.internal`).
- URL: **https://agent-production-74fa31fa.up.railway.app** (chi tiết, biến môi trường và lệnh trong [DEPLOYMENT.md](DEPLOYMENT.md))
- 4 lệnh tự kiểm CP6: `/health` **200**, `/ready` **200**, `/ask` không key **401**, `/ask` có key **200**. `check_production_ready.py --public-url` → `✅ Public URL: GET .../health → 200`.
- Railway báo `railway.toml` (Config as Code) sẽ ngừng hỗ trợ sau 2026-12-01 và khuyên chuyển sang `.railway/railway.ts`. Tôi giữ `railway.toml` vì rubric và script chấm yêu cầu file này.
- Screenshot: [screenshots/dashboard.png](screenshots/dashboard.png), [screenshots/running.png](screenshots/running.png), [screenshots/test.png](screenshots/test.png)

### So sánh `railway.toml` với `render.yaml` (cả hai đều viết trong `06-lab-complete/`)

| Khía cạnh | `railway.toml` | `render.yaml` (Blueprint) | Vì sao khác |
|---|---|---|---|
| Phạm vi | Cấu hình **build/deploy của một service** | Mô tả **toàn bộ hạ tầng** (Infrastructure as Code): service web + Key Value (Redis) + liên kết giữa chúng | Railway tạo service, Redis và biến môi trường qua dashboard/CLI (`railway add`, `railway variables`); Render cho khai báo cả stack trong một file để tạo bằng "New → Blueprint" |
| Định dạng | TOML: `[build] builder = "DOCKERFILE"`, `[deploy] healthcheckPath`, `restartPolicyType` | YAML: `services:` gồm `type: web`, `runtime: docker`, `healthCheckPath`, `plan`, `region` | Quy ước riêng của mỗi platform |
| Redis | Không khai báo trong file; tạo plugin Redis rồi đặt biến `REDIS_URL=${{Redis.REDIS_URL}}` | Khai báo service `type: keyvalue` và nối bằng `fromService: {type: keyvalue, property: connectionString}` | Render nối service bằng tham chiếu trong blueprint; Railway dùng biến tham chiếu |
| Secret | Không nằm trong file, đặt bằng `railway variables set AGENT_API_KEY=...` | Khai báo key nhưng không có giá trị: `sync: false` (nhập tay trên dashboard) hoặc `generateValue: true` | Cả hai đều giữ secret ngoài git; Render cần liệt kê key để biết phải hỏi |
| Thư mục con | Chạy `railway up` ngay trong `06-lab-complete/` | Phải có `rootDir: 06-lab-complete` vì repo chứa nhiều bài | Railway upload thư mục hiện tại; Render build từ repo GitHub |
| PORT | Railway cấp `PORT`, CMD lắng nghe `${PORT:-8000}` | Render cũng cấp `PORT` (mặc định 10000), cùng CMD | Dockerfile dùng `${PORT:-8000}` nên chạy được ở cả hai |
| Health check | `healthcheckPath` chỉ dùng **lúc deploy** (gate trước khi chuyển traffic) | `healthCheckPath` dùng cả lúc deploy (zero-downtime) **và liên tục** để restart instance hỏng | Khác cách hai platform dùng probe |

### Exercise 3.3: Cloud Run CI/CD pipeline (đọc `cloudbuild.yaml` + `service.yaml`, không deploy)

Pipeline Cloud Build có 4 bước nối nhau bằng `waitFor`: (1) **test**: trong image `python:3.11-slim`, cài requirements + pytest và chạy `pytest tests/`, fail thì dừng toàn bộ; (2) **build**: `docker build` gắn 2 tag `ai-agent:$COMMIT_SHA` và `:latest`, dùng `--cache-from :latest` để tận dụng layer cache; (3) **push**: đẩy mọi tag lên Container Registry `gcr.io/$PROJECT_ID`; (4) **deploy**: `gcloud run deploy ai-agent` với image theo đúng `$COMMIT_SHA` (truy vết và rollback được) ở `asia-southeast1`, 1 CPU / 512Mi, `min-instances=1` để tránh cold start, `max-instances=10`, timeout 60 s, secret `OPENAI_API_KEY` lấy từ Secret Manager. `service.yaml` mô tả cùng service theo Knative: autoscale 1–10 instance, `containerConcurrency: 80`, liveness probe `/health`, startup probe `/ready`, `AGENT_API_KEY` và `OPENAI_API_KEY` lấy từ secret thay vì ghi thẳng vào file.

## Part 4: API Security

### Exercise 4.1: API key (`04-api-gateway/develop/app.py`)

- **Check ở đâu:** dependency `verify_api_key()` dùng `APIKeyHeader(name="X-API-Key", auto_error=False)`, gắn vào `/ask` qua `Depends(verify_api_key)`. Key so với `API_KEY = os.getenv("AGENT_API_KEY", ...)`.
- **Sai key:** thiếu header trả **401** "Missing API key"; có header nhưng sai trả **403** "Invalid API key". (Ở Part 6 tôi trả 401 cho cả hai trường hợp theo yêu cầu, và dùng `hmac.compare_digest` để so sánh constant-time.)
- **Rotate key:** key đọc từ env, nên chỉ cần đặt `AGENT_API_KEY` mới trên platform rồi restart/redeploy, không sửa code. Muốn không downtime thì hỗ trợ tạm thời 2 key (cũ + mới, vd `AGENT_API_KEYS=k1,k2`), chuyển client sang key mới rồi gỡ key cũ.

Chạy với `AGENT_API_KEY=secret-key-123 python app.py`:

```text
$ curl -s -i http://localhost:8000/ask -X POST -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 401 Unauthorized
{"detail":"Missing API key. Include header: X-API-Key: <your-key>"}

$ curl -s -i http://localhost:8000/ask -X POST -H "X-API-Key: wrong-key" -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 403 Forbidden
{"detail":"Invalid API key."}

$ curl -s -i http://localhost:8000/ask -X POST -H "X-API-Key: ***" -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 200 OK
{"question":"Hello","answer":"Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic."}
```

### Exercise 4.2: JWT (`04-api-gateway/production`)

JWT flow: `POST /auth/token` với username/password → `authenticate_user` → `create_token` ký HS256 bằng `JWT_SECRET`, payload gồm `sub`, `role`, `iat`, `exp` (60 phút). Client gửi `Authorization: Bearer <token>`. `verify_token` chỉ cần verify chữ ký và hạn, không cần tra DB (stateless). Hết hạn trả 401, token sai trả 403.

```text
### Không có token
HTTP/1.1 401 Unauthorized
{"detail":"Authentication required. Include: Authorization: Bearer <token>"}
### Lấy token (student/demo123)
$ curl -s -X POST http://localhost:8000/auth/token -H "Content-Type: application/json" -d '{"username":"student","password":"demo123"}'
{"access_token": "eyJhbGciOiJI...<redacted>", "token_type": "bearer", "expires_in_minutes": 60, "hint": "<redacted>"}
### Token sai
HTTP/1.1 403 Forbidden
{"detail":"Invalid token."}
### Token đúng
HTTP/1.1 200 OK
{"question":"Explain JWT","answer":"Đây là câu trả lời từ AI agent (mock). ...","usage":{"requests_remaining":9,"budget_remaining_usd":2.1e-05}}
```

### Exercise 4.3: Rate limiting

- **Algorithm:** **sliding window log**. Mỗi user có một `deque` timestamp; mỗi request xoá các timestamp cũ hơn 60 s, đếm phần còn lại, vượt thì 429.
- **Limit:** user thường **10 req/phút** (`rate_limiter_user`), admin 100 req/phút (`rate_limiter_admin`).
- **Bypass cho admin:** `/ask` chọn limiter theo role trong JWT: `rate_limiter_admin if role == "admin" else rate_limiter_user`. Admin (`teacher`) có limit cao hơn chứ không bypass hẳn; muốn bypass hoàn toàn thì bỏ qua `limiter.check()` khi `role == "admin"`.
- **Hạn chế:** state nằm trong RAM của process, nên chạy nhiều worker/instance thì mỗi cái đếm riêng, restart là mất. Part 6 tôi chuyển sang Redis sorted set.

Vòng lặp 20 request với token student (request "Token đúng" ở trên đã dùng 1 lượt):

```text
req 1: HTTP/1.1 200 OK
...
req 9: HTTP/1.1 200 OK
req 10: HTTP/1.1 429 Too Many Requests
x-ratelimit-limit: 10
x-ratelimit-remaining: 0
x-ratelimit-reset: 1795611102
retry-after: 56
{"detail":{"error":"Rate limit exceeded","limit":10,"window_seconds":60,"retry_after_seconds":56}}
req 11: HTTP/1.1 429 Too Many Requests
...
req 20: HTTP/1.1 429 Too Many Requests
```

Đúng 10 request 200 trong 60 s, request thứ 11 bị 429 kèm `Retry-After`.

### Exercise 4.4: Cost guard

`04-api-gateway/production/cost_guard.py` có sẵn: đếm token mỗi user mỗi ngày **trong RAM**, vượt $1/ngày/user trả **402**, vượt $10/ngày toàn hệ thống trả **503**, cảnh báo khi dùng 80 %. Output `/me/usage` sau 10 request: `{"requests":10,"input_tokens":40,"output_tokens":316,"cost_usd":0.000196,"budget_usd":1.0,...}`. (Có một bug nhỏ: `/ask` trả `"budget_remaining_usd": usage.total_cost_usd`, tức là số **đã dùng** chứ không phải số còn lại.)

Cách tôi làm trong Part 6 (`06-lab-complete/app/cost_guard.py`), lưu **trong Redis** để mọi worker/instance dùng chung và không mất khi restart:

- **Tính cost** theo token: `cost = input_tokens/1000 × $0.00015 + output_tokens/1000 × $0.0006`.
- **Hai key theo thời gian (UTC):** `cost:daily:YYYY-MM-DD` (TTL 2 ngày) và `cost:monthly:YYYY-MM` (TTL 32 ngày). Key mới tự sinh khi sang ngày/tháng mới, nên budget **tự reset** mà không cần cron; TTL dọn key cũ.
- `check_budget()` gọi **trước** LLM: `MGET` hai key, `daily ≥ DAILY_BUDGET_USD` hoặc `monthly ≥ MONTHLY_BUDGET_USD` thì `HTTPException(402)`. Redis lỗi trả 503 (fail closed: không biết đã tiêu bao nhiêu thì không cho gọi LLM).
- `record_cost()` gọi **sau** LLM: `INCRBYFLOAT` + `EXPIRE` trong một `MULTI/EXEC` (atomic giữa các worker). Nếu ghi lỗi thì vẫn trả câu trả lời (LLM đã gọi xong) và ghi log `record_cost_failed`.
- Budget đọc từ env (`DAILY_BUDGET_USD`, `MONTHLY_BUDGET_USD`). Script chấm đặt budget 0.000001 USD và nhận **402 ngay ở request thứ 1**: các lần chạy trước của script (budget 1000) dùng chung Redis nên đã cộng chi phí vào key ngày, tổng đã vượt 0.000001 trước khi request đó tới. Với Redis trống thì request đầu đi qua (tổng = 0) và từ request thứ 2 trả 402.

**Rate limit vs cost guard:** rate limit bảo vệ **tốc độ** (chống spam/DoS, chia đều tài nguyên theo thời gian); cost guard bảo vệ **tiền** (tổng chi phí tích luỹ). Cần cả hai: một client gửi đều 10 req/phút không bao giờ bị rate limit nhưng chạy 24/7 với prompt dài vẫn đốt hết ngân sách; ngược lại cost guard không chặn được burst làm quá tải server.

## Part 5: Scaling & Reliability

### Exercise 5.1: Health checks

`05-scaling-reliability/develop/app.py` đã có:
- `GET /health` (**liveness**): luôn trả 200 kèm `status` ok/degraded (theo RAM qua psutil), uptime, version. Fail thì platform **restart** container.
- `GET /ready` (**readiness**): trả 503 khi `_is_ready=False` (đang khởi động hoặc đang shutdown), 200 kèm `in_flight_requests` khi sẵn sàng. Fail thì load balancer **ngừng route traffic** tới instance, nhưng không restart.

Liveness không nên kiểm tra dependency ngoài (Redis/DB): Redis chết mà `/health` fail thì platform restart hàng loạt instance vô ích, vì restart không sửa được Redis. Kiểm tra dependency thuộc về readiness. Đó là lý do Part 6 `/health` luôn 200 còn `/ready` trả 503 khi mất Redis.

### Exercise 5.2: Graceful shutdown

Chạy trong WSL (Windows không gửi được SIGTERM): bật app, gửi 1 request rồi `kill -TERM` sau 50 ms.

```text
health: 200  ready: {"ready":true,"in_flight_requests":1}
in-flight request: {"answer":"Đây là câu trả lời từ AI agent (mock). ..."} -> HTTP 200 in 3.028054s
process exit code: 0, exited 3.16s after SIGTERM
--- server log ---
INFO:     127.0.0.1:54350 - "POST /ask?question=Long%20task HTTP/1.1" 200 OK
INFO:     Shutting down
INFO:     Waiting for application shutdown.
2026-11-25 12:51:26,135 INFO 🔄 Graceful shutdown initiated...
2026-11-25 12:51:26,135 INFO ✅ Shutdown complete
INFO:     Application shutdown complete.
INFO:     Finished server process [433]
2026-11-25 12:51:26,136 INFO Received signal 15 — uvicorn will handle graceful shutdown
```

Request đang chạy vẫn hoàn thành (200), process thoát với exit code 0. Quan sát thêm: dòng `Received signal 15` của `handle_sigterm` xuất hiện **sau** "Finished server process". Lý do: `uvicorn.run()` cài handler SIGTERM của chính nó (đè lên handler đăng ký ở cấp module) và chỉ gọi lại handler cũ sau khi shutdown xong. Vì vậy handler tự viết không được chặn hay `sys.exit()`; nếu cần làm gì đó lúc nhận SIGTERM thì phải bọc handler của uvicorn và gọi lại nó (cách tôi làm ở Part 6 để `/ready` trả 503 ngay khi nhận SIGTERM).

### Exercise 5.3: Stateless design

`05-scaling-reliability/production/app.py` lưu session/history trong Redis (`session:{id}` = JSON, `SETEX` TTL 3600 s, giữ tối đa 20 message), không dùng dict trong RAM. Mỗi response có `served_by: INSTANCE_ID`. Khi chạy nhiều instance, mỗi instance có RAM riêng: lưu history trong dict thì request sau rơi vào instance khác sẽ mất history, restart hay scale down cũng mất. Redis là nơi lưu chung nên **instance nào cũng phục vụ được bất kỳ request nào**.

### Exercise 5.4: Load balancing (`docker compose -p k5-day14-scaling up -d --build --scale agent=3`)

```text
NAME                       STATUS                    PORTS
k5-day14-scaling-agent-1   Up 11 seconds (healthy)   8000/tcp
k5-day14-scaling-agent-2   Up 11 seconds (healthy)   8000/tcp
k5-day14-scaling-agent-3   Up 11 seconds (healthy)   8000/tcp
k5-day14-scaling-nginx-1   Up 11 seconds             0.0.0.0:8080->80/tcp
k5-day14-scaling-redis-1   Up 22 seconds (healthy)   6379/tcp
```

10 request `POST /chat`, đếm header `X-Served-By` (Nginx thêm `$upstream_addr`):

```text
      3 X-Served-By: 172.19.0.3:8000
      3 X-Served-By: 172.19.0.4:8000
      4 X-Served-By: 172.19.0.5:8000
```

Nginx (`server agent:8000`, Docker DNS trả IP cả 3 container) chia round-robin gần đều 3/3/4.

### Exercise 5.5: Test stateless (`python test_stateless.py`)

```text
Session ID: db55d05b-0efc-4d37-9375-befd4329a2e7
Request 1: [instance-71d0b4]  Q: What is Docker?
Request 2: [instance-b595a1]  Q: Why do we need containers?
Request 3: [instance-71d0b4]  Q: What is Kubernetes?
Request 4: [instance-41ab5d]  Q: How does load balancing work?
Request 5: [instance-b595a1]  Q: What is Redis used for?
------------------------------------------------------------
Total requests: 5
Instances used: {'instance-41ab5d', 'instance-b595a1', 'instance-71d0b4'}
✅ All requests served despite different instances!
--- Conversation History ---
Total messages: 10
  [user]: What is Docker?...
  [assistant]: Container là cách đóng gói app ...
  ... (đủ 5 cặp user/assistant theo đúng thứ tự)
✅ Session history preserved across all instances via Redis!
```

**Giải thích:** 5 câu của cùng một session được **cả 3 instance** phục vụ xen kẽ, nhưng history vẫn đủ 10 message theo đúng thứ tự, vì mọi instance đọc/ghi cùng key `session:<id>` trong Redis.

**Thử thêm (kill 1 instance):** script không tự tắt instance nào nên tôi tự làm: tạo session, `docker stop k5-day14-scaling-agent-2`, gửi tiếp 6 lượt cùng session:

```text
stopped agent-2
turn 3 served_by instance-41ab5d HTTP 200
turn 4 served_by instance-b595a1 HTTP 200
turn 5 served_by instance-41ab5d HTTP 200
turn 6 served_by instance-b595a1 HTTP 200
turn 7 served_by instance-41ab5d HTTP 200
turn 8 served_by instance-b595a1 HTTP 200
history count: 14
```

6/6 request vẫn 200, traffic chuyển sang 2 instance còn lại (`proxy_next_upstream error timeout http_503`), history đủ 14 message (7 cặp). (Trường `turn` lệch +1 vì app tính `turn` sau khi đã append câu hỏi vào history; đây là bug hiển thị, không phải mất dữ liệu.)

## Part 6: Final Project — `06-lab-complete/`

Tóm tắt cách làm từng tiêu chí:

| # | Tiêu chí | Cách làm (file) |
|---|---|---|
| 1 | Dockerfile | Multi-stage: builder cài deps vào venv `/opt/venv`; runtime `python:3.11-slim` chỉ copy venv + `app/` + `utils/`, user non-root `app`, `HEALTHCHECK` bằng python urllib (slim không có curl), `CMD ["sh","-c","exec uvicorn ... --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout-graceful-shutdown 8"]`, `exec` để uvicorn là PID 1. Image 273 MB |
| 2 | Compose | `agent` (build `.`, `env_file .env` với `required: false`, `REDIS_URL=redis://redis:6379/0`, `depends_on redis: service_healthy`) + `redis:7-alpine` (healthcheck `redis-cli ping`, AOF + volume) |
| 3 | `.dockerignore` | Loại `.env*` (giữ `.env.example`), `__pycache__`, `*.pyc`, `venv/.venv`, `.git`, `benchmark-observability/`, docs |
| 4 | `/health` | Luôn 200, không đụng Redis |
| 5 | `/ready` | `redis_healthy()` (ping, timeout 2 s), lỗi trả 503; cũng trả 503 ngay khi nhận SIGTERM |
| 6 | API key | `APIKeyHeader(auto_error=False)`, `hmac.compare_digest`; thiếu/sai/chưa đặt `AGENT_API_KEY` thì 401 |
| 7 | Rate limit | Redis sorted set sliding window 60 s theo **hash SHA-256 của key** (không lưu key thô): `ZREMRANGEBYSCORE → ZADD → ZCARD → EXPIRE` trong `MULTI/EXEC`; vượt thì xoá member vừa thêm, trả 429 + `Retry-After` tính từ phần tử cũ nhất. Redis lỗi trả 503 (fail closed) |
| 8 | Cost guard | Xem Exercise 4.4: Redis `INCRBYFLOAT` theo ngày + tháng, vượt budget trả 402 |
| 9 | Config | `config.py` dataclass + `os.getenv` (parse int/float an toàn); `.env.example` đủ biến; `AGENT_API_KEY` không có mặc định |
| 10 | Log JSON | `JsonFormatter` (`json.dumps`) gắn vào root logger: `ts`, `lvl`, `logger`, `event` + field `extra`; middleware log mỗi request (`request_id`, `path`, `status`, `ms`), không log API key |
| 11 | Graceful shutdown | Dựa vào uvicorn (ngừng nhận kết nối, chờ request in-flight, chạy lifespan shutdown, exit 0). Trong lifespan bọc handler SIGTERM: đặt `shutting_down=True` để `/ready` trả 503 rồi **gọi lại handler của uvicorn**; shutdown đóng Redis client |
| 12 | Deploy config | `railway.toml` (builder `DOCKERFILE`, `healthcheckPath = "/health"`) và `render.yaml` (`type: web`, `runtime: docker`, `rootDir`, `healthCheckPath: /health`, `AGENT_API_KEY` với `sync: false`, Redis = `keyvalue` + `fromService`) |

Thêm: exception handler trả `{"error":"invalid_request","details":[...]}` (422) cho input sai và `{"error":"internal_error"}` (500) cho lỗi không lường trước, không lộ stack trace; tắt `/docs` khi `ENVIRONMENT=production`.

### Output `python check_production_ready.py` (bản đầy đủ, có Docker)

Chạy trong WSL2 Ubuntu (`os.name == posix`) để test được SIGTERM:

```text
================================================================
  📋 Checklist Deliverable
     ✅ Dockerfile (multi-stage, < 500 MB)
     ✅ docker-compose.yml (agent + redis)
     ✅ .dockerignore
     ✅ Health check endpoint (GET /health)
     ✅ Readiness endpoint (GET /ready)
     ✅ API Key authentication
     ✅ Rate limiting
     ✅ Cost guard
     ✅ Config từ environment variables
     ✅ Structured logging
     ✅ Graceful shutdown
     ✅ Public URL ready (Railway / Render config)
----------------------------------------------------------------
  Result: 12/12 tiêu chí đạt · 43/43 checks passed
  🎉 PASS 12/12 — PRODUCTION READY! Deploy nào!
================================================================
```

Một số dòng chi tiết đáng chú ý: `Docker: image < 500 MB — 273 MB`; `429 sau 5 request thành công (RATE_LIMIT_PER_MINUTE=5)`; `state nằm trong Redis — restart agent vẫn bị 429`; `Redis mất kết nối → /ready 503 (còn /health vẫn 200)`; `402 ở request thứ 1`; `SIGTERM lúc đang có request → request vẫn 200, process tự thoát — exit code 0`; `docker stop tắt êm — exit code 0, 0.6s`.

Ghi chú môi trường: Python trong WSL là 3.14 (không có pip/sudo), nên tôi tải wheel Linux bằng pip phía Windows (`--platform manylinux... --python-version 3.14`) rồi trỏ `PYTHONPATH`. Vì pydantic 2.9 không có wheel cho 3.14, lần chạy WSL dùng FastAPI/pydantic bản mới hơn. Docker Desktop chưa bật WSL integration cho distro Ubuntu, nên tôi dùng một script tạm gọi `docker.exe` của Windows (đổi đường dẫn `/mnt/c/...` sang `C:\...`). Lần chạy đầy đủ trên Windows với đúng version trong `requirements.txt` cũng đạt 42/42 check, chỉ mục SIGTERM là ⚠️ vì Windows không gửi được SIGTERM. Phần Docker (`docker stop` exit 0, 0.7 s) vẫn đạt.

## AI log

Dùng **Claude Code (Claude Opus 5.5)** trong VS Code với yêu cầu "làm cho tôi bài lab của ngày hôm nay". Tôi kiểm tra lại từng phần như sau:

| Phần | Hỏi / nhờ gì | Nhận được | Tôi kiểm tra / sửa thế nào |
|---|---|---|---|
| Part 6 code | Viết các chỗ TODO theo bảng tiêu chí trong README + script chấm | `config.py`, `auth.py`, `redis_client.py`, `rate_limiter.py`, `cost_guard.py`, `main.py`, Dockerfile, compose, `.dockerignore`, `.env.example`, `railway.toml`, `render.yaml` | Chạy `check_production_ready.py --static-only` (28/28), bản đầy đủ trên Windows (42/42) rồi trong WSL (43/43, PASS 12/12). Đọc lại từng file để giải thích được: vì sao `/health` không kiểm tra Redis, vì sao `auto_error=False` (tránh 422/403), vì sao phải gọi lại handler SIGTERM của uvicorn, vì sao `exec` trong CMD |
| Part 1–5 | Chạy các ví dụ, ghi output, trả lời câu hỏi | Output thật của curl/docker/test script; câu trả lời | Mọi số (1.67 GB / 240 MB, 401/403/200/429, X-Served-By) đều lấy từ lệnh đã chạy. AI phát hiện thêm 2 bug của code mẫu (log JSON ở Part 1, `budget_remaining_usd` ở Part 4); tôi đọc lại code để xác nhận |
| Part 7 | Chạy load test, eval, sửa KB, viết CI, chạy stack + chaos, chụp ảnh | `reports/*.json`, `benchmark-results/*.csv`, `.github/workflows/quality-gate.yml`, 4 ảnh (chụp bằng Edge headless từ stack local), `BENCHMARK_REPORT.md` | Đối chiếu số trong report với JSON/CSV. Không sửa `golden_dataset.jsonl` hay script chấm. Sửa lỗi `PYTHONUSERBASE` trong `production/Dockerfile` vì container crash-loop |
| Part 3 deploy | Sau khi tôi tự cài Railway CLI và `railway login`, nhờ AI tạo project, Redis, đặt biến, deploy | Project `day14-production-agent` (agent + Redis), URL public, `test.png`/`running.png` dựng từ output CLI | Kiểm tra 4 lệnh CP6 (200/200/401/200), `--public-url` ✅, log `redis_ok=true`. Key sinh ngẫu nhiên, truyền qua stdin, lưu trong `.env.railway` (gitignored), không in ra. AI lỡ tạo trùng một service Redis (`Redis-94QD`) do service đầu chưa khởi tạo xong, đã xoá ngay |
| Môi trường | — | AI lỡ cài `requirements.txt` (pin pydantic 2.9) vào Python global làm hỏng ràng buộc của `pydantic-ai`; đã nâng lại pydantic global (`pip check` sạch) và chuyển sang `.venv` cho lab | — |
