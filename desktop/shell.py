"""Native WebView2 window over a private loopback FastAPI server."""
from __future__ import annotations
import argparse, json, logging, os, socket, sys, threading, time, traceback, urllib.request
from pathlib import Path

def write_report(path,value):
    if path:
        target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(target)

def run():
    parser=argparse.ArgumentParser(description='PILOT Studio 설치형 데스크톱')
    parser.add_argument('--data-dir')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--desktop-smoke',type=float)
    parser.add_argument('--report')
    parser.add_argument('--restore-activation',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    implicit_storage=not args.data_dir and not os.environ.get('PILOT_DATA_DIR')
    if implicit_storage:
        from .settings import selected_data_root
        os.environ['PILOT_DATA_DIR']=str(selected_data_root())
    if args.data_dir:os.environ['PILOT_DATA_DIR']=str(Path(args.data_dir).resolve())
    from backend import store
    store.DATA_ROOT.joinpath('logs').mkdir(parents=True,exist_ok=True)
    if getattr(sys,'frozen',False):
        # Windowed executables have no console; dependency logging still needs
        # a valid stream. Passwords are never written to these streams.
        stream=(store.DATA_ROOT/'logs'/'application.log').open('a',encoding='utf-8',buffering=1)
        sys.stdout=sys.stdout or stream;sys.stderr=sys.stderr or stream
    if args.self_test:
        import OCP, imageio_ffmpeg, webview
        from backend.desktop_storage import status
        try:
            store.init()
            result={'status':'passed','version':'1.0.0','window':'pywebview/WebView2','storage':status(),
                    'frontend_present':(store.RESOURCE_ROOT/'frontend'/'dist'/'index.html').is_file(),
                    'ffmpeg_present':Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file(),
                    'chromium_present':bool(list((store.RESOURCE_ROOT/'browsers').glob('chromium*'))),
                    'asap_runtime_present':(store.RESOURCE_ROOT/'runtime'/'asap').is_dir()}
            if not result['frontend_present'] or result['storage']['status']!='connected':result['status']='failed'
            write_report(args.report,result);print(json.dumps(result,ensure_ascii=True))
            if result['status']!='passed':raise SystemExit(2)
        finally:store.shutdown()
        return
    import webview, uvicorn
    from backend import main as backend
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    backend.SERVER_PORT=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(backend.app,host='127.0.0.1',port=backend.SERVER_PORT,log_level='warning',access_log=False))
    server_thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
    server_thread.start()
    url=f'http://127.0.0.1:{backend.SERVER_PORT}/'
    report={'status':'starting','window':'pywebview/WebView2','backend_url':url,'external_browser_opened':False}
    webview.settings['ALLOW_DOWNLOADS']=True
    from .api import DesktopApi
    native_api=DesktopApi(persist_default=implicit_storage or args.restore_activation)
    window=webview.create_window('PILOT Studio',html='<html lang="ko"><body style="font-family:Segoe UI;background:#eff3f8;padding:50px;color:#18334c"><h1>PILOT Studio</h1><p>저장소와 분석 환경을 준비하고 있습니다.</p></body></html>',width=1440,height=940,min_size=(1080,720),hidden=bool(args.desktop_smoke),confirm_close=False,js_api=native_api)
    native_api._window=window
    def ready():
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if server.started:
                window.load_url(url)
                report.update(status='loaded',storage=__import__('backend.desktop_storage',fromlist=['status']).status())
                write_report(args.report,report)
                if args.desktop_smoke:
                    time.sleep(max(1,args.desktop_smoke));window.destroy()
                return
            if not server_thread.is_alive():
                report.update(status='failed',message='백엔드 시작에 실패했습니다. logs 폴더를 확인해 주세요.')
                write_report(args.report,report)
                window.load_html('<h2>PILOT Studio 시작 실패</h2><p>사용자 데이터 폴더의 logs 진단을 확인하고 설치 프로그램으로 복구해 주세요.</p>')
                if args.desktop_smoke:window.destroy()
                return
            time.sleep(.15)
        report.update(status='failed',message='준비 제한시간을 초과했습니다.');write_report(args.report,report)
        if args.desktop_smoke:window.destroy()
    def closed():server.should_exit=True
    window.events.closed+=closed
    try:
        webview.start(ready,gui='edgechromium',debug=False,private_mode=False,storage_path=str(store.DATA_ROOT/'webview'))
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__,message='WebView2 런타임 또는 네이티브 창 실행을 확인해 주세요.')
        write_report(args.report,report)
        (store.DATA_ROOT/'logs'/'desktop-error.log').write_text(traceback.format_exc(),encoding='utf-8')
        if args.desktop_smoke:raise
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,'독립 앱 창을 시작할 수 없습니다. 설치 프로그램으로 WebView2 구성 요소를 복구해 주세요.','PILOT Studio',16)
    finally:
        server.should_exit=True;server_thread.join(timeout=20);store.shutdown();sock.close()
