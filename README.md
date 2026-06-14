# 移动云电脑保活工具（协议级）

基于对「移动云电脑」Windows V3.8.2 客户端 Electron 源码逆向 + HAR 抓包解密，用 Python 复刻完整协议。

## ✅ 两个保活层次都已实现并验证

| 层次 | 命令 | 机制 | 状态 |
|------|------|------|------|
| **账号登录态保活** | `keepalive` | 周期调 getUserInfo/getDeviceList | ✅ 已验证 |
| **桌面会话保活** ⭐ | `desktop-keepalive` | 周期调 `/resource/desktopUptime` | ✅ 已验证 |

### 桌面会话保活验证记录

用抓包解密的真实凭证调用，服务端返回运行时长持续增长：

```
12:47:46 启动桌面保活: instance=CCA-2b44466f2dd0, 间隔=5s
12:47:47 桌面 CCA-2b44466f2dd0 运行时长: 11小时8分7秒   ← 第1轮 ✓
12:47:52 桌面 CCA-2b44466f2dd0 运行时长: 11小时8分12秒  ← 第2轮 ✓ (增长5秒)
```

**这证明：桌面会话保活不需要 SPICE 协议，不需要 uSmartView 二进制，纯 HTTP 即可。**
（之前基于纯源码分析得出的"必须 SPICE 才能保活"的结论，已被抓包证伪。）

## 安装

```bash
pip install -r requirements.txt
```

## 配置说明（cloud_pc.json）

`cloud_pc.json` 由 `login` 命令自动生成，也可手动创建。字段分两类：

### 🔴 必填（缺一不可）

| 字段 | 说明 | 获取方式 |
|------|------|---------|
| `username` | 移动云账号 | 你提供 |
| `password` | 账号密码 | 你提供 |
| `access_token` | 登录凭证，每个请求都带 | `login` 自动获取，token 失效后自动用账号密码重登 |
| `device_uid` | 设备指纹（**必须跨运行稳定**） | 首次运行自动生成（Windows 注册表 / Linux machine-id）。⚠️ 改了会触发"未授信设备"，需重新短信验证 |
| `instance_id` | 目标云电脑实例（保活对象） | `desktop-keepalive` 自动拉取，无需手填 |

**最小配置示例**（首次使用，让 `login` 命令自动补全其余字段）：
```json
{
  "username": "你的账号",
  "password": "你的密码"
}
```

### 🟢 可选（全部有默认值，服务端不校验）

以下字段会作为"设备统计信息"上报，但**实测服务端不做任何校验**（空值也能成功保活）。
不填则用默认值，对保活功能无影响：

| 字段 | 默认值 |
|------|--------|
| `device_name` | 主机名 |
| `client_type` | 按系统自动判断（`linux_x86-64` / `pc_windows_64_yt` / `pc_mac`） |
| `operating_system` | `platform.system()` |
| `cores` / `ram` | 4 / 8 |
| `processor` / `device_company` / `device_model` 等 | "Unknown" / "Server" |
| `ip_address` / `mac_address` | 127.0.0.1 / 00:00:... |

> **为什么这些字段可选？** 通过逐字段删除实测验证：连空 commonParams（只有 `instanceId + accessToken`）都能成功调用 `desktopUptime`。这些字段纯粹是客户端上报的统计信息，服务端只凭 `accessToken` 鉴权。

### 迁移到其他服务器

只需拷贝 `cloud_pc.json`，**保持 `device_uid` 不变**即可：

```bash
scp -r cloudpc-keepalive/ user@server:~/
ssh user@server
cd cloudpc-keepalive && pip install -r requirements.txt
python main.py desktop-keepalive   # 直接能用
```

## 使用

### 方式1：完整流程（登录 + 桌面保活）

```bash
# 1. 登录（首次，交互式输入账号密码）
python main.py login
# account: <账号>
# password: <密码>
# → 自动保存 cloud_pc.json

# 2. 桌面保活（全自动：自动拉桌面列表 + 选桌面 + 保活）
python main.py desktop-keepalive
```

> `desktop-keepalive` 不带任何参数即为全自动模式：自动调 `/user/getDeviceInfo`
> 拉取桌面列表，自动选择目标桌面，周期性保活。无需手动获取 `instance_id`。

### 命令一览

```bash
python main.py login                    # 交互式登录
python main.py keepalive                # 账号登录态保活（5分钟一次）
python main.py desktop-keepalive        # 桌面会话保活（5分钟一次）⭐
python main.py desktop-keepalive --instance-id CCA-xxx --interval 180
python main.py list-desktops            # 尝试拉桌面列表
python main.py status                   # 检查 token 有效性
python main.py logout                   # 登出
```

---

## 以下为旧版说明（保留作协议细节参考）

> ℹ️ 下文是阶段1时期（抓包前）的说明，部分结论（如"桌面会话必须 SPICE"）
> 已被 HAR 抓包解密推翻。协议细节（签名/加密/登录）仍然准确。

## 已验证

通过真实请求 `/user/getSysTime` 接口确认整个协议栈工作正常：

```
Testing /user/getSysTime (no auth)...
RESPONSE: {'systime': '2026-06-14 12:28:11'}
=== SUCCESS: server accepted signature + encryption ===
```

这证明：
- ✅ HmacSHA1 URL 签名（`BC_SIGNATURE&` + secretKey）被服务端接受
- ✅ RSA-1024 PKCS1 请求体加密正确
- ✅ RSA-1024 响应解密正确
- ✅ 设备指纹（commonParams）格式正确

## 安装

```bash
pip install -r requirements.txt
```

依赖：`requests`、`pycryptodome`（Linux 服务器可选 `netifaces` 用于精确读网卡）。

## 使用

### 1. 登录

```bash
python main.py login
# account: <你的账号>
# password: <你的密码>   # 输入不回显
```

登录成功后自动保存 `cloud_pc.json`（含账号、密码明文、device_uid、access_token）。

若服务端要求短信验证（未授信设备/二次验证/增强策略），会交互式提示输入手机号和验证码。

### 2. 保活

```bash
# 默认：每 5 分钟一次，无限循环
python main.py keepalive

# 自定义间隔和轮数
python main.py keepalive --interval 300 --rounds 100

# 配合 crontab（每 10 分钟保活一次）
*/10 * * * * cd /path/to/cloudpc-keepalive && python main.py keepalive --rounds 1 >> keepalive.log 2>&1
```

保活逻辑（每个周期依次尝试）：
1. `POST /client/getSysConfig` —— 拉用户信息，证明 token 有效
2. `POST /user/getDeviceInfo` —— 拉桌面列表，触发服务端会话刷新
3. `POST /login/batchPushLoginQkk` —— 上报探针事件，模拟客户端活跃

任一接口返回 token 失效错误时，自动用保存的账号密码重新登录。

### 3. 检查 token 状态

```bash
python main.py status
```

### 4. 登出

```bash
python main.py logout
```

## 协议细节（逆向自 V3.8.2 源码）

### 请求格式

每个请求两层加密：

**1) URL 签名（HmacSHA1）** —— 复刻 `util/ecloudHttpUtil.js:189-204`

```
GET 参数（按插入顺序）:
  AccessKey        = 53bb79015a3f47c4be166d9371f68f14
  SignatureMethod  = HmacSHA1
  SignatureNonce   = <uuid4 去横线>
  SignatureVersion = V2.0
  Timestamp        = <UTC+8, YYYY-MM-DDTHH:MM:SSZ>

stringToSign = "POST\n" + encodeURIComponent(apiPath + endpoint) + "\n" + sha256(querystring)
Signature    = hmac_sha1_hex(stringToSign, "BC_SIGNATURE&" + secretKey)
```

**2) 请求体（RSA-1024 加密）** —— 复刻 `util/cryptoUtil.js:39-66`

```python
merged_body = {**业务参数, **commonParams, "accessToken": token?}
encrypted   = RSA_PKCS1_encrypt(JSON(merged_body), chunk=117字节)
http_body   = {"params": base64(encrypted)}
```

### 关键常量

| 常量 | 值 | 来源 |
|------|-----|------|
| baseUrl | `https://cloudpc.ecloud.10086.cn` | settingValue.js 解密 |
| apiPath | `/api/cem/gateway/outer/cem-webapi` | 同上 |
| accessKey | `53bb79015a3f47c4be166d9371f68f14` | 同上 |
| secretKey | `6b0d3b93f3aa4c7ea076c841bead1ddd` | 同上 |
| RSA | 1024-bit, PKCS1 padding | cryptoUtil.js |
| HMAC key 前缀 | `BC_SIGNATURE&` | ecloudHttpUtil.js:204 |
| clientVersion | `3.8.2` | package.json |
| companyCode | `ECloud` | deviceUtil.js:51 |
| clientType(Linux x64) | `linux_x86-64` | deviceUtil.js:210 |

### 登录流程

```
密码登录 (/login/verify)
  ├─ 成功 → accessTicket → /login/verifyAccessTicket → accessToken ✓
  ├─ 30002009 (未授信设备) → 短信 + /login/trustDevice
  ├─ 30002060 (二次验证)   → 短信 + /login/verifyTwoFactorAuthSms
  ├─ 30002063 (增强策略)   → 短信 + /login/verifyLoginEnhanceSms
  └─ userId 字段           → 4A MFA（本工具暂不支持）
```

## 文件结构

```
cloudpc-keepalive/
├── config.py          协议常量与密钥（从客户端逆向提取）
├── device.py          设备指纹采集（复刻 deviceUtil.js getCommonParams）
├── ecloud_client.py   HTTP 客户端（签名 + RSA 加解密，复刻 ecloudHttpUtil.js）
├── login.py           登录流程（复刻 service/user.js）
├── keepalive.py       保活循环
├── main.py            CLI 入口
├── requirements.txt
└── cloud_pc.json      运行时生成（账号/token/设备指纹）
```

## 局限

1. **桌面需处于开机状态**：本工具维持桌面"在线态"（防闲置关机），但不能远程**开机**。
   桌面已关机时，需先在官方客户端或网页端开机。
2. **4A MFA 登录**：`userId` 字段触发的 4A 认证流程较复杂，本工具未实现（其他登录方式都已支持）。
3. **多桌面**：当前自动选择第一个桌面保活。多桌面场景可用 `--instance-id` 指定。
4. **token 有效期**：accessToken 有效期由服务端控制（实测数小时）。失效后自动用账号密码重登，
   已授信设备无需短信。

## 依赖

- **运行时依赖**：仅 `requests` + `pycryptodome`（见 requirements.txt）
- **不依赖**：移动云客户端、uSmartView、Windows、Electron、任何 GUI 或第三方 SDK
- **跨平台**：Linux / macOS / Windows 均可，纯 Python 3.10+

## 安全提示

- `cloud_pc.json` 含明文密码，请设置文件权限 `chmod 600 cloud_pc.json`。
- 本工具仅供学习研究，请遵守移动云电脑服务条款。
