# dd-voice

把某包网页版（游客匿名会话）的语音识别封装为一个本机运行的 **Whisper / OpenAI 兼容转写服务**。

无需登录、无需账号、无需 API Key（可选开启鉴权）。接入任何支持 OpenAI Whisper 接口的程序（如 OpenChamber、Whisper 客户端、自定义脚本）即可使用，语音识别由某包后端完成。

> 协议基于某包网页前端的观察逆向，网页结构变化可能导致失效；与字节跳动无隶属关系，仅供学习研究使用。

## 功能

- `POST /v1/audio/transcriptions` — OpenAI Whisper 兼容转写接口（multipart：`file` + `model`）
- `GET /v1/models` — 模型列表（`doubao-voice`，兼容 `whisper-1` 别名）
- `GET /health` — 健康检查
- 支持格式：WAV（自动归一化为 16 kHz 单声道）、MP3、FLAC、OGG、WebM（内置 FFmpeg 解码）
- 单文件上限 25 MiB；默认并发 4，超出排队

## 安装

要求 Python 3.10+ 与 [uv](https://github.com/astral-sh/uv)：

```bash
uv sync
```

## 启动

```bash
# 推荐：绕过系统代理直连某包（本机代理会劫持 wss 连接时必须）
no_proxy="*" NO_PROXY="*" uv run dd-voice --port 8009
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | 8009 | 监听端口 |
| `--host` | 127.0.0.1 | 仅本机；改为非回环地址必须设置 `DDVOICE_API_KEY` |
| `--concurrency` | 4 | 转写并发数 |

## 接入

服务启动后，按以下参数配置任意 OpenAI 兼容客户端：

| 参数 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:8009/v1` |
| model | `doubao-voice`（或 `whisper-1`） |
| API Key | 未启用鉴权时任意值被忽略 |

### Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8009/v1", api_key="local",
                timeout=180, max_retries=0)
with open("audio.mp3", "rb") as audio:
    print(client.audio.transcriptions.create(model="doubao-voice", file=audio).text)
```

### curl

```bash
curl -X POST http://127.0.0.1:8009/v1/audio/transcriptions \
  -F model=doubao-voice -F "file=@audio.wav"
```

### OpenChamber 语音输入

设置 → Voice → Dictating messages 选 `server`，URL 填 `http://127.0.0.1:8009/v1`，API Key 任意。

## 鉴权（可选）

```bash
DDVOICE_API_KEY=your-key no_proxy="*" NO_PROXY="*" uv run dd-voice
```

设置后所有 `/v1/` 请求需带 `Authorization: Bearer your-key`。

## 排查

- **转写返回 502 / URLError**：通常是本机代理劫持了某包 wss 连接，按上面加 `no_proxy="*"` 启动即可。
- **麦克风无法授权（macOS）**：系统设置 → 隐私与安全性 → 麦克风，给浏览器开启权限，并完全退出浏览器（Cmd+Q）重开。
- **微信/输入法相关功能**：本项目不含微信、微信输入法后端，仅使用某包网页版。

## 已知限制

- 不支持流式响应、逐词时间戳、说话人分离；`language`/`prompt` 参数传入即报错
- 依赖某包网页协议，非官方接口，随时可能失效
- 音频按近似实时速度上行（某包网关要求），长音频耗时约为音频时长

## 法律声明与免责声明

1. **非官方实现**：本项目通过逆向某包网页前端协议实现语音转写，并非字节跳动、某包或 OpenAI 的官方产品、SDK 或合作伙伴，与上述任何主体**无任何隶属、授权或关联关系**。项目内部标识符不暗示任何官方身份。

2. **仅供学习研究**：本项目仅用于技术学习、协议研究与个人实验目的。使用本软件即表示你理解并同意其用途应符合所在国家/地区的法律法规、相关服务条款及网络使用规范。

3. **协议可能变化**：某包网页协议及其网关为私有实现，可能随时变更、下线或增加反爬/风控措施。当前实现**不能保证对任意版本或长期稳定运行**，因协议变化导致的失效、限流或中断，作者不承担责任。

4. **风险自担**：使用者须自行承担因使用本软件产生的一切风险与后果，包括但不限于：账号或服务受限、数据隐私泄露、网络请求被记录、违反第三方服务条款、以及任何直接或间接损失。作者及贡献者**不对任何使用后果负责**。

5. **开源许可**：本软件以 MIT 许可证开源（见 `LICENSE`），但本声明作为附加条款优先于对使用目的与场景的限制性说明；若二者冲突，以本声明中关于「使用目的与风险承担」的条款为准。

6. **商业使用警示**：在将本软件用于任何商业、生产或对外服务场景前，请确保你已获得相关权利方的合法授权，否则可能面临侵权、违约或行政处罚风险。
