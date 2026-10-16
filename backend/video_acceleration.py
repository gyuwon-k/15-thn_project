"""Probe the installed encoder on this PC before selecting a hardware path."""
from __future__ import annotations

import subprocess
import time


PROFILES = (
    {'codec':'h264_nvenc', 'provider':'NVIDIA NVENC', 'hardware':True,
     'arguments':['-preset','p4','-rc','vbr','-cq','22','-b:v','0']},
    {'codec':'h264_qsv', 'provider':'Intel Quick Sync', 'hardware':True,
     'arguments':['-preset','veryfast','-global_quality','22']},
    {'codec':'h264_amf', 'provider':'AMD AMF', 'hardware':True,
     'arguments':['-quality','speed','-rc','cqp','-qp_i','22','-qp_p','22']},
    {'codec':'libx264', 'provider':'CPU', 'hardware':False,
     'arguments':['-preset','veryfast','-crf','22']},
)


def cpu_profile():
    return dict(PROFILES[-1])


def probe_encoder(ffmpeg, profile, width, height, fps, cancel=None, timeout=12):
    """A listed codec is insufficient; actually initialize it and encode frames."""
    command=[str(ffmpeg),'-hide_banner','-loglevel','error','-f','lavfi','-i',
             f'testsrc2=size={width}x{height}:rate={fps}','-frames:v','4','-an',
             '-c:v',profile['codec'],*profile['arguments'],'-pix_fmt','yuv420p','-f','null','-']
    process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    deadline=time.monotonic()+timeout
    try:
        while True:
            if cancel and cancel():
                raise InterruptedError('영상 생성을 취소했습니다.')
            remaining=deadline-time.monotonic()
            if remaining<=0:
                return {'available':False,'reason':'GPU_ENCODER_PROBE_TIMEOUT'}
            try:
                _,stderr=process.communicate(timeout=min(.25,remaining))
                return {'available':process.returncode==0,
                        'reason':stderr.decode(errors='replace').strip()[:1200] if process.returncode else None}
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            process.kill();process.communicate()


def select_encoder(ffmpeg, width, height, fps, *, mode='auto', cancel=None, progress=None, probe=None):
    if mode not in ('auto','cpu'):
        raise ValueError('VIDEO_ACCELERATION_MODE_INVALID')
    if mode=='cpu':
        return cpu_profile(), []
    probe=probe or probe_encoder
    attempts=[]
    for profile in PROFILES[:-1]:
        if cancel and cancel():
            raise InterruptedError('영상 생성을 취소했습니다.')
        if progress:
            progress(2,'GPU 영상 인코더 확인 · '+profile['provider'])
        result=probe(ffmpeg,profile,width,height,fps,cancel)
        attempts.append({'codec':profile['codec'],**result})
        if result['available']:
            return dict(profile), attempts
    return cpu_profile(), attempts


def encoding_arguments(ffmpeg, profile, fps, frames, output):
    return [str(ffmpeg),'-hide_banner','-y','-f','image2pipe','-vcodec','png',
            '-framerate',str(fps),'-i','pipe:0','-an','-c:v',profile['codec'],
            *profile['arguments'],'-pix_fmt','yuv420p','-movflags','+faststart',
            '-frames:v',str(frames),str(output)]
