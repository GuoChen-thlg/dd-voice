"""Whisper 兼容 HTTP 服务：把某包语音转写暴露为 OpenAI /v1/audio/transcriptions 接口。"""
import hmac
import io
import logging
import os
import time
import uuid
import wave

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
import uvicorn

from .asr import ASRError, MAX_AUDIO_BYTES, transcribe_pcm
from .protocol import ProtocolError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('ddvoice')

MODEL_ID = 'doubao-voice'
# 兼容写死 whisper-1 的客户端
MODEL_ALIASES = {'whisper-1', 'doubao-voice'}
MODEL_CREATED = int(time.time())
MAX_CONCURRENCY = 4

app = FastAPI(title='dd-voice：Whisper 兼容某包转写服务', docs_url=None, redoc_url=None)
gate = anyio.Semaphore(MAX_CONCURRENCY)
worker_limit = anyio.CapacityLimiter(MAX_CONCURRENCY)
stats = {'last_error': None, 'last_success': None, 'active': 0}


def error_response(message, status=400, code='invalid_request_error', param=None):
    kind = 'server_error' if status >= 500 else ('authentication_error' if status == 401 else 'invalid_request_error')
    return JSONResponse({'error': {'message': message, 'type': kind, 'param': param, 'code': code}},
                        status_code=status)


@app.middleware('http')
async def auth_and_request_id(request, call_next):
    """可选 API Key 鉴权：设置环境变量 DDVOICE_API_KEY 后启用。"""
    request.state.request_id = f'req_{uuid.uuid4().hex}'
    key = os.environ.get('DDVOICE_API_KEY')
    if key and request.url.path.startswith('/v1/'):
        supplied = request.headers.get('authorization', '')
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + key).encode()):
            response = error_response('API Key 不正确。', 401, 'invalid_api_key')
            response.headers['x-request-id'] = request.state.request_id
            return response
    response = await call_next(request)
    response.headers['x-request-id'] = request.state.request_id
    return response


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return error_response(str(error.detail), error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return error_response('请求格式不正确。', 400)


@app.get('/health')
async def health():
    return {'status': 'ok', 'backend': 'doubao-web-anonymous', 'model': MODEL_ID,
            'busy': gate.value < MAX_CONCURRENCY, 'max_concurrency': MAX_CONCURRENCY,
            'active': stats['active'], 'last_error': stats['last_error'],
            'last_success': stats['last_success']}


@app.get('/v1/models')
async def models():
    return {'object': 'list', 'data': [
        {'id': alias, 'object': 'model', 'created': MODEL_CREATED, 'owned_by': 'doubao-web'}
        for alias in sorted(MODEL_ALIASES)]}


def _decode_audio(data):
    """把上传音频解码为 16 kHz 单声道 PCM16；WAV 直接读取，其余交给 ffmpeg。"""
    if data:
        try:
            with wave.open(io.BytesIO(data), 'rb') as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                        audio.getcomptype()) == (1, 2, 16000, 'NONE'):
                    duration = audio.getnframes() / 16000
                    if duration <= 0:
                        raise ASRError('音频没有有效采样')
                    pcm = audio.readframes(audio.getnframes())
                    if len(pcm) != audio.getnframes() * 2:
                        raise ASRError('WAV 数据不完整')
                    return pcm + b'\0' * (-len(pcm) % 640), duration
        except (wave.Error, EOFError):
            pass
    import subprocess
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        output = subprocess.run(
            [get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin',
             '-protocol_whitelist', 'pipe', '-i', 'pipe:0', '-vn',
             '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'],
            input=data, capture_output=True, timeout=20)
    except (OSError, RuntimeError, ImportError):
        raise ASRError('音频解码失败或超时')
    if output.returncode or not output.stdout:
        raise ASRError('无法解码音频，请上传有效的 WAV、MP3、FLAC、OGG 或 WebM')
    return output.stdout + b'\0' * (-len(output.stdout) % 640), len(output.stdout) / 32000


def _transcribe_sync(data, response_format):
    """在线程池中执行解码与转写，返回按格式包装的结果。"""
    try:
        pcm, duration = _decode_audio(data)
        result = transcribe_pcm(pcm, duration=duration)
    except ASRError as error:
        stats['last_error'] = str(error)
        raise
    stats['last_error'] = None
    stats['last_success'] = time.time()
    if response_format == 'text':
        return PlainTextResponse(result.text, media_type='text/plain; charset=utf-8')
    return JSONResponse({'text': result.text})


@app.post('/v1/audio/transcriptions')
async def transcriptions(request: Request):
    """Whisper 兼容转写接口：multipart 表单，字段 file 和 model。"""
    form = await request.form()
    upload = form.get('file')
    if not isinstance(upload, UploadFile):
        raise HTTPException(400, '缺少 file 字段')
    model = form.get('model')
    if model is not None and model not in MODEL_ALIASES:
        raise HTTPException(400, f"仅支持 model={' 或 '.join(sorted(MODEL_ALIASES))}")
    response_format = form.get('response_format') or 'json'
    if response_format not in ('json', 'text'):
        raise HTTPException(400, '仅支持 response_format=json 或 text')
    if form.get('stream') not in (None, 'false', False):
        raise HTTPException(400, '流式响应尚未实现')
    for unsupported in ('language', 'prompt', 'timestamp_granularities'):
        if form.get(unsupported) not in (None, '', 'false'):
            raise HTTPException(400, f'参数 {unsupported} 尚未实现')
    data = await upload.read()
    if not data:
        raise HTTPException(400, '音频文件为空')
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, '音频文件超过 25 MiB')
    stats['active'] += 1
    try:
        async with gate:
            return await anyio.to_thread.run_sync(
                _transcribe_sync, data, response_format, limiter=worker_limit)
    except ASRError as error:
        status = 504 if '超时' in str(error) else 502
        return error_response(str(error), status, 'doubao_unavailable')
    finally:
        stats['active'] -= 1


@app.on_event('shutdown')
async def shutdown():
    log.info('服务停止')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='dd-voice：Whisper 兼容某包转写服务')
    parser.add_argument('--port', type=int, default=8009)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--concurrency', type=int, default=None)
    args = parser.parse_args()
    global MAX_CONCURRENCY, gate, worker_limit
    MAX_CONCURRENCY = max(1, args.concurrency or MAX_CONCURRENCY)
    gate = anyio.Semaphore(MAX_CONCURRENCY)
    worker_limit = anyio.CapacityLimiter(MAX_CONCURRENCY)
    if args.host != '127.0.0.1' and not os.environ.get('DDVOICE_API_KEY'):
        raise SystemExit('暴露非本机地址前必须设置 DDVOICE_API_KEY')
    uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, workers=1, log_level='info')).run()


if __name__ == '__main__':
    main()
