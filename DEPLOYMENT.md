# Deployment Information

## Public URL

https://agent-production-74fa31fa.up.railway.app

## Platform

**Railway** (project `day14-production-agent`, environment `production`, region `iad`). Deploy chính agent Part 6 trong thư mục `06-lab-complete/`, build bằng `Dockerfile` theo `railway.toml` (`builder = "DOCKERFILE"`, `healthcheckPath = "/health"`). Project có 2 service:

| Service | Nguồn | Ghi chú |
|---|---|---|
| `agent` | `railway up` từ `06-lab-complete/` | Railway cấp `PORT=8080`; CMD `uvicorn ... --port ${PORT:-8000} --workers 2` |
| `Redis` | Template Redis của Railway (`redis:8.2`, có volume `/data`) | Agent kết nối qua mạng nội bộ `redis.railway.internal:6379` |

Các lệnh đã dùng:

```bash
cd 06-lab-complete
railway init --name day14-production-agent
railway add --database redis
railway add --service agent
echo "<key>" | railway variable set AGENT_API_KEY --stdin --service agent --skip-deploys   # key không nằm trong repo
railway variable set 'REDIS_URL=${{Redis.REDIS_URL}}' ENVIRONMENT=production RATE_LIMIT_PER_MINUTE=10 \
        DAILY_BUDGET_USD=1.0 MONTHLY_BUDGET_USD=10.0 --service agent --skip-deploys
railway up --service agent --ci
railway domain --service agent
```

## Test Commands

4 lệnh tự kiểm của CP6 (chạy 2026-11-25 13:40 UTC). Mỗi lệnh chỉ in mã HTTP:

```bash
export BASE=https://agent-production-74fa31fa.up.railway.app
export AGENT_API_KEY=<key đã đặt trên Railway>

curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/health"     # 200
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/ready"      # 200
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" -H 'Content-Type: application/json' -d '{"question":"Hello"}'   # 401
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" -H 'Content-Type: application/json' -H "X-API-Key: $AGENT_API_KEY" -d '{"question":"Hello"}'   # 200
```

Kết quả thực tế:

```text
200
200
401
200
```

Script chấm: `python check_production_ready.py --static-only --public-url "$BASE"` → `✅ Public URL: GET https://agent-production-74fa31fa.up.railway.app/health → 200`.

### Health Check

```bash
curl https://agent-production-74fa31fa.up.railway.app/health
# {"status":"ok","environment":"production"}
```

### API Test (with authentication)

```bash
curl -X POST https://agent-production-74fa31fa.up.railway.app/ask \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "question": "What is deployment?"}'
# {"question":"What is deployment?","answer":"Deployment là quá trình đưa code từ máy bạn lên server để người khác dùng được.","usage":{"input_tokens":3,"output_tokens":16,"cost_usd":1.005e-05}}
```

`GET /docs` trả 404 vì `ENVIRONMENT=production` đã tắt Swagger UI.

## Environment Variables Set

Đặt trên Railway cho service `agent`, không ghi trong code hay repo:

| Biến | Giá trị |
|---|---|
| `AGENT_API_KEY` | secret ngẫu nhiên 43 ký tự (`secrets.token_urlsafe(32)`), chỉ có trên Railway |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` (tham chiếu service Redis, resolve thành `redis://...@redis.railway.internal:6379`) |
| `ENVIRONMENT` | `production` |
| `RATE_LIMIT_PER_MINUTE` | `10` |
| `DAILY_BUDGET_USD` / `MONTHLY_BUDGET_USD` | `1.0` / `10.0` |
| `PORT` | Railway tự cấp (`8080`) |
| `LOG_LEVEL` | không đặt, dùng mặc định `INFO` |

Log runtime trên Railway (dòng `startup`): `environment="production" port=8080 rate_limit_per_minute=10 redis_ok=true`.

## Screenshots

- [Deployment dashboard](screenshots/dashboard.png): canvas project `day14-production-agent` trên Railway (agent Online → Redis Online + volume), panel Metrics của agent (8 request: 6×2xx, 2×4xx từ lần tự kiểm)
- [Service running](screenshots/running.png): `railway service list` (agent và Redis ● Online) + log runtime JSON của agent
- [Test results](screenshots/test.png): 4 lệnh tự kiểm ra 200 / 200 / 401 / 200 + script chấm `--public-url`

`running.png` và `test.png` được dựng từ output terminal thật lưu lúc 13:40 UTC (render thành trang dạng terminal rồi chụp), không phải chụp trực tiếp cửa sổ terminal.
