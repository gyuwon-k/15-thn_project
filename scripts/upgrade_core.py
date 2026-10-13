from pathlib import Path
root=Path(__file__).resolve().parents[1]
p=root/'backend/main.py';s=p.read_text(encoding='utf-8')
if "        if action == 'load_sample':" in s:
    a=s.index("        if action == 'load_sample':");b=s.index("        if action in ('generate', 'validate'):",a)
    s=s[:a]+s[b:]
if '    if not args.no_browser:' in s:
    a=s.index('    if not args.no_browser:');b=s.index('    import uvicorn',a);s=s[:a]+s[b:]
s=s.replace("    initial['project']['input_mode']=input_mode\n",'')
s=s.replace("        commit_result(project_id, initial, result, 'models_imported')","        result['project']['input_mode']=input_mode\n        commit_result(project_id, initial, result, 'models_imported')")
s=s.replace("origin='manual', parent_plan_id=old['id']","origin=old.get('origin','manual'), parent_plan_id=old['id']")
s=s.replace("'version':'0.1.0'","'version':app.version")
s=s.replace("初期",'')
p.write_text(s,encoding='utf-8')
p=root/'backend/worker.py';s=p.read_text(encoding='utf-8')
if "            if params.get('sample'):" in s:
    a=s.index("            if params.get('sample'):");b=s.index('            add_features(result)',a);s=s[:a]+s[b:]
s=s.replace(", sample=params.get('sample', False)",'')
s=s.replace("params.get('strategy', 'baseline')","params.get('strategy', 'heuristic')")
p.write_text(s,encoding='utf-8')
print('Generic import, no sample flow bootstrap, desktop-only server entry updated')
