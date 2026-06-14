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

## 使用

### 方式1：完整流程（登录 + 桌面保活）

```bash
# 1. 登录
python main.py login
# account: <账号>
# password: <密码>

# 2. 桌面保活（需要 instance_id，见方式2获取）
python main.py desktop-keepalive --instance-id CCA-xxxxxxxxxxxxxxxx
```

### 方式2：从抓包提取凭证（推荐首次使用）

由于 `instance_id`/`machine_id`/`ticket` 的确切获取接口尚未完全逆向，首次使用建议从抓包提取：

1. 用 Reqable 抓一次「连接桌面」的流量
2. 把 HAR 放到 `抓包/` 目录
3. 运行解密脚本（用本工具的 RSA 私钥自动解密所有密文）：
   ```bash
   python 抓包/decrypt_har.py "抓包/xxx.har"
   ```
4. 从生成的 `_DECRYPTED.md` 报告里提取：
   - `instanceId`（CCA-开头）— desktopUptime 必需
   - `accessToken`（token:开头）
   - `machineId`（UUID）— 可选
5. 启动保活：
   ```bash
   python main.py desktop-keepalive --instance-id CCA-xxx
   ```

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

## 局限与下一步

### 本工具不能做的

1. **维持桌面 SPICE 会话**：真正的桌面会话由 `uSmartView_VDI_Client.exe` 维持，
   其 SCG 网关认证 + 穿云 Trunk 多路复用 + SPICE 握手协议封装在二进制内，
   Python 层无法复刻。如果你的云电脑关机策略是"SPICE 会话闲置 N 分钟则关机"，
   本工具无效。

2. **4A MFA 登录**：`userId` 字段触发的 4A 认证流程较复杂，本工具未实现。

### 阶段2（桌面会话保活）需要的前置工作

要实现真正的桌面会话保活，需要先抓包确认 V3.8.2 的连接链路：

1. 在装了客户端的 Windows 上用 Wireshark 抓 `uSmartView_VDI_Client.exe` 的出站流量
2. 确认它是否直连 SCG 网关（端口？TLS？），还是走本地代理
3. 确认 SPICE 握手包格式（参考博客 codming.com 的 macOS 版分析，但 V3.8.2 可能不同）

抓包结论出来后，可在 `keepalive.py` 基础上扩展 `desktop_session.py` 实现 SCG 认证 +
SPICE 心跳。

### 参考但不可直接照搬的资料

- **codming.com 博客**：完整逆向了 macOS 家庭云版的 SCG+穿云+SPICE 协议。但 Windows
  V3.8.2 政企版的这些协议全在 uSmartView 二进制内，Electron 源码里零痕迹（已 grep 验证），
  博客的具体参数（SCG 端口、AES 密钥、Trunk 帧格式）必须自己抓包确认。
- **nodeseek Swilder-M/cloudpc-dist**：作者写的 Go 保活工具，但**仓库和 release 二进制
  都已删除（404）**，源码从未公开，无法获取。
- **Rgoogle/jiatingyun_pc_automation**（GitHub, 72★）：Python 方案，走 Docker 跑真客户端 +
  Xvfb + 模拟点击的"重方案"，可作为阶段2 的备选（方案B）。

## 安全提示

- `cloud_pc.json` 含明文密码，请设置文件权限 `chmod 600 cloud_pc.json`。
- 本工具仅供学习研究，请遵守移动云电脑服务条款。
