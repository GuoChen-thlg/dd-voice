"""dd-voice：Whisper 兼容的某包语音转写服务。

免责声明：本项目通过逆向某包网页前端私有协议实现，与字节跳动、某包、OpenAI
无任何隶属或授权关系，仅供个人学习研究使用。某包网页协议可能随时变更，当前实现
不保证长期可用。使用本软件的一切法律风险与后果由使用者自行承担。
"""
from .asr import ASRError, Transcription, transcribe, transcribe_pcm
from .protocol import ProtocolError

__version__ = '0.1.0'
__all__ = ['ASRError', 'ProtocolError', 'Transcription', 'transcribe', 'transcribe_pcm']
