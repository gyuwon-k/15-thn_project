"""Storage UI/API surface; sync operations report actual completion."""
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from . import store
from . import desktop_storage as service

router=APIRouter()
@router.get('/api/storage/health')
@router.get('/api/storage/config')
def health():return service.status()
@router.get('/api/backups')
def backups():return service.list_backups()
@router.post('/api/backups')
def backup():
    try:return service.create_backup()
    except ValueError as exc:raise HTTPException(409,detail={'code':str(exc).split(':')[0],'message':str(exc)})
@router.get('/api/backups/{backup_id}')
def download(backup_id:str):
    if backup_id not in {r['id'] for r in service.list_backups()}:raise HTTPException(404,'BACKUP_NOT_FOUND')
    path=store.DATA_ROOT/'backups'/(backup_id+'.zip')
    return FileResponse(path,filename=path.name,media_type='application/zip')
@router.post('/api/backups/restore')
async def restore(file:UploadFile=File(...)):
    folder=store.DATA_ROOT/'restore-staging'/store.uid('upload-');folder.mkdir(parents=True)
    path=folder/'backup.zip';size=0
    try:
        with path.open('wb') as target:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>16*1024**3:raise HTTPException(413,'BACKUP_UPLOAD_LIMIT')
                target.write(chunk)
        return service.restore_backup(path)
    except (ValueError,RuntimeError) as exc:raise HTTPException(422,detail={'code':str(exc).split(':')[0],'message':str(exc)})
    finally:
        import shutil
        shutil.rmtree(folder,ignore_errors=True)
