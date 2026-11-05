"""某包语音转写核心：游客初始化与单次 ASR 会话。"""
from dataclasses import dataclass
import http.cookiejar
import json
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import urlencode
import urllib.request
import wave

from .protocol import ProtocolError, decode_response, encode_request

HOMEPAGE = 'https://www.doubao.com/chat/'
GATEWAY = 'wss://frontier-audio-web-ws.doubao.com/api/v2/sami/voicegenie'
# 某包网页前端的公开应用标识，不是账号 API 凭据。
APPKEY = 'GOqQpfo1fO7slHv8'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class ASRError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transcription:
    text: str
    duration: float
    elapsed: float


def bootstrap_guest(timeout=60):
    """使用全新内存 CookieJar 做游客初始化，不读取任何浏览器配置。"""
    # 强制不走系统代理，避免本机代理把某包首页请求劫持/拖慢导致转写失败。
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(
        proxy_handler, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request = urllib.request.Request(HOMEPAGE, headers={
        'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9'})
    marker_re = re.compile(r'window\._ROUTER_DATA\s*=\s*')
    try:
        with opener.open(request, timeout=timeout) as response:
            chunks = []
            total = 0
            cap = 10 * 1024 * 1024 + 1
            router = None
            # 分块读取：先找到游客数据标记，再继续读到能完整解析出 JSON 为止，
            # 避免标记所在 chunk 之后 JSON 值被截断导致解析失败。
            while total < cap:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                html = b''.join(chunks).decode('utf-8', errors='replace')
                marker = marker_re.search(html)
                if marker:
                    try:
                        router, _ = json.JSONDecoder().raw_decode(
                            html[marker.end():])
                        break
                    except ValueError:
                        continue
            data = b''.join(chunks)
        if total > 10 * 1024 * 1024:
            raise ASRError('某包首页响应过大')
        if router is None:
            raise ASRError('首页缺少游客初始化数据，网页结构可能已改变')
        layout = router['loaderData']['chat_layout']['chat_layout']
        device_id = str(layout['launchCore']['web_id'])
        if not device_id.isascii() or not device_id.isdigit() or int(device_id) <= 0:
            raise ASRError('首页未返回有效的游客设备标识')
        version = str(layout['pc_version'])
        aid = str(layout['aid'])
    except (KeyError, TypeError, ValueError) as error:
        raise ASRError('无法解析游客初始化数据，网页结构可能已改变') from error
    except OSError as error:
        raise ASRError(f'访问某包首页失败（{type(error).__name__}）') from error
    # 首页 Cookie 与 ttwid 不转发也不持久化。
    return {
        'api_app_key': APPKEY, 'namespace': 'VoiceGenie', 'version_code': '20800',
        'language': 'zh', 'device_platform': 'web', 'pkg_type': 'release_version',
        'pc_version': version, 'doubao_pc_version': version,
        'region': 'CN', 'sys_region': 'CN', 'samantha_web': '1',
        'use-olympus-account': '1', 'doubao_device_platform': 'web',
        'aid': aid, 'real_aid': aid, 'device_id': device_id, 'web_platform': 'browser',
    }


def load_wav(path):
    """校验整个 WAV 文件后读取 PCM，再创建远程会话。"""
    try:
        if Path(path).stat().st_size > MAX_AUDIO_BYTES:
            raise ASRError('WAV 文件超过 25 MiB')
        with wave.open(str(path), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(),
                    audio.getcomptype()) != (16000, 1, 2, 'NONE'):
                raise ASRError('需要单声道、16 kHz、PCM16 WAV')
            frames = audio.getnframes()
            pcm = audio.readframes(frames)
        if not frames or len(pcm) != frames * 2:
            raise ASRError('WAV 音频为空或不完整')
        return pcm, frames / 16000
    except (OSError, EOFError, wave.Error) as error:
        raise ASRError('无法读取有效 WAV 文件') from error


def transcribe(path, *, timeout=15, on_event=None):
    """按近似实时速度发送 PCM，只返回最终结果。"""
    pcm, duration = load_wav(path)
    return transcribe_pcm(pcm, duration=duration, timeout=timeout, on_event=on_event)


def transcribe_pcm(pcm, *, duration=None, timeout=15, on_event=None):
    """使用内存中已归一化的 PCM16，不持久化任何上传文件。"""
    if not 0 < timeout <= 300:
        raise ASRError('超时必须大于 0 且不超过 300 秒')
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise ASRError('需要有效的单声道 PCM16 音频')
    duration = len(pcm) / 32000 if duration is None else duration
    try:
        import websocket
    except ImportError as error:
        raise ASRError('请先安装 websocket-client 依赖') from error
    started = time.monotonic()
    # 首页拉取使用独立的较大超时：某包首页服务器限速，完整读取约需 20s，
    # 不能复用 ws 会话的 timeout（默认仅 15s），否则会提前被掐断导致转写失败。
    context = bootstrap_guest(max(timeout, 60))
    try:
        ws = websocket.create_connection(
            GATEWAY + '?' + urlencode(context), origin='https://www.doubao.com',
            timeout=timeout, header={'User-Agent': USER_AGENT})
    except (OSError, websocket.WebSocketException) as error:
        raise ASRError(f'语音连接失败（{type(error).__name__}）') from error
    incoming = queue.Queue()
    stopped = threading.Event()
    task_id = ''
    finals = []
    session_started = False
    session_finished = False

    def send(event, **kwargs):
        ws.send_binary(encode_request(event, APPKEY, task_id=task_id, **kwargs))

    def reader():
        while not stopped.is_set():
            try:
                raw = ws.recv()
                if not raw:
                    raise ASRError('语音连接在会话结束前关闭')
                incoming.put(decode_response(raw))
            except websocket.WebSocketTimeoutException:
                continue
            except (OSError, websocket.WebSocketException, ProtocolError, ASRError) as error:
                incoming.put(ASRError(f'读取语音响应失败（{type(error).__name__}）'))
                return

    def process(response):
        if isinstance(response, Exception):
            raise response
        event = response.get('event', '')
        code = response.get('status_code')
        if code not in (None, 20000000) or event in ('TaskFailed', 'SessionFailed'):
            detail = str(response.get('status_text', ''))[:200]
            raise ASRError(f'语音服务返回失败：{event} / {code} / {detail}')
        if on_event:
            on_event({'event': event, 'status_code': code})
        if event == 'ASRResponse':
            try:
                payload = response.get('payload', '{}')
                payload = json.loads(payload) if isinstance(payload, str) else payload
                results = payload.get('results', [])
                if not isinstance(results, list):
                    raise ValueError('results must be a list')
                for result in results:
                    if result.get('is_interim') is False:
                        text = result['text']
                        if not isinstance(text, str):
                            raise ValueError('text must be a string')
                        finals.append(text)
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                raise ASRError('语音识别响应格式无效') from error
        return event

    def wait_for(expected):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ASRError(f'等待 {expected} 超时')
            try:
                response = incoming.get(timeout=remaining)
            except queue.Empty as error:
                raise ASRError(f'等待 {expected} 超时') from error
            if process(response) == expected:
                return response

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        send('StartTask')
        response = wait_for('TaskStarted')
        task_id = response.get('task_id')
        if not isinstance(task_id, str) or not task_id:
            raise ASRError('创建语音任务后未收到 task_id')
        send('StartSession', payload={'business': 1, 'request_type': 3, 'asr': {
            'audio_info': {'channel': 1, 'format': 'pcm', 'sample_rate': 16000}}})
        wait_for('SessionStarted')
        session_started = True
        stream_start = time.monotonic()
        for sequence, offset in enumerate(range(0, len(pcm), 3200), 1):
            while True:
                try:
                    process(incoming.get_nowait())
                except queue.Empty:
                    break
            send('TaskRequest', audio=pcm[offset:offset + 3200], sequence=sequence)
            remaining = stream_start + min(offset + 3200, len(pcm)) / 32000 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        send('EndASR')
        wait_for('ASREnded')
        send('FinishSession')
        wait_for('SessionFinished')
        session_finished = True
        if not finals:
            raise ASRError('会话已结束，但没有最终识别结果')
        return Transcription(''.join(finals), duration, time.monotonic() - started)
    except (OSError, websocket.WebSocketException, ProtocolError) as error:
        raise ASRError(f'语音调用失败（{type(error).__name__}）') from error
    finally:
        if session_started and not session_finished:
            try:
                send('CancelSession')
            except (OSError, websocket.WebSocketException):
                pass
        stopped.set()
        try:
            ws.close(timeout=1)
        finally:
            thread.join(timeout=2)
