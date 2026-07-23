# sub2api Kubernetes 部署

把 sub2api 部署到百度 CCE 集群，作为 [litellm-relay](../litellm/README.md) 的内网上游。
清单风格对齐 litellm-relay 的 `k8s/`：Kustomize 编排 + 百度 BCR 镜像 + `vast-secret` 拉取凭证。

## 设计要点

- **无状态**：所有状态（用户、group、账号、Key、用量）落在**外部 PostgreSQL + Redis**，Pod 不挂 PVC，可随 HPA 水平扩展。`AUTO_SETUP=true` 在首启时自动建表/建管理员。
- **PG / Redis 复用已有服务**：连接信息全部放进 `sub2api-env` Secret（见 `secret.example.yaml`），不在集群内自建数据库。
- **仅内网暴露**：Service 是 `ClusterIP`，只给同集群的 litellm-relay 调用，不开公网 LoadBalancer。要访问管理后台请另配带鉴权的 Ingress。
- **固定密钥**：`JWT_SECRET`、`TOTP_ENCRYPTION_KEY` 必须长期不变，否则重启后登录会话失效、2FA 全部作废。

## 文件

| 文件 | 作用 |
|------|------|
| `namespace.yaml` | `sub2api` 命名空间 |
| `deployment.yaml` | 工作负载（镜像占位 `:latest`，发布时替换为 VPC 地址+版本） |
| `service.yaml` | `ClusterIP` :8080 |
| `horizontal-pod-autoscaler.yaml` | CPU 60% / 内存 75%，2→6 副本 |
| `secret.example.yaml` | `sub2api-env` 字段参考（DB / Redis / 密钥 / 管理员） |
| `kustomization.yaml` | 汇总以上资源 |
| `Makefile` | 构建、推送、发布、校验 |

## 首次部署

```bash
cd deploy/k8s

# 1. 命名空间 + 镜像拉取凭证（复用 litellm 那套 BCR 账号）
make create-pull-secret

# 2. 准备并创建运行时 Secret
#    从 secret.example.yaml 抄字段到 .env，填入 PG / Redis 连接信息与固定密钥：
#      JWT_SECRET=$(openssl rand -hex 32)
#      TOTP_ENCRYPTION_KEY=$(openssl rand -hex 32)
make create-env-secret ENV_FILE=.env

# 3. 构建镜像 + 推 BCR + 部署 + 等 rollout
make deploy
```

> `make deploy` 用仓库根的 `Dockerfile` 构建（上下文 = 仓库根，因为要 `backend/` `frontend/` `docs/`），
> 打 `VERSION` 标签推到公网 BCR，再渲染成 VPC 内网镜像地址应用到集群。

## 日常操作

| 命令 | 说明 |
|------|------|
| `make deploy` | 构建+推送+部署（代码/镜像变化时用） |
| `make restart-production` | 滚动重启（改了 Secret 后让 Pod 重新读取） |
| `make apply-production` | 只重新应用清单，不构建镜像 |
| `make k8s-check` | 本地 dry-run 校验清单 |
| `make show-version` | 打印当前镜像标签 |

改 Secret 后需要 `make restart-production`：Pod 的 `envFrom` 只在启动时读取，热更不会生效。

## 前置：外部 PG / Redis

- **PostgreSQL**：建议单独一个 database（如 `sub2api`），不与 litellm 共库。RDS 通常要求 `DATABASE_SSLMODE=require`（或 `verify-full`）。
- **Redis**：sub2api 的粘性会话、并发限制依赖它。填 `REDIS_HOST/PORT/PASSWORD`，云 Redis 若强制 TLS 则设 `REDIS_ENABLE_TLS=true`。

## 对接 litellm-relay

litellm 与 sub2api 在同一集群，走内网 DNS 直连，不经公网。

1. **拿池 Key**：在 sub2api 后台对每个厂商建 group → 挂订阅账号 → 生成绑定该 group 的 API Key。每厂商建 ≥2 个 group，litellm 才能做跨池容灾。

2. **把地址和池 Key 加进 litellm 的 `litellm-relay-env` Secret**：

   ```
   # sub2api 与 litellm 在不同 namespace，用完整 FQDN，结尾带 /v1
   SUB2API_BASE_URL=http://sub2api.sub2api.svc.cluster.local:8080/v1
   SUB2API_GPT_GROUP1=sk-<group1 的 sub2api Key>
   SUB2API_GPT_GROUP2=sk-<group2 的 sub2api Key>
   ```

3. **在 litellm 的 `config.yaml` `model_list` 增加指向 sub2api 的条目**
   （参考 [../litellm/config.yaml](../litellm/config.yaml) 的写法），然后在 litellm-relay 仓库执行
   `make deploy-config` 发布（纯配置变更，不重构镜像）。

## 两个必须先定的坑

1. **粘性会话**：不要在 litellm 开 `forward_client_headers_to_llm_api`（会用客户端 `Authorization` 覆盖池 Key，导致鉴权错乱）。改让客户端在请求体带稳定会话标识 `metadata.user_id=session_xxx` 来保持粘性。
2. **双重计费**：sub2api 与 litellm 两层都会计量。先定「以谁为准」——推荐 litellm 做组织预算/限流，给它用的 sub2api 内部 Key 设宽松额度，避免两边同时报额度耗尽。
