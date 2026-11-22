import sys, json, UnityPy
UnityPy.config.FALLBACK_UNITY_VERSION = '2022.3.33f1'
path = sys.argv[1]; filt = sys.argv[2] if len(sys.argv) > 2 else ''
env = UnityPy.load(path)
for obj in env.objects:
    if obj.type.name != 'MonoBehaviour': continue
    try:
        tree = obj.read_typetree()
    except Exception as e:
        print('ERR', obj.path_id, e); continue
    name = tree.get('m_Name', '')
    keys = list(tree.keys())
    if filt and filt.lower() not in (name + ' ' + ' '.join(keys)).lower(): continue
    s = json.dumps(tree, default=str)
    print('---', obj.path_id, repr(name), 'script', tree.get('m_Script'), 'fields', keys[:30])
    print(s[:1500])
