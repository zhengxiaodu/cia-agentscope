{{/*
Nginx 主配置（完整 nginx.conf，整体挂载到 /etc/nginx/nginx.conf）。

之所以是"完整 nginx.conf"而不是 conf.d 片段：配置含 worker_processes / events，
这两个指令只允许在 main 上下文，作为 conf.d 片段挂载 nginx 会判定为非法并拒绝启动。

注意：api.enabled=false 时不要把 /api/ 反代指向不存在的 Service，
nginx 静态解析 upstream 失败会直接退出（host not found in upstream）。
*/}}
{{- define "oia-agentscope.nginx.config.nginx" }}
user  nginx;
worker_processes  auto;
{{- if .Values.proxy.log.persistence.enabled }}
error_log  {{ .Values.proxy.log.persistence.mountPath }}/error.log notice;
{{- end }}
pid        /var/run/nginx.pid;

events {
    worker_connections  1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    server_tokens off;

    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

{{- if .Values.proxy.log.persistence.enabled }}
    access_log  {{ .Values.proxy.log.persistence.mountPath }}/access.log  main;
{{- end }}

    sendfile        on;
    keepalive_timeout  65;

    client_max_body_size {{ .Values.proxy.clientMaxBodySize }};
    gzip  on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript;
    gzip_min_length 1024;

    server {
        listen       80;
        server_name  localhost;

        root  /usr/share/nginx/html;
        index index.html index.htm;

        # SPA 路由 fallback
        location / {
            try_files $uri $uri/ /index.html;
        }

        # Nginx 自身健康检查，不依赖后端
        location = /nginx-healthz {
            default_type text/plain;
            access_log off;
            return 200 'OK';
        }
{{- if .Values.api.enabled }}

        # 后端健康检查
        location = /health {
            proxy_pass http://{{ template "oia-agentscope.api.fullname" . }}:{{ .Values.api.service.port }}/health;
            proxy_set_header Host $host;
        }

        # 业务接口
        location /api/ {
            proxy_pass http://{{ template "oia-agentscope.api.fullname" . }}:{{ .Values.api.service.port }};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout {{ .Values.proxy.proxyReadTimeout }}s;
        }
{{- end }}

        location ~* \.(jpg|jpeg|png|gif|ico|css|js)$ {
            expires 1d;
            add_header Cache-Control "public, max-age=86400";
        }

        error_page 500 502 503 504 /50x.html;
        location = /50x.html {
            root /usr/share/nginx/html;
        }
    }
}
{{- end }}

{{/*
 Redis 连接串（REDIS_URL）：优先使用 externalRedis.url，否则由分段配置拼装。
 密码用 urlquery 转义，避免 # @ % $ 等特殊字符截断连接串或被当成插值符。
*/}}
{{- define "oia-agentscope.redisUrl" -}}
{{- if .Values.externalRedis.url -}}
{{- .Values.externalRedis.url -}}
{{- else -}}
redis://{{ if .Values.externalRedis.password }}:{{ urlquery .Values.externalRedis.password }}@{{ end }}{{ .Values.externalRedis.host }}:{{ .Values.externalRedis.port }}/{{ .Values.externalRedis.db }}
{{- if .Values.externalRedis.params -}}
?{{ .Values.externalRedis.params }}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
 输出一个后端环境变量（供 api-config ConfigMap 与 Secret 注入共用判断逻辑）：
   - api.existingSecret 已设置 且 existingSecretKeys.<field> 非空 -> 只写注释（真实值由 Secret 注入）
   - 否则输出 "KEY: value"（明文写入 ConfigMap）
 用法：
   {{ include "oia-agentscope.api.env" (dict "ctx" . "name" "JWT_SECRET" "value" .Values.api.jwt.secret "field" "jwtSecret") }}
*/}}
{{- define "oia-agentscope.api.env" -}}
{{- $ctx := .ctx -}}
{{- $secretKey := "" -}}
{{- if and $ctx.Values.api.existingSecret $ctx.Values.api.existingSecretKeys -}}
{{- $secretKey = default "" (index $ctx.Values.api.existingSecretKeys .field) -}}
{{- end -}}
{{- if $secretKey -}}
# {{ .name }} 由 Secret {{ $ctx.Values.api.existingSecret }} 注入
{{- else -}}
{{ .name }}: {{ .value | quote }}
{{- end -}}
{{- end -}}

{{/*
 后端环境变量（api-config ConfigMap）。

 只输出应用 .env / Settings 实际读取的变量：
   CONFIG_DECRYPT_KEY / JWT_SECRET / JWT_EXPIRE_HOURS /
   AUTH_MOCK / AUTH_API_URL / REDIS_URL / REDIS_SESSION_TTL /
   MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE /
   UPLOAD_MAX_SIZE_MB / SESSION_FILES_PERSIST_DIR /
   LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY / LANGFUSE_BASE_URL /
   LOG_LEVEL / LOG_DIR / LOG_MAX_BYTES / LOG_BACKUP_COUNT /
   MNG_AUTH_URL / MNG_INTENT_URL / EXTERNAL_SKILLS_DIR /
   OPENSANDBOX_* / WORKSPACE_TTL / REGULATIONS_* / POLICY_QA_KB_MAP /
   MINERU_* / SENSITIVE_* / BOCHA_*
*/}}
{{- define "oia-agentscope.api.config" -}}
# ===== 敏感配置加密主密钥 =====
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "CONFIG_DECRYPT_KEY" "value" .Values.api.configDecryptKey "field" "configDecryptKey") }}

# ===== JWT 鉴权 =====
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "JWT_SECRET" "value" .Values.api.jwt.secret "field" "jwtSecret") }}
JWT_EXPIRE_HOURS: {{ .Values.api.jwt.expireHours | quote }}

# ===== 第三方管理系统登录校验 =====
AUTH_MOCK: {{ .Values.api.auth.mock | quote }}
AUTH_API_URL: {{ .Values.api.auth.apiUrl | quote }}

# ===== Redis 会话存储 =====
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "REDIS_URL" "value" (include "oia-agentscope.redisUrl" .) "field" "redisUrl") }}
REDIS_SESSION_TTL: {{ .Values.api.session.redisSessionTtl | quote }}

# ===== MySQL 会话持久化 =====
MYSQL_HOST: {{ .Values.externalMysql.host | quote }}
MYSQL_PORT: {{ .Values.externalMysql.port | quote }}
MYSQL_USER: {{ .Values.externalMysql.username | quote }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "MYSQL_PASSWORD" "value" .Values.externalMysql.password "field" "mysqlPassword") }}
MYSQL_DATABASE: {{ .Values.externalMysql.database | quote }}

# ===== 文件上传 =====
UPLOAD_MAX_SIZE_MB: {{ .Values.api.upload.maxSizeMb | quote }}
SESSION_FILES_PERSIST_DIR: {{ .Values.api.upload.sessionFilesPersistDir | quote }}

# ===== Langfuse 可观测性 =====
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "LANGFUSE_SECRET_KEY" "value" .Values.langfuse.secretKey "field" "langfuseSecretKey") }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "LANGFUSE_PUBLIC_KEY" "value" .Values.langfuse.publicKey "field" "langfusePublicKey") }}
LANGFUSE_BASE_URL: {{ .Values.langfuse.baseUrl | quote }}

# ===== 日志 =====
LOG_LEVEL: {{ .Values.api.log.level | quote }}
LOG_DIR: {{ .Values.api.log.dir | quote }}
LOG_MAX_BYTES: {{ .Values.api.log.maxBytes | quote }}
LOG_BACKUP_COUNT: {{ .Values.api.log.backupCount | quote }}

# ===== 管理中心（用户鉴权 / 意图卡片地址分离） =====
MNG_AUTH_URL: {{ .Values.api.mng.authUrl | quote }}
MNG_INTENT_URL: {{ .Values.api.mng.intentUrl | quote }}

# ===== 外部技能目录 =====
EXTERNAL_SKILLS_DIR: {{ .Values.api.skills.externalDir | quote }}

# ===== OpenSandbox 沙箱 =====
OPENSANDBOX_DOMAIN: {{ .Values.api.opensandbox.domain | quote }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "OPENSANDBOX_API_KEY" "value" .Values.api.opensandbox.apiKey "field" "opensandboxApiKey") }}
OPENSANDBOX_PROTOCOL: {{ .Values.api.opensandbox.protocol | quote }}
OPENSANDBOX_USE_SERVER_PROXY: {{ .Values.api.opensandbox.useServerProxy | quote }}
OPENSANDBOX_IMAGE: {{ .Values.api.opensandbox.image | quote }}
OPENSANDBOX_RESOURCE_CPU: {{ .Values.api.opensandbox.resource.cpu | quote }}
OPENSANDBOX_RESOURCE_MEMORY: {{ .Values.api.opensandbox.resource.memory | quote }}
WORKSPACE_TTL: {{ .Values.api.opensandbox.workspaceTtl | quote }}
OPENSANDBOX_POOL_SIZE: {{ .Values.api.opensandbox.poolSize | quote }}
OPENSANDBOX_POOL_REFILL: {{ .Values.api.opensandbox.poolRefill | quote }}

# ===== 制度问答（regulations） =====
REGULATIONS_KB_API_BASE_URL: {{ .Values.api.regulations.kb.baseUrl | quote }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "REGULATIONS_KB_INTERNAL_TOKEN" "value" .Values.api.regulations.kb.internalToken "field" "kbInternalToken") }}
REGULATIONS_TOP_K_OVERRIDE: {{ .Values.api.regulations.topKOverride | quote }}
REGULATIONS_RERANK_TOP_K_OVERRIDE: {{ .Values.api.regulations.rerankTopKOverride | quote }}
REGULATIONS_QUERY_UNDERSTANDING_ENABLED: {{ .Values.api.regulations.query.understandingEnabled | quote }}
REGULATIONS_QUERY_REWRITE_HYDE_ENABLED: {{ .Values.api.regulations.query.rewriteHydeEnabled | quote }}
REGULATIONS_QUERY_REWRITE_STEPBACK_ENABLED: {{ .Values.api.regulations.query.rewriteStepbackEnabled | quote }}
REGULATIONS_QUERY_REWRITE_LLM_CLASSIFY_ENABLED: {{ .Values.api.regulations.query.rewriteLlmClassifyEnabled | quote }}
REGULATIONS_QUERY_REWRITE_BUSINESS_CLASSIFY_ENABLED: {{ .Values.api.regulations.query.rewriteBusinessClassifyEnabled | quote }}
REGULATIONS_QUERY_REWRITE_BUSINESS_INTENTS_ENABLED: {{ .Values.api.regulations.query.rewriteBusinessIntentsEnabled | quote }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "REGULATIONS_LANGFUSE_PUBLIC_KEY" "value" .Values.api.regulations.langfuse.publicKey "field" "regulationsLangfusePublicKey") }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "REGULATIONS_LANGFUSE_SECRET_KEY" "value" .Values.api.regulations.langfuse.secretKey "field" "regulationsLangfuseSecretKey") }}
REGULATIONS_LANGFUSE_ENABLED: {{ .Values.api.regulations.langfuse.enabled | quote }}
REGULATIONS_LANGFUSE_SAMPLE_RATE: {{ .Values.api.regulations.langfuse.sampleRate | quote }}
REGULATIONS_DASHBOARD_USE_REAL_DATA: {{ .Values.api.regulations.dashboard.useRealData | quote }}
REGULATIONS_DASHBOARD_LOOKBACK_DAYS: {{ .Values.api.regulations.dashboard.lookbackDays | quote }}
POLICY_QA_KB_MAP: {{ .Values.api.regulations.policyQaKbMap | quote }}

# ===== MinerU 文档解析 =====
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "MINERU_API_KEY" "value" .Values.api.mineru.apiKey "field" "mineruApiKey") }}
MINERU_BASE_URL: {{ .Values.api.mineru.baseUrl | quote }}

# ===== 安全敏感内容检测 =====
SENSITIVE_ENABLED: {{ .Values.api.sensitive.enabled | quote }}
SENSITIVE_SERVICE_URL: {{ .Values.api.sensitive.serviceUrl | quote }}
SENSITIVE_THRESHOLD: {{ .Values.api.sensitive.threshold | quote }}
SENSITIVE_TIMEOUT: {{ .Values.api.sensitive.timeout | quote }}

# ===== 博查网络搜索 =====
BOCHA_API_BASE: {{ .Values.api.bocha.apiBase | quote }}
{{ include "oia-agentscope.api.env" (dict "ctx" . "name" "BOCHA_API_KEY" "value" .Values.api.bocha.apiKey "field" "bochaApiKey") }}
{{- end }}
