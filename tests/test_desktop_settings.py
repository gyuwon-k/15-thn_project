import json
from pathlib import Path
import pytest
from desktop import settings

def test_default_selection_remembers_only_verified_managed_restore(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    base=settings.user_base()
    assert settings.selected_data_root()==base/'data'
    root=base/'restored'/('restore-'+'b'*32);root.mkdir(parents=True)
    record={'id':root.name,'status':'verified','data_dir':str(root)}
    (root/'restore-result.json').write_text(json.dumps(record),encoding='utf-8')
    settings.remember_storage(root)
    assert settings.selected_data_root()==root
    outside=tmp_path/('restore-'+'c'*32);outside.mkdir()
    with pytest.raises(ValueError,match='ACTIVE_STORAGE_INVALID'):settings.remember_storage(outside)
    record['status']='incomplete'
    (root/'restore-result.json').write_text(json.dumps(record),encoding='utf-8')
    with pytest.raises(ValueError,match='ACTIVE_STORAGE_NOT_VERIFIED'):settings.selected_data_root()
