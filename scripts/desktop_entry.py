"""Keep early frozen-start failures visible without leaking account/runtime logs."""
import json
import os
from pathlib import Path
import sys
import traceback

try:
    from bilidesk.app import main
    result=main()
except Exception as exc:
    if '--smoke-output' in sys.argv:
        output=Path(sys.argv[sys.argv.index('--smoke-output')+1])
    else:
        output=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'BiliBatchDownloaderDesk'
    output.mkdir(parents=True,exist_ok=True)
    report={'status':'STARTUP_FAILED','type':type(exc).__name__,'missing_module':getattr(exc,'name',None),
            'message':str(exc)[:1000],
            'frames':[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]}
    (output/'startup-failure.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if '--smoke-output' not in sys.argv and os.name=='nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,'应用启动失败。请查看用户数据目录中的 startup-failure.json。','Bili 作品库',0x10)
    result=1
raise SystemExit(result)
